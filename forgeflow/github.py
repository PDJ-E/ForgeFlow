"""Minimal GitHub linkage through the GitHub CLI (``gh``).

Checkpoint A only *reads* GitHub: it verifies that an existing issue belongs to the job's
repository, looks up the Project items that issue is on, and reads a pull request's state so
the store can verify Gate B and record merges it actually observes. It never creates issues,
PRs, Projects, fields or labels, so the user's board stays the single source of truth
(ADR 0001).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from forgeflow.errors import GitHubError, GitHubScopeError, ToolNotFoundError, ValidationError
from forgeflow.jobs import IssueLink, Job, ProjectItemLink, PullRequestObservation
from forgeflow.proc import CommandResult, Runner, run_command
from forgeflow.store import JobStore

_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) { id number url title state }
  }
}
"""

_PROJECT_ITEMS_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      projectItems(first: 20) {
        nodes {
          id
          project { number title url }
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
      }
    }
  }
}
"""


_PULL_REQUEST_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number url state isDraft merged mergedAt
      headRefName headRefOid baseRefName
      headRepository { nameWithOwner }
    }
  }
}
"""


@dataclass(frozen=True)
class LinkResult:
    job: Job
    warnings: tuple[str, ...]


class GitHubClient:
    def __init__(self, runner: Runner = run_command, timeout: float = 30) -> None:
        self.runner = runner
        self.timeout = timeout

    def _graphql(self, query: str, owner: str, name: str, number: int) -> dict[str, Any]:
        # -f sends raw strings; -F would coerce e.g. a numeric owner name to an integer.
        argv = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={int(number)}",
        ]
        try:
            result = self.runner(argv, timeout=self.timeout)
        except ToolNotFoundError as exc:
            raise GitHubError("GitHub CLI `gh` is not installed") from exc
        return _parse_graphql(result)

    def view_issue(self, repo: str, number: int) -> IssueLink:
        owner, name = _split_slug(repo)
        data = self._graphql(_ISSUE_QUERY, owner, name, number)
        issue = (data.get("repository") or {}).get("issue")
        if not issue:
            raise GitHubError(f"{repo}#{number} is not an issue (it may be a pull request)")
        return IssueLink(
            repo=repo,
            number=int(issue["number"]),
            url=str(issue["url"]),
            node_id=str(issue["id"]),
            title=str(issue["title"]),
            state=str(issue["state"]),
        )

    def view_pull_request(self, repo: str, number: int) -> PullRequestObservation:
        """The pull request as GitHub reports it now (state, draft, merge, head commit)."""
        owner, name = _split_slug(repo)
        data = self._graphql(_PULL_REQUEST_QUERY, owner, name, number)
        pr = (data.get("repository") or {}).get("pullRequest")
        if not pr:
            raise GitHubError(f"{repo}#{number} is not a pull request")
        try:
            return PullRequestObservation(
                repo=repo,
                number=int(pr["number"]),
                url=str(pr["url"]),
                state=str(pr["state"]),
                is_draft=bool(pr["isDraft"]),
                merged=bool(pr["merged"]),
                merged_at=str(pr["mergedAt"]) if pr.get("mergedAt") else None,
                head_ref=str(pr["headRefName"]),
                head_sha=str(pr["headRefOid"]),
                head_repo=(pr.get("headRepository") or {}).get("nameWithOwner"),
                base_ref=str(pr["baseRefName"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError(f"unexpected pull request data for {repo}#{number}") from exc

    def issue_project_items(self, repo: str, number: int) -> list[ProjectItemLink]:
        """Project (v2) items for the issue. Needs the ``read:project`` token scope."""
        owner, name = _split_slug(repo)
        data = self._graphql(_PROJECT_ITEMS_QUERY, owner, name, number)
        issue = (data.get("repository") or {}).get("issue") or {}
        nodes = (issue.get("projectItems") or {}).get("nodes") or []
        items = []
        for node in nodes:
            project = node.get("project") or {}
            status = (node.get("fieldValueByName") or {}).get("name")
            items.append(
                ProjectItemLink(
                    project_number=int(project["number"]),
                    project_title=str(project["title"]),
                    project_url=str(project["url"]),
                    item_id=str(node["id"]),
                    status=status,
                )
            )
        return items


def _split_slug(repo: str) -> tuple[str, str]:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValidationError(f"repository {repo!r} is not owner/name")
    return owner, name


def _parse_graphql(result: CommandResult) -> dict[str, Any]:
    if result.timed_out:
        raise GitHubError(f"{result.describe()}")
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        payload = {}
    errors = payload.get("errors") or []
    for err in errors:
        if err.get("type") == "INSUFFICIENT_SCOPES":
            message = str(err.get("message", ""))
            scope = "read:project" if "read:project" in message else "unknown"
            raise GitHubScopeError(
                f"the GitHub token lacks the {scope} scope; run `gh auth refresh -s {scope}`",
                required_scope=scope,
            )
    if errors:
        raise GitHubError("; ".join(str(e.get("message", e)) for e in errors))
    if not result.ok:
        raise GitHubError(result.describe())
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GitHubError("unexpected response from `gh api graphql`")
    return data


def link_issue(store: JobStore, job_id: str, number: int, client: GitHubClient) -> LinkResult:
    """Attach an existing, open issue in the job's repository (and its Project items)."""
    job = store.load(job_id)
    if job.is_terminal:
        raise ValidationError(f"job {job_id} is {job.state.value}")
    if not job.repo.slug:
        raise ValidationError("the job's repository has no GitHub remote")
    issue = client.view_issue(job.repo.slug, number)
    if issue.state != "OPEN":
        raise ValidationError(f"{job.repo.slug}#{number} is {issue.state}, not OPEN")

    warnings: list[str] = []
    items: list[ProjectItemLink] = []
    try:
        items = client.issue_project_items(job.repo.slug, number)
        lookup = "ok" if items else "blocked: issue is not on any GitHub Project"
        if not items:
            warnings.append(
                f"{job.repo.slug}#{number} is not on any GitHub Project; add it to "
                "the board before execution"
            )
    except GitHubScopeError as exc:
        lookup = f"blocked: token lacks {exc.required_scope}"
        warnings.append(str(exc))

    def apply(j: Job) -> None:
        j.github.issue = issue
        j.github.project_items = items
        j.github.project_lookup = lookup

    updated = store.update_if_issue_free(
        job_id,
        job.repo.slug,
        number,
        apply,
        details={"issue": issue.url, "project_lookup": lookup},
    )
    return LinkResult(updated, tuple(warnings))
