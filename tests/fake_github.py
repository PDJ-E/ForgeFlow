"""A stateful stand-in for `gh api` (GraphQL and REST), for handoff tests. No network.

It keeps issues, comments and Project items in memory and answers exactly the queries and
mutations ``forgeflow.github.GitHubClient`` sends, so retries, interruptions and permission
failures can be replayed deterministically.
"""

from __future__ import annotations

import json
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
        statuses: Sequence[str] | None = ("Backlog", "Ready", "Building", "Done"),
        mutation_scope: bool = True,
    ) -> None:
        self.repo = repo
        self.private = private
        self.statuses = statuses
        self.mutation_scope = mutation_scope
        self.offline = False
        self.crash_after: set[str] = set()  # write names: create_issue, add_item, set_status
        self.issues: dict[int, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}  # item id -> {content, status}
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

    def _rest(self, argv: Sequence[str], method: str, path: str, body: Any) -> CommandResult:
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
