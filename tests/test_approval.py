"""Gate A: only a human at an interactive console approves, and only the exact version shown."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from forgeflow import approval, cli, design
from forgeflow.dispatch import run_builder
from forgeflow.errors import (
    InvalidTransitionError,
    LeaseError,
    ProviderError,
    ValidationError,
)
from forgeflow.jobs import UNASSIGNED_PROVIDER, Job, JobState, Policy
from forgeflow.providers import MockProvider, ProviderTask
from forgeflow.worktree import WorktreeManager, prepare_job_worktree
from tests.support import PROJECT_ROOT, GitRepoTestCase, ScriptedConsole

S = JobState


class ConsoleDetectionTests(GitRepoTestCase):
    def test_refuses_without_a_console_on_either_stream(self) -> None:
        for stdin_ok, stdout_ok in ((False, True), (True, False), (False, False)):
            with self.subTest(stdin=stdin_ok, stdout=stdout_ok):
                answers = {0: stdin_ok, 1: stdout_ok}
                with (
                    mock.patch.object(approval, "_is_console", side_effect=answers.__getitem__),
                    mock.patch.object(sys, "stdin", mock.Mock(fileno=lambda: 0)),
                    mock.patch.object(sys, "stdout", mock.Mock(fileno=lambda: 1)),
                    self.assertRaises(approval.ApprovalUnavailableError),
                ):
                    approval.open_human_console()

    def test_redirected_streams_are_not_a_console(self) -> None:
        with (
            mock.patch.object(sys, "stdin", io.StringIO("yes\n")),
            self.assertRaises(approval.ApprovalUnavailableError),
        ):
            approval.open_human_console()  # StringIO has no file descriptor at all

    def test_cli_approve_in_a_real_non_interactive_process_records_nothing(self) -> None:
        """What an agent's tool call looks like: a child process with piped stdin/stdout."""
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        proc = subprocess.run(
            [sys.executable, "-m", "forgeflow", "--repo", str(self.repo), "approve", job.job_id],
            input="yes\n",
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("needs a human at an interactive console", proc.stderr)
        self.assertNotIn("GATE A", proc.stdout)  # refused before showing or asking anything
        self.assertIs(self.store.load(job.job_id).state, S.AWAITING_PLAN_APPROVAL)


class CeremonyTests(GitRepoTestCase):
    def submitted(self) -> Job:
        return self.store.submit_plan(self.new_job().job_id, "docs/plan.md")

    def test_shows_exact_plan_version_hash_and_consequences(self) -> None:
        job = self.submitted()
        console = ScriptedConsole()
        context = approval.ApprovalContext(
            summary=["Tasks 1"], authorises=["publishing to a NEW GitHub issue in example/demo"]
        )
        with mock.patch("forgeflow.approval.open_human_console", return_value=console):
            approved = approval.approve_plan(self.store, job.job_id, context)
        self.assertIn("Add a greeting.", console.shown)  # the plan text itself
        self.assertIn(f"sha256       {job.plan.sha256}", console.shown)
        self.assertIn("version 1", console.shown)
        self.assertIn("publishing to a NEW GitHub issue", console.shown)
        self.assertIn("dispatching Claude Code, Codex", console.shown)  # what it does NOT allow
        record = approved.plan.approval
        assert record is not None
        self.assertEqual(
            (record.subject_sha256, record.subject_version, record.method),
            (job.plan.sha256, 1, "interactive-console"),
        )

    def test_wrong_code_empty_answer_and_eof_record_nothing(self) -> None:
        job = self.submitted()
        for answer in ("yes", "", "approve"):
            with self.subTest(answer=answer), self.assertRaises(approval.ApprovalDeclinedError):
                self.approve(job.job_id, ScriptedConsole(answer))

        class Eof(ScriptedConsole):
            def read_line(self, prompt: str) -> str:
                raise EOFError

        with self.assertRaises(approval.ApprovalDeclinedError):
            self.approve(job.job_id, Eof())
        current = self.store.load(job.job_id)
        self.assertIs(current.state, S.AWAITING_PLAN_APPROVAL)
        self.assertIsNone(current.plan.approval)

    def test_each_attempt_uses_a_fresh_code(self) -> None:
        codes = {approval.one_time_code() for _ in range(50)}
        self.assertEqual(len(codes), 50)

    def test_plan_edited_while_the_human_reads_is_not_approved(self) -> None:
        job = self.submitted()

        def edit() -> None:
            (self.repo / "docs" / "plan.md").write_text("# Plan\n\nMore.\n", encoding="utf-8")

        with self.assertRaisesRegex(InvalidTransitionError, "plan changed after submission"):
            self.approve(job.job_id, ScriptedConsole(before_answer=edit))
        self.assertIsNone(self.store.load(job.job_id).plan.approval)

    def test_resubmission_while_the_human_reads_is_not_approved(self) -> None:
        job = self.submitted()

        def resubmit_same_bytes() -> None:
            # Same file, new version: the version shown is no longer the pending one.
            self.store.transition(job.job_id, S.DRAFT, reason="resubmit")
            self.store.submit_plan(job.job_id, "docs/plan.md")

        with self.assertRaisesRegex(InvalidTransitionError, "changed while the plan was being"):
            self.approve(job.job_id, ScriptedConsole(before_answer=resubmit_same_bytes))
        current = self.store.load(job.job_id)
        self.assertEqual(current.plan.version, 2)
        self.assertIsNone(current.plan.approval)

    def test_only_a_submitted_plan_can_be_approved(self) -> None:
        draft = self.new_job()
        with self.assertRaisesRegex(InvalidTransitionError, "submit a plan first"):
            self.approve(draft.job_id)
        approved = self.approved_job()
        with self.assertRaisesRegex(InvalidTransitionError, "already approved"):
            self.approve(approved.job_id)


class UnauthorizedApprovalTests(GitRepoTestCase):
    """Approval records an agent could produce without the ceremony are refused everywhere."""

    def test_generic_transition_cannot_approve(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        with self.assertRaisesRegex(InvalidTransitionError, "captured only by `forgeflow approve`"):
            self.store.transition(job.job_id, S.PLAN_APPROVED, approval=self.approval_for(job))

    def test_non_human_methods_are_refused(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        for method in ("test-fixture", "model-claim", "interactive-cli"):
            with (
                self.subTest(method),
                self.assertRaisesRegex(InvalidTransitionError, "not accepted"),
            ):
                self.store._record_plan_approval(
                    job.job_id, self.approval_for(job, method), expected_revision=job.revision
                )

    def test_approval_of_another_version_is_refused(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        stale = self.approval_for(job)
        stale.subject_version = 7
        with self.assertRaisesRegex(InvalidTransitionError, "submitted plan version"):
            self.store._record_plan_approval(job.job_id, stale, expected_revision=job.revision)

    def test_hand_written_approval_is_refused_at_later_boundaries(self) -> None:
        job = self.approved_job()

        def forge(data: dict[str, object]) -> None:
            plan = data["plan"]
            assert isinstance(plan, dict)
            plan["approval"]["method"] = "model-claim"  # "the user approved this"

        self.tamper(job.job_id, forge)
        with self.assertRaisesRegex(InvalidTransitionError, "not by a human"):
            self.store.verify_plan_approval(job.job_id)
        prepare_job_worktree(self.store, WorktreeManager(self.repo), job.job_id)
        with self.assertRaisesRegex(InvalidTransitionError, "not by a human"):
            self.store.transition(job.job_id, S.READY)

    def test_plan_edited_after_approval_is_refused_at_later_boundaries(self) -> None:
        job = self.approved_job()
        (self.repo / "docs" / "plan.md").write_text("# Plan\n\nScope creep.\n", encoding="utf-8")
        with self.assertRaisesRegex(InvalidTransitionError, "changed after approval"):
            self.store.verify_plan_approval(job.job_id)


class StateBoundaryTests(GitRepoTestCase):
    def test_revoke_requires_reason_and_no_handoff(self) -> None:
        job = self.approved_job()
        with self.assertRaisesRegex(InvalidTransitionError, "requires a reason"):
            self.store.transition(job.job_id, S.DRAFT)
        revoked = self.store.transition(job.job_id, S.DRAFT, reason="rethink")
        self.assertIsNone(revoked.plan.approval)
        self.assertEqual(revoked.plan.version, 1)

    def test_approved_but_unpublished_job_cannot_become_ready(self) -> None:
        job = self.approved_job()
        with self.assertRaisesRegex(InvalidTransitionError, "worktree"):
            self.store.transition(job.job_id, S.READY)

    def test_schema_v1_records_still_load(self) -> None:
        job = self.approved_job()

        def downgrade(data: dict[str, object]) -> None:
            data["schema_version"] = 1
            plan = data["plan"]
            assert isinstance(plan, dict)
            for key in ("version", "submitted_at"):
                plan.pop(key)
            plan["approval"].pop("subject_version")
            github = data["github"]
            assert isinstance(github, dict)
            github.pop("handoff")

        self.tamper(job.job_id, downgrade)
        loaded = self.store.load(job.job_id)
        self.assertEqual(loaded.plan.version, 0)
        self.assertIsNone(loaded.github.handoff)
        updated = self.store.update(job.job_id, lambda j: None, event="touch")
        raw = json.loads((self.store.job_dir(job.job_id) / "job.json").read_text("utf-8"))
        self.assertEqual((updated.schema_version, raw["schema_version"]), (2, 2))


class PrematureDispatchTests(GitRepoTestCase):
    """No builder can start before Checkpoint C's gated path, whatever discovery produced."""

    def test_no_lease_before_ready(self) -> None:
        draft = self.new_job()
        awaiting = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        approved = self.approved_job()
        for job in (draft, awaiting, approved):
            with self.subTest(job.state.value), self.assertRaises(LeaseError):
                self.store.acquire_lease(job.job_id, "builder", "mock")

    def test_unassigned_builder_cannot_hold_a_lease(self) -> None:
        job = self.ready_job(builder=UNASSIGNED_PROVIDER)
        for provider in ("claude", "codex", "mock", UNASSIGNED_PROVIDER):
            with self.subTest(provider), self.assertRaisesRegex(LeaseError, "no builder assigned"):
                self.store.acquire_lease(job.job_id, "builder", provider)

    def test_run_builder_refuses_an_unready_job(self) -> None:
        job = self.approved_job()
        provider = MockProvider("mock", writes={"x.txt": "x"})
        lease = mock.Mock(job_id=job.job_id, token="t", active=True)
        task = mock.Mock(spec=ProviderTask, role="builder", job_id=job.job_id)
        with self.assertRaises((ProviderError, InvalidTransitionError, LeaseError)):
            run_builder(self.store, lease, provider, task)
        self.assertEqual(provider.executed, [])
        self.assertIs(self.store.load(job.job_id).state, S.PLAN_APPROVED)

    def test_discovery_jobs_have_no_builder(self) -> None:
        from forgeflow import discovery

        job, _ = discovery.start(self.store, "Idea", "idea", self.repo_record())
        self.assertEqual(job.policy, Policy(UNASSIGNED_PROVIDER, None))


class CliApproveTests(GitRepoTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def test_approve_without_console_exits_3_and_changes_nothing(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        with mock.patch.object(approval, "_is_console", return_value=False):
            code, _, err = self.run_cli("approve", job.job_id)
        self.assertEqual(code, 3)
        self.assertIn("py -3.13 -m forgeflow approve", err)
        self.assertIs(self.store.load(job.job_id).state, S.AWAITING_PLAN_APPROVAL)

    def test_approve_with_console_and_no_project_records_only(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        with mock.patch("forgeflow.approval.open_human_console", return_value=ScriptedConsole()):
            with mock.patch("forgeflow.cli.open_human_console"):
                code, out, _ = self.run_cli("approve", job.job_id)
        self.assertEqual(code, 0, out)
        self.assertIn("Approved plan version 1", out)
        self.assertIn("forgeflow handoff", out)
        self.assertIs(self.store.load(job.job_id).state, S.PLAN_APPROVED)

    def test_design_commands_report_validation(self) -> None:
        from forgeflow import discovery

        job, _ = discovery.start(self.store, "Idea", "idea", self.repo_record())
        code, out, _ = self.run_cli("design", "new", job.job_id)
        self.assertEqual(code, 0)
        self.assertIn("TODO(forgeflow)", out)
        code, out, _ = self.run_cli("design", "check", job.job_id)
        self.assertEqual(code, 1)
        code, _, err = self.run_cli("design", "submit", job.job_id)
        self.assertEqual(code, 1)
        self.assertIn("not ready to submit", err)
        with self.assertRaises(ValidationError):
            design.revise(self.store, job.job_id, "")
