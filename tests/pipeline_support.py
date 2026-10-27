"""Fixture for Checkpoint C tests: an approved, handed-off job ready to run.

REAL: the git repository, its committed ``.forge/forgeflow.toml``, the job's worktree, the check
commands, ForgeFlow's own commits, and pushes (to a local bare repository standing in for
``origin``). MOCKED: GitHub (``FakeGitHubWorld``, which reads PR heads from that bare
repository) and the providers (``tests/helpers/fake_cli.py`` behind the real Claude and Codex
provider classes, so every provider result is marked mocked).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from forgeflow.board import BoardListener
from forgeflow.config import ProjectBinding
from forgeflow.github import GitHubClient
from forgeflow.handoff import handoff
from forgeflow.jobs import Job, utc_now
from forgeflow.pipeline import RunOptions, run_job
from forgeflow.providers import ClaudeCodeProvider, CodexProvider, Provider
from tests.fake_github import PROJECT_URL, FakeGitHubWorld
from tests.support import GitRepoTestCase, run_git

# The check passes once greet.txt says hello. It runs in the job's worktree.
CHECK = (
    "import pathlib, sys; sys.exit(0 if 'hello' in pathlib.Path('greet.txt').read_text() else 1)"
)
CONFIG = f"""[checks]
timeout_seconds = 120

[[checks.commands]]
name = "greeting"
argv = ["{{python}}", "-c", "{CHECK}"]
"""

APPROVE: dict[str, Any] = {"verdict": "approve", "summary": "meets the plan", "findings": []}


def request_changes(issue: str, severity: str = "major") -> dict[str, Any]:
    finding = {"severity": severity, "file": "greet.txt", "issue": issue, "suggestion": "fix it"}
    return {"verdict": "request-changes", "summary": "needs work", "findings": [finding]}


class PipelineTestCase(GitRepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        (self.repo / ".forge").mkdir()
        (self.repo / ".forge" / "forgeflow.toml").write_text(CONFIG, encoding="utf-8")
        (self.repo / "greet.txt").write_text("todo\n", encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", "configure checks")
        run_git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        self.remote = self.tmp / "remote.git"
        run_git(self.tmp, "init", "-q", "--bare", str(self.remote))
        run_git(self.repo, "remote", "set-url", "--push", "origin", str(self.remote))
        self.world = FakeGitHubWorld()
        self.world.remote = self.remote
        self.client = GitHubClient(runner=self.world)
        self.binding = ProjectBinding(
            owner="example",
            number=2,
            project_id="PVT_project",
            title="ForgeFlow Development",
            url=PROJECT_URL,
            ready_status="Ready",
            bound_at=utc_now(),
        )
        self.lines: list[str] = []

    def handed_off_job(self) -> Job:
        from forgeflow.jobs import UNASSIGNED_PROVIDER

        job = self.approved_job(builder=UNASSIGNED_PROVIDER, reviewer=None)
        return handoff(self.store, job.job_id, self.client, self.binding).job

    def providers(
        self, builder: list[dict[str, Any]] | dict[str, Any], reviewer: Any
    ) -> tuple[Callable[[str], Provider], list[str], list[str]]:
        """Claude builds and Codex reviews, both through the fake CLI."""
        b = self.fake_cli("claude", builder)
        r = self.fake_cli("codex", reviewer)
        made = {"claude": ClaudeCodeProvider(command=b), "codex": CodexProvider(command=r)}
        return made.__getitem__, b, r

    def run_pipeline(self, job_id: str, factory: Callable[[str], Provider], **options: Any) -> Job:
        options.setdefault("builder", "claude")
        options.setdefault("reviewer", "codex")
        return run_job(
            self.store,
            job_id,
            RunOptions(**options),
            providers=factory,
            github=self.client,
            board=BoardListener(self.store, self.client, self.binding),
            report=self.lines.append,
        )

    def board_status(self, job: Job) -> str | None:
        assert job.github.issue is not None
        item = self.world.item_for(job.github.issue.number)
        return item["status"] if item else None

    def statuses_set(self) -> list[str]:
        """Every Status ForgeFlow wrote, in order (handoff's Ready included)."""
        names = {f"opt-{n.lower()}": n for n in self.world.statuses or ()}
        return [
            names[a.split("=", 1)[1]]
            for call in self.world.calls
            if any("updateProjectV2ItemFieldValue" in part for part in call)
            for a in call
            if a.startswith("optionId=")
        ]

    def worktree(self, job: Job) -> Path:
        assert job.worktree is not None
        return Path(job.worktree.path)
