"""A stateful stand-in for `gh api` (GraphQL and REST), for handoff tests. No network.

It keeps issues, comments and Project items in memory and answers exactly the queries and
mutations ``forgeflow.github.GitHubClient`` sends, so retries, interruptions and permission
failures can be replayed deterministically.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from forgeflow.proc import DEFAULT_TIMEOUT, CommandResult

PROJECT_URL = "https://github.com/users/example/projects/2"


class SimulatedCrash(Exception):
    """Raised right after a write took effect, as if ForgeFlow died before recording it."""


class FakeGitHubWorld:
    def __init__(
        self,
        repo: str = "example/demo",
        *,
        private: bool = False,
        statuses: Sequence[str] | None = (
            "Backlog",
            "Ready",
            "Building",
            "QA",
            "Review",
            "Blocked",
            "Done",
        ),
        mutation_scope: bool = True,
    ) -> None:
        self.repo = repo
        self.private = private
        self.statuses = statuses
        self.mutation_scope = mutation_scope
        self.offline = False
        # write names: create_issue, add_item, set_status, create_pr, update_pr
        self.crash_after: set[str] = set()
        self.issues: dict[int, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}  # item id -> {content, status}
        self.pulls: dict[int, dict[str, Any]] = {}  # PR number -> REST-like record
        # A local bare repository standing in for the GitHub remote: PR heads are read from it,
        # so a PR shows exactly what was really pushed.
        self.remote: Path | None = None
        self.calls: list[tuple[str, ...]] = []
        self.writes: list[str] = []

    # --- helpers for tests -------------------------------------------------------------------

    def seed_issue(self, number: int, *, state: str = "OPEN", body: str = "") -> None:
        self.issues[number] = self._issue(number, f"Existing issue {number}", body, state)

    def item_for(self, number: int) -> dict[str, Any] | None:
        node = self.issues[number]["id"]
        return next((i for i in self.items.values() if i["content"] == node), None)

    def _issue(self, number: int, title: str, body: str, state: str = "OPEN") -> dict[str, Any]:
        return {
            "id": f"I_node{number}",
            "number": number,
            "url": f"https://github.com/{self.repo}/issues/{number}",
            "title": title,
            "state": state,
            "body": body,
            "comments": [],
        }

    def _ok(self, argv: Sequence[str], payload: Any) -> CommandResult:
        return CommandResult(tuple(argv), 0, json.dumps(payload), "")

    def _write(self, name: str) -> None:
        self.writes.append(name)

    def _maybe_crash(self, name: str) -> None:
        if name in self.crash_after:
            self.crash_after.discard(name)
            raise SimulatedCrash(f"interrupted right after {name}")

    # --- the runner ------------------------------------------------------------------------------

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.calls.append(tuple(argv))
        if self.offline:
            return CommandResult(
                tuple(argv), 1, "", "error connecting to api.github.com\ncheck your internet"
            )
        if argv[:3] == ["gh", "api", "--method"]:
            return self._rest(argv, argv[3], argv[4], json.loads(input_text or "null"))
        if list(argv[:3]) == ["gh", "api", "graphql"]:
            query = next(a for a in argv if a.startswith("query="))
            variables = dict(
                a.split("=", 1) for a in argv[3:] if "=" in a and not a.startswith("query=")
            )
            return self._graphql(argv, query, variables)
        raise AssertionError(f"unexpected command {argv}")

    # --- pull requests ---------------------------------------------------------------------------

    def branch_head(self, branch: str) -> str:
        if self.remote is None:
            raise AssertionError("set world.remote to a bare repository to use pull requests")
        out = subprocess.run(
            ["git", "-C", str(self.remote), "rev-parse", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            raise AssertionError(f"branch {branch} was never pushed")
        return out.stdout.strip()

    def _pr_rest(self, pr: dict[str, Any]) -> dict[str, Any]:
        return {
            "number": pr["number"],
            "html_url": f"https://github.com/{self.repo}/pull/{pr['number']}",
            "node_id": f"PR_node{pr['number']}",
            "state": pr["state"],
            "draft": pr["draft"],
            "merged_at": "2026-10-26T00:00:00Z" if pr["merged"] else None,
            "head": {"ref": pr["head"], "sha": self.branch_head(pr["head"])},
            "base": {"ref": pr["base"]},
            "body": pr["body"],
            "title": pr["title"],
        }

    def merge(self, number: int) -> None:
        """The human merges on GitHub (only ever done by a test acting as the owner)."""
        pr = self.pulls[number]
        pr.update(state="closed", merged=True, merged_sha=self.branch_head(pr["head"]))

    def _pulls_rest(self, argv: Sequence[str], method: str, path: str, body: Any) -> CommandResult:
        prefix = f"repos/{self.repo}/pulls"
        if method == "GET" and path.startswith(prefix + "?"):
            query = dict(p.split("=", 1) for p in path.split("?", 1)[1].split("&"))
            head = query["head"].split(":", 1)[1]
            found = [self._pr_rest(p) for p in self.pulls.values() if p["head"] == head]
            return self._ok(argv, found)
        if method == "POST" and path == prefix:
            if any(p["head"] == body["head"] and p["state"] == "open" for p in self.pulls.values()):
                return CommandResult(
                    tuple(argv), 1, "", "gh: A pull request already exists (HTTP 422)"
                )
            self.branch_head(body["head"])  # GitHub refuses a PR from a branch it does not have
            number = max([*self.issues, *self.pulls], default=0) + 1
            self.pulls[number] = {
                "number": number,
                "head": body["head"],
                "base": body["base"],
                "title": body["title"],
                "body": body["body"],
                "draft": bool(body.get("draft")),
                "state": "open",
                "merged": False,
            }
            self._write("create_pr")
            self._maybe_crash("create_pr")
            return self._ok(argv, self._pr_rest(self.pulls[number]))
        if method == "PATCH" and path.startswith(prefix + "/"):
            pr = self.pulls[int(path.rsplit("/", 1)[1])]
            pr.update(title=body["title"], body=body["body"])
            self._write("update_pr")
            self._maybe_crash("update_pr")
            return self._ok(argv, self._pr_rest(pr))
        return CommandResult(tuple(argv), 1, "", f"gh: Not Found (HTTP 404) {path}")

    def _pr_graphql(self, number: int) -> dict[str, Any] | None:
        pr = self.pulls.get(number)
        if pr is None:
            return None
        head = pr.get("merged_sha") or self.branch_head(pr["head"])
        return {
            "number": number,
            "url": f"https://github.com/{self.repo}/pull/{number}",
            "state": "MERGED" if pr["merged"] else pr["state"].upper(),
            "isDraft": pr["draft"],
            "merged": pr["merged"],
            "mergedAt": "2026-10-26T00:00:00Z" if pr["merged"] else None,
            "headRefName": pr["head"],
            "headRefOid": head,
            "baseRefName": pr["base"],
            "headRepository": {"nameWithOwner": self.repo},
        }

    def _rest(self, argv: Sequence[str], method: str, path: str, body: Any) -> CommandResult:
        if path.startswith(f"repos/{self.repo}/pulls"):
            return self._pulls_rest(argv, method, path, body)
        prefix = f"repos/{self.repo}/issues"
        if method == "POST" and path == prefix:
            number = max(self.issues, default=0) + 1
            self.issues[number] = self._issue(number, body["title"], body["body"])
            self._write("create_issue")
            self._maybe_crash("create_issue")
            issue = self.issues[number]
            return self._ok(
                argv,
                {
                    "number": number,
                    "html_url": issue["url"],
                    "node_id": issue["id"],
                    "title": issue["title"],
                    "state": "open",
                },
            )
        if path.startswith(prefix + "/") and "/comments" in path:
            number = int(path[len(prefix) + 1 :].split("/", 1)[0])
            issue = self.issues[number]
            if method == "GET":
                return self._ok(argv, [{"body": c} for c in issue["comments"]])
            issue["comments"].append(body["body"])
            self._write("comment")
            return self._ok(argv, {"id": len(issue["comments"])})
        return CommandResult(tuple(argv), 1, "", f"gh: Not Found (HTTP 404) {path}")

    def _graphql(self, argv: Sequence[str], query: str, v: dict[str, str]) -> CommandResult:
        if "mutation" in query and not self.mutation_scope:
            error = {
                "type": "INSUFFICIENT_SCOPES",
                "message": "Your token has not been granted the required scopes to execute "
                "this query. The 'addProjectV2ItemById' field requires one of the following "
                "scopes: ['project'], but your token has only been granted the: ['repo'] scopes.",
            }
            return CommandResult(tuple(argv), 1, json.dumps({"errors": [error]}), "")
        if "convertPullRequestToDraft" in query:
            self.pulls[int(v["id"].removeprefix("PR_node"))]["draft"] = True
            self._write("convert_to_draft")
            converted = {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}
            return self._ok(argv, {"data": converted})
        if "pullRequest(number" in query:
            pr_node = self._pr_graphql(int(v["number"]))
            return self._ok(argv, {"data": {"repository": {"pullRequest": pr_node}}})
        if "addProjectV2ItemById" in query:
            existing = next(
                (k for k, i in self.items.items() if i["content"] == v["contentId"]), None
            )
            item_id = existing or f"PVTI_{len(self.items) + 1}"
            if existing is None:
                self.items[item_id] = {"content": v["contentId"], "status": None}
                self._write("add_item")
            self._maybe_crash("add_item")
            return self._ok(argv, {"data": {"addProjectV2ItemById": {"item": {"id": item_id}}}})
        if "updateProjectV2ItemFieldValue" in query:
            names = {f"opt-{n.lower()}": n for n in self.statuses or ()}
            self.items[v["itemId"]]["status"] = names[v["optionId"]]
            self._write("set_status")
            self._maybe_crash("set_status")
            data = {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": v["itemId"]}}}
            return self._ok(argv, {"data": data})
        if "hasIssuesEnabled" in query:
            repo = {
                "id": "R_repo",
                "nameWithOwner": self.repo,
                "isPrivate": self.private,
                "hasIssuesEnabled": True,
                "viewerPermission": "ADMIN",
            }
            return self._ok(argv, {"data": {"repository": repo}})
        if "projectV2(number" in query:
            field = (
                {
                    "id": "PVTSSF_status",
                    "name": "Status",
                    "options": [{"id": f"opt-{n.lower()}", "name": n} for n in self.statuses],
                }
                if self.statuses is not None
                else None
            )
            project = {
                "id": "PVT_project",
                "number": int(v["project"]),
                "title": "ForgeFlow Development",
                "url": PROJECT_URL,
                "closed": False,
                "field": field,
            }
            return self._ok(argv, {"data": {"repositoryOwner": {"projectV2": project}}})
        if "issues(first: 100" in query:
            nodes = [
                {k: i[k] for k in ("id", "number", "url", "title", "state", "body")}
                for i in sorted(self.issues.values(), key=lambda i: -i["number"])
            ]
            return self._ok(argv, {"data": {"repository": {"issues": {"nodes": nodes}}}})
        number = int(v["number"])
        issue = self.issues.get(number)
        if "projectItems" in query:
            nodes = []
            if issue is not None:
                for item_id, item in self.items.items():
                    if item["content"] == issue["id"]:
                        status = {"name": item["status"]} if item["status"] else None
                        project = {
                            "number": 2,
                            "title": "ForgeFlow Development",
                            "url": PROJECT_URL,
                        }
                        nodes.append(
                            {"id": item_id, "project": project, "fieldValueByName": status}
                        )
            found: dict[str, Any] = {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}
            return self._ok(argv, {"data": found})
        if issue is None:
            return CommandResult(
                tuple(argv),
                1,
                json.dumps({"errors": [{"type": "NOT_FOUND", "message": "no such issue"}]}),
                "",
            )
        node = {k: issue[k] for k in ("id", "number", "url", "title", "state")}
        return self._ok(argv, {"data": {"repository": {"issue": node}}})
