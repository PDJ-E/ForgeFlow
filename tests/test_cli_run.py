"""The run, resume, rework and sync commands: refusals that change nothing, and status output.

REAL: git, the store, the CLI. MOCKED: provider preflight (patched, so no real CLI is
started) and, where a pipeline runs, the providers (fake CLI) and GitHub (FakeGitHubWorld).
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from forgeflow import cli
from forgeflow.jobs import JobState
from forgeflow.providers import ClaudeCodeProvider, CodexProvider
from tests.pipeline_support import APPROVE, PipelineTestCase

S = JobState


class CliRunTests(PipelineTestCase):
    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            mock.patch.object(ClaudeCodeProvider, "preflight"),
            mock.patch.object(CodexProvider, "preflight"),
        ):
            code = cli.main(["--repo", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def test_run_refuses_an_unpublished_plan_and_changes_nothing(self) -> None:
        job = self.approved_job()
        code, _, err = self.cli(
            "run", job.ref or job.job_id, "--builder", "claude", "--reviewer", "codex"
        )
        self.assertEqual(code, 1)
        self.assertIn("forgeflow handoff", err)
        after = self.store.load(job.job_id)
        self.assertEqual((after.state, after.worktree), (S.PLAN_APPROVED, None))
        self.assertEqual([e for e in after.evidence], [])

    def test_run_refuses_a_draft(self) -> None:
        job = self.new_job()
        code, _, err = self.cli("run", job.job_id, "--builder", "claude", "--reviewer", "codex")
        self.assertEqual(code, 1)
        self.assertIn("Gate A", err)

    def test_unknown_providers_are_rejected_by_the_parser(self) -> None:
        job = self.new_job()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(["--repo", str(self.repo), "run", job.job_id, "--builder", "gemini"])
        self.assertEqual(ctx.exception.code, 2)

    def test_resume_needs_a_started_job(self) -> None:
        job = self.handed_off_job()
        code, _, err = self.cli("resume", job.job_id)
        self.assertEqual(code, 1)
        self.assertIn("nothing to resume", err)

    def test_rework_needs_a_delivered_pr(self) -> None:
        job = self.handed_off_job()
        code, _, err = self.cli("rework", job.job_id, "--reason", "rename it")
        self.assertEqual(code, 1)
        self.assertIn("awaiting_merge_approval", err)

    def test_status_shows_the_latest_evidence_and_board(self) -> None:
        job = self.handed_off_job()
        factory, _, _ = self.providers({"writes": {"greet.txt": "hello\n"}}, {"review": APPROVE})
        self.run_pipeline(job.job_id, factory)
        code, out, _ = self.cli("status", job.job_id)
        self.assertEqual(code, 0)
        for expected in ("latest checks", "passed", "real", "latest review", "mocked", "Blocked"):
            self.assertIn(expected, out)

    def test_only_learn_is_still_unimplemented(self) -> None:
        self.assertEqual(set(cli.NOT_YET), {"learn"})
