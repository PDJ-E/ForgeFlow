"""Minimal GitHub linkage through the GitHub CLI (``gh``).

Reads: an issue in the job's repository, the Project items it is on, a pull request's state
(Gate B and merge records), repository metadata and a Project's Status field.

Writes (Checkpoint B, used only by ``forgeflow.handoff`` after Gate A): create an issue, comment
on an issue, add an issue to an existing Project and set its Status to an existing option.
Checkpoint C adds, for ``forgeflow.delivery`` and ``forgeflow.board`` only: open a **draft**
pull request from the job's branch, update its title and body, turn it back into a draft, and
set the Status of the job's own Project item. ForgeFlow never merges, closes issues, or creates
or edits Projects, fields, options, labels or repository settings, so the user's board stays
the single source of truth (ADR 0001).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from forgeflow.errors import GitHubError, GitHubScopeError, ToolNotFoundError, ValidationError
from forgeflow.jobs import (
    TERMINAL_STATES,
    IssueLink,
    Job,
    JobState,
    ProjectItemLink,
    PullRequestObservation,
)
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


_REPOSITORY_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id nameWithOwner isPrivate hasIssuesEnabled viewerPermission
  }
}
"""

# Newest first and not search-indexed, so an issue created a moment ago is always listed.
_RECENT_ISSUES_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    issues(first: 100, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { id number url title state body }
    }
  }
}
"""

_PROJECT_QUERY = """
query($owner: String!, $project: Int!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $project) {
        id number title url closed
        field(name: "Status") {
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
    }
  }
}
"""

_ADD_ITEM_MUTATION = """
mutation($projectId: ID!, $contentId: ID!) {
  addProjectV2ItemById(input: {projectId: $projectId, contentId: $contentId}) { item { id } }
}
"""

_SET_STATUS_MUTATION = """
mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $optionId: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
    value: {singleSelectOptionId: $optionId}
  }) { projectV2Item { id } }
}
"""


_CONVERT_TO_DRAFT_MUTATION = """
mutation($id: ID!) {
  convertPullRequestToDraft(input: {pullRequestId: $id}) { pullRequest { isDraft } }
}
"""


@dataclass(frozen=True)
class LinkResult:
    job: Job
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryInfo:
    id: str
    slug: str
    is_private: bool
    has_issues: bool
    viewer_permission: str | None

    @property
    def visibility(self) -> str:
        return "private" if self.is_private else "PUBLIC"


@dataclass(frozen=True)
class ProjectInfo:
    id: str
    number: int
    title: str
    url: str
    closed: bool
    status_field_id: str | None
    status_options: dict[str, str]  # option name -> option id

    def option_id(self, name: str) -> str | None:
        for option, option_id in self.status_options.items():
            if option.casefold() == name.casefold():
                return option_id
        return None


@dataclass(frozen=True)
class PullRequestSummary:
    """A pull request as listed or written through the REST API (delivery bookkeeping)."""

    number: int
    url: str
    node_id: str
    state: str  # "open" | "closed"
    draft: bool
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    body: str

    @classmethod
    def from_rest(cls, data: Any) -> PullRequestSummary:
        try:
            return cls(
                number=int(data["number"]),
                url=str(data["html_url"]),
                node_id=str(data["node_id"]),
                state=str(data["state"]),
                draft=bool(data.get("draft")),
                merged=bool(data.get("merged_at")),
                head_ref=str(data["head"]["ref"]),
                head_sha=str(data["head"]["sha"]),
                base_ref=str(data["base"]["ref"]),
                body=str(data.get("body") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("unexpected pull request data from GitHub") from exc


@dataclass(frozen=True)
class IssueBody:
    """An issue with its body, for finding ForgeFlow's trace marker."""

    link: IssueLink
    body: str


class GitHubClient:
    def __init__(self, runner: Runner = run_command, timeout: float = 30) -> None:
        self.runner = runner
        self.timeout = timeout

    def graphql(self, query: str, variables: dict[str, str | int]) -> dict[str, Any]:
        # -f sends raw strings; -F would coerce e.g. a numeric owner name to an integer.
        argv = ["gh", "api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if isinstance(value, int):
                argv += ["-F", f"{key}={int(value)}"]
            else:
                argv += ["-f", f"{key}={value}"]
        try:
            result = self.runner(argv, timeout=self.timeout)
        except ToolNotFoundError as exc:
            raise GitHubError("GitHub CLI `gh` is not installed") from exc
        return _parse_graphql(result)

    def _graphql(self, query: str, owner: str, name: str, number: int) -> dict[str, Any]:
        return self.graphql(query, {"owner": owner, "name": name, "number": int(number)})

    def rest(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        """A REST call; a JSON body travels on stdin, so its size is not limited by argv."""
        argv = ["gh", "api", "--method", method, path]
        if body is not None:
            argv += ["--input", "-"]
        try:
            result = self.runner(
                argv,
                timeout=self.timeout,
                input_text=json.dumps(body) if body is not None else None,
            )
        except ToolNotFoundError as exc:
            raise GitHubError("GitHub CLI `gh` is not installed") from exc
        if result.timed_out or not result.ok:
            raise GitHubError(_rest_failure(result))
        try:
            return json.loads(result.stdout) if result.stdout.strip() else None
        except ValueError as exc:
            raise GitHubError(f"unexpected response from `gh api {path}`") from exc

    # --- reads used by the handoff -------------------------------------------------------------

    def repository_info(self, repo: str) -> RepositoryInfo:
        owner, name = _split_slug(repo)
        data = self.graphql(_REPOSITORY_QUERY, {"owner": owner, "name": name})
        node = data.get("repository")
        if not node:
            raise GitHubError(f"repository {repo} was not found or is not visible to this token")
        return RepositoryInfo(
            id=str(node["id"]),
            slug=str(node["nameWithOwner"]),
            is_private=bool(node["isPrivate"]),
            has_issues=bool(node["hasIssuesEnabled"]),
            viewer_permission=node.get("viewerPermission"),
        )

    def recent_issues(self, repo: str) -> list[IssueBody]:
        owner, name = _split_slug(repo)
        data = self.graphql(_RECENT_ISSUES_QUERY, {"owner": owner, "name": name})
        nodes = ((data.get("repository") or {}).get("issues") or {}).get("nodes") or []
        return [
            IssueBody(
                IssueLink(
                    repo=repo,
                    number=int(n["number"]),
                    url=str(n["url"]),
                    node_id=str(n["id"]),
                    title=str(n["title"]),
                    state=str(n["state"]),
                ),
                str(n.get("body") or ""),
            )
            for n in nodes
            if n
        ]

    def project_info(self, owner: str, number: int) -> ProjectInfo:
        """A user's or organisation's Project (v2) and its Status field. Read-only."""
        data = self.graphql(_PROJECT_QUERY, {"owner": owner, "project": int(number)})
        project = (data.get("repositoryOwner") or {}).get("projectV2")
        if not project:
            raise GitHubError(f"GitHub Project #{number} of {owner} was not found")
        field = project.get("field") or {}
        return ProjectInfo(
            id=str(project["id"]),
            number=int(project["number"]),
            title=str(project["title"]),
            url=str(project["url"]),
            closed=bool(project.get("closed")),
            status_field_id=field.get("id"),
            status_options={str(o["name"]): str(o["id"]) for o in field.get("options") or []},
        )

    def issue_comments(self, repo: str, number: int) -> list[str]:
        _split_slug(repo)
        data = self.rest("GET", f"repos/{repo}/issues/{int(number)}/comments?per_page=100")
        return [str(c.get("body") or "") for c in data or []]

    # --- writes (handoff only, after Gate A) -----------------------------------------------------

    def create_issue(self, repo: str, title: str, body: str) -> IssueLink:
        _split_slug(repo)
        data = self.rest("POST", f"repos/{repo}/issues", {"title": title, "body": body})
        try:
            return IssueLink(
                repo=repo,
                number=int(data["number"]),
                url=str(data["html_url"]),
                node_id=str(data["node_id"]),
                title=str(data["title"]),
                state=str(data["state"]).upper(),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("unexpected response when creating the issue") from exc

    def add_issue_comment(self, repo: str, number: int, body: str) -> None:
        _split_slug(repo)
        self.rest("POST", f"repos/{repo}/issues/{int(number)}/comments", {"body": body})

    def add_project_item(self, project_id: str, content_id: str) -> str:
        data = self.graphql(_ADD_ITEM_MUTATION, {"projectId": project_id, "contentId": content_id})
        try:
            return str(data["addProjectV2ItemById"]["item"]["id"])
        except (KeyError, TypeError) as exc:
            raise GitHubError("unexpected response when adding the issue to the Project") from exc

    def set_project_status(
        self, project_id: str, item_id: str, field_id: str, option_id: str
    ) -> None:
        self.graphql(
            _SET_STATUS_MUTATION,
            {
                "projectId": project_id,
                "itemId": item_id,
                "fieldId": field_id,
                "optionId": option_id,
            },
        )

    # --- pull requests (delivery only, after checks and review passed) ---------------------------

    def pull_requests_for_branch(self, repo: str, branch: str) -> list[PullRequestSummary]:
        """Every pull request (open or closed) whose head is ``branch`` of ``repo`` itself."""
        owner, _ = _split_slug(repo)
        data = self.rest("GET", f"repos/{repo}/pulls?head={owner}:{branch}&state=all&per_page=100")
        return [PullRequestSummary.from_rest(p) for p in data or []]

    def create_draft_pull_request(
        self, repo: str, *, head: str, base: str, title: str, body: str
    ) -> PullRequestSummary:
        _split_slug(repo)
        data = self.rest(
            "POST",
            f"repos/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "draft": True},
        )
        return PullRequestSummary.from_rest(data)

    def update_pull_request(
        self, repo: str, number: int, *, title: str, body: str
    ) -> PullRequestSummary:
        _split_slug(repo)
        data = self.rest(
            "PATCH", f"repos/{repo}/pulls/{int(number)}", {"title": title, "body": body}
        )
        return PullRequestSummary.from_rest(data)

    def convert_to_draft(self, node_id: str) -> None:
        self.graphql(_CONVERT_TO_DRAFT_MUTATION, {"id": node_id})

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


_SCOPES = re.compile(r"scopes?: \['([^']+)'")


def _rest_failure(result: CommandResult) -> str:
    """A precise message for a failed ``gh api`` call, naming auth and network problems."""
    text = (result.stderr + "\n" + result.stdout).lower()
    detail = result.describe()
    if result.timed_out:
        return f"GitHub did not answer in time ({detail})"
    if "http 401" in text or "gh auth login" in text or "authentication" in text:
        return f"GitHub CLI is not authenticated; run `gh auth login` ({detail})"
    if "http 403" in text or "resource not accessible" in text:
        return f"the GitHub token is not allowed to do this ({detail})"
    if "http 404" in text:
        return f"not found, or not visible to this token ({detail})"
    if "error connecting" in text or "could not resolve" in text or "timeout" in text:
        return f"GitHub is unreachable ({detail})"
    return detail


def _parse_graphql(result: CommandResult) -> dict[str, Any]:
    if result.timed_out:
        raise GitHubError(f"{result.describe()}")
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    errors = payload.get("errors") or []
    for err in errors:
        if err.get("type") == "INSUFFICIENT_SCOPES":
            message = str(err.get("message", ""))
            match = _SCOPES.search(message)
            scope = match.group(1) if match else "unknown"
            raise GitHubScopeError(
                f"the GitHub token lacks the {scope} scope; run `gh auth refresh -s {scope}`",
                required_scope=scope,
            )
        if err.get("type") == "FORBIDDEN":
            raise GitHubError(
                f"GitHub refused the operation (permission denied): {err.get('message', '')}"
            )
    if errors:
        raise GitHubError("; ".join(str(e.get("message", e)) for e in errors))
    if not result.ok:
        raise GitHubError(_rest_failure(result))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GitHubError("unexpected response from `gh api graphql`")
    return data


_UNAPPROVED = frozenset({JobState.DRAFT, JobState.AWAITING_PLAN_APPROVAL})


def link_issue(store: JobStore, job_id: str, number: int, client: GitHubClient) -> LinkResult:
    """Attach an existing, open issue in the job's repository (and its Project items).

    Only approved work is linked to an issue (Gate A): drafts and plans awaiting approval stay
    discussion-only.
    """
    job = store.load(job_id)
    if job.state in TERMINAL_STATES or job.state in _UNAPPROVED:
        raise ValidationError(
            f"job {job_id} is {job.state.value}; only an approved plan is linked to an issue"
        )
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
