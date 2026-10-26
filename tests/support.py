"""Shared test fixtures: throwaway git repositories with isolated git configuration."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from forgeflow.approval import approve_plan
from forgeflow.gitrepo import discover_identity
from forgeflow.jobs import (
    HUMAN_APPROVAL_METHOD,
    Approval,
    Job,
    JobState,
    Policy,
    PullRequestLink,
    Repo,
    utc_now,
)
from forgeflow.proc import DEFAULT_TIMEOUT, CommandResult
from forgeflow.store import JobStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FAKE_CLI = PROJECT_ROOT / "tests" / "helpers" / "fake_cli.py"


def _on_rm_error(func: Callable[..., object], path: str, _exc: BaseException) -> None:
    # Git marks object files read-only; Windows refuses to delete those.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def rmtree(path: Path) -> None:
    shutil.rmtree(path, onexc=_on_rm_error)


def run_git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return out.stdout.rstrip()  # keep leading columns, e.g. porcelain " M"


class GitRepoTestCase(unittest.TestCase):
    """Each test gets a fresh repository whose ``origin/main`` points at the initial commit."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="forgeflow-test-"))
        self.addCleanup(rmtree, self.tmp)
        gitconfig = self.tmp / "gitconfig"
        gitconfig.write_text(
            "[user]\n\tname = ForgeFlow Test\n\temail = test@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n[core]\n\tautocrlf = false\n"
            "[commit]\n\tgpgsign = false\n",
            encoding="utf-8",
        )
        env = mock.patch.dict(
            os.environ,
            {
                "GIT_CONFIG_GLOBAL": str(gitconfig),
                "GIT_CONFIG_NOSYSTEM": "1",
                "PYTHONPATH": str(PROJECT_ROOT),
            },
        )
        env.start()
        self.addCleanup(env.stop)

        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        run_git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("# demo\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "plan.md").write_text("# Plan\n\nAdd a greeting.\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", "initial")
        run_git(self.repo, "remote", "add", "origin", "https://github.com/example/demo.git")
        run_git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        run_git(self.repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.store = JobStore.for_repo(self.repo, lock_timeout=5)

    # --- job helpers -----------------------------------------------------------------------

    def repo_record(self) -> Repo:
        ident = discover_identity(self.repo)
        return Repo(
            root=ident.root,
            slug=ident.slug,
            remote_url=ident.remote_url,
            base_branch=ident.base_branch,
        )

    def new_job(
        self,
        title: str = "Add a greeting",
        builder: str = "mock",
        reviewer: str | None = "mock-reviewer",
    ) -> Job:
        return self.store.create(title, self.repo_record(), Policy(builder, reviewer))

    def approval_for(self, job: Job, method: str = HUMAN_APPROVAL_METHOD) -> Approval:
        """An approval record as a hostile or buggy caller could build it by hand."""
        assert job.plan.sha256 is not None
        return Approval(
            gate="plan",
            approver="test-owner",
            approved_at=utc_now(),
            subject_sha256=job.plan.sha256,
            method=method,
            subject_version=job.plan.version,
        )

    def approve(self, job_id: str, console: ScriptedConsole | None = None) -> Job:
        """Gate A through the real ceremony; only the console is simulated (a test double)."""
        console = console or ScriptedConsole()
        with mock.patch("forgeflow.approval.open_human_console", return_value=console):
            return approve_plan(self.store, job_id)

    def approved_job(self, **kwargs: str | None) -> Job:
        job = self.new_job(**kwargs)  # type: ignore[arg-type]
        job = self.store.submit_plan(job.job_id, "docs/plan.md")
        return self.approve(job.job_id)

    def ready_job(self, issue_number: int = 7, **kwargs: str | None) -> Job:
        from forgeflow.github import GitHubClient, link_issue
        from forgeflow.worktree import WorktreeManager, prepare_job_worktree

        job = self.approved_job(**kwargs)
        prepare_job_worktree(self.store, WorktreeManager(self.repo), job.job_id)
        fake = FakeGitHub({issue_number: "OPEN"})
        link_issue(self.store, job.job_id, issue_number, GitHubClient(runner=fake))
        return self.store.transition(job.job_id, JobState.READY)

    def link_draft_pr(self, job_id: str, number: int = 42) -> Job:
        def apply(j: Job) -> None:
            j.github.pull_request = PullRequestLink(
                number=number,
                url=f"https://github.com/example/demo/pull/{number}",
                is_draft=True,
                state="OPEN",
            )

        return self.store.update(job_id, apply, event="pr-linked")

    def fake_cli(self, cli: str, steps: dict[str, Any] | list[dict[str, Any]]) -> list[str]:
        """A provider ``command`` running tests/helpers/fake_cli.py; its results are mocked."""
        self._fake_clis = getattr(self, "_fake_clis", 0) + 1
        script = self.tmp / f"fake-{cli}-{self._fake_clis}.json"
        script.write_text(json.dumps(steps), encoding="utf-8")
        return [sys.executable, str(FAKE_CLI), cli, str(script)]

    def fake_cli_log(self, command: list[str]) -> list[dict[str, Any]]:
        log = Path(command[3]).with_suffix(".log")
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    def tamper(self, job_id: str, edit: Callable[[dict[str, Any]], None]) -> None:
        """Edit job.json behind the store's back, as a buggy or hostile local process could."""
        path = self.store.job_dir(job_id) / "job.json"
        data = json.loads(path.read_text("utf-8"))
        edit(data)
        path.write_text(json.dumps(data), encoding="utf-8")


class ScriptedConsole:
    """Test double for the human at the console: answers ``answer`` (default "y")."""

    kind = "test-console"

    def __init__(
        self,
        answer: str = "y",
        *,
        before_answer: Callable[[], None] | None = None,
        type_ahead: str | None = None,
    ) -> None:
        self.answer = answer
        self.before_answer = before_answer
        # Keys pressed before the question appeared; discard_pending_input() drops them.
        self.pending = type_ahead
        self.shown = ""
        self.prompts: list[str] = []
        self.calls: list[str] = []

    def write(self, text: str) -> None:
        self.shown += text

    def discard_pending_input(self) -> None:
        self.calls.append("discard")
        self.pending = None

    def read_line(self, prompt: str) -> str:
        self.calls.append("read")
        self.prompts.append(prompt)
        if self.before_answer is not None:
            self.before_answer()  # e.g. someone edits the plan while the human reads it
        if self.pending is not None:  # type-ahead would answer if it had not been discarded
            return self.pending
        return self.answer


def pull_request(
    number: int,
    *,
    head_ref: str,
    head_sha: str,
    draft: bool = True,
    state: str = "OPEN",
    merged: bool = False,
    repo: str = "example/demo",
    base_ref: str = "main",
) -> dict[str, Any]:
    """A `gh api graphql` pullRequest node, as FakeGitHub returns it."""
    return {
        "number": number,
        "url": f"https://github.com/{repo}/pull/{number}",
        "state": "MERGED" if merged else state,
        "isDraft": draft,
        "merged": merged,
        "mergedAt": "2026-10-25T00:00:00Z" if merged else None,
        "headRefName": head_ref,
        "headRefOid": head_sha,
        "baseRefName": base_ref,
        "headRepository": {"nameWithOwner": repo},
    }


class FakeGitHub:
    """Stands in for `gh api graphql`. Issues map number -> state; PR numbers are not issues."""

    def __init__(
        self,
        issues: Mapping[int, str],
        *,
        project_scope: bool = True,
        project_items: Sequence[dict[str, object]] | None = None,
        pull_requests: Mapping[int, dict[str, Any]] | None = None,
    ) -> None:
        self.issues = dict(issues)
        self.pull_requests = dict(pull_requests or {})
        self.project_scope = project_scope
        self.project_items = (
            list(project_items)
            if project_items is not None
            else [
                {
                    "id": "PVTI_test",
                    "project": {
                        "number": 1,
                        "title": "ForgeFlow",
                        "url": "https://github.com/users/example/projects/1",
                    },
                    "fieldValueByName": {"name": "Todo"},
                }
            ]
        )
        self.calls: list[tuple[str, ...]] = []

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
        args = dict(a.split("=", 1) for a in argv if "=" in a and not a.startswith("query="))
        query = next(a for a in argv if a.startswith("query="))
        number = int(args["number"])
        if "pullRequest(" in query:
            pr = self.pull_requests.get(number)
            pr_body = {"data": {"repository": {"pullRequest": pr}}}
            return CommandResult(tuple(argv), 0, json.dumps(pr_body), "")
        if "projectItems" in query:
            if not self.project_scope:
                body: dict[str, Any] = {
                    "errors": [
                        {
                            "type": "INSUFFICIENT_SCOPES",
                            "message": "The 'projectItems' field requires one of the "
                            "following scopes: ['read:project']",
                        }
                    ]
                }
                return CommandResult(tuple(argv), 1, json.dumps(body), "gh: insufficient scopes")
            nodes = self.project_items if number in self.issues else []
            body = {"data": {"repository": {"issue": {"projectItems": {"nodes": nodes}}}}}
            return CommandResult(tuple(argv), 0, json.dumps(body), "")
        if number not in self.issues:
            body = {
                "data": {"repository": {"issue": None}},
                "errors": [
                    {
                        "type": "NOT_FOUND",
                        "message": f"Could not resolve to an Issue with the number of {number}.",
                    }
                ],
            }
            return CommandResult(tuple(argv), 1, json.dumps(body), "gh: not found")
        issue = {
            "id": f"I_kw{number}",
            "number": number,
            "url": f"https://github.com/{args['owner']}/{args['name']}/issues/{number}",
            "title": f"Issue {number}",
            "state": self.issues[number],
        }
        body = {"data": {"repository": {"issue": issue}}}
        return CommandResult(tuple(argv), 0, json.dumps(body), "")
