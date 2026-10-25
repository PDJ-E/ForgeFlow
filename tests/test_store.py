from __future__ import annotations

import json
import sys
import unittest

from forgeflow.errors import (
    ActiveJobError,
    ConcurrencyError,
    DuplicateWorkerError,
    InvalidTransitionError,
    LeaseError,
    StaleLeaseError,
    ValidationError,
)
from forgeflow.github import GitHubClient
from forgeflow.jobs import Approval, Job, JobState, utc_now
from forgeflow.store import JobStore
from tests.support import FakeGitHub, GitRepoTestCase, pull_request, run_git

S = JobState


class CreationAndReadTests(GitRepoTestCase):
    def test_create_draft_persists_and_logs(self) -> None:
        job = self.new_job()
        self.assertEqual(job.state, S.DRAFT)
        self.assertEqual(self.store.load(job.job_id).to_dict(), job.to_dict())
        self.assertEqual([e["event"] for e in self.store.events(job.job_id)], ["created"])

    def test_runtime_dir_is_ignored_by_git(self) -> None:
        self.new_job()
        self.assertEqual(run_git(self.repo, "status", "--porcelain"), "")

    def test_reads_do_not_create_the_runtime(self) -> None:
        store = JobStore.for_repo(self.tmp / "elsewhere")
        self.assertEqual(store.list_jobs(), ([], []))
        self.assertFalse((self.tmp / "elsewhere").exists())

    def test_unknown_job(self) -> None:
        with self.assertRaises(ValidationError):
            self.store.load("ff-20000101-000000")

    def test_corrupt_record_is_reported_not_hidden(self) -> None:
        job = self.new_job()
        (self.store.job_dir(job.job_id) / "job.json").write_text("{not json", encoding="utf-8")
        jobs, problems = self.store.list_jobs()
        self.assertEqual(jobs, [])
        self.assertEqual(len(problems), 1)


class GateATests(GitRepoTestCase):
    def test_draft_cannot_be_approved_or_dispatched(self) -> None:
        job = self.new_job()
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.PLAN_APPROVED)
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.BUILDING)
        with self.assertRaises(LeaseError):
            self.store.acquire_lease(job.job_id, "builder", "mock")
        self.assertEqual(self.store.load(job.job_id).state, S.DRAFT)

    def test_approval_is_required(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.PLAN_APPROVED)

    def test_approval_must_match_the_submitted_plan(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        wrong = Approval(
            gate="plan",
            approver="owner",
            approved_at=utc_now(),
            subject_sha256="0" * 64,
            method="test-fixture",
        )
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.PLAN_APPROVED, approval=wrong)

    def test_plan_edited_after_submission_cannot_be_approved(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        (self.repo / "docs" / "plan.md").write_text(
            "# Plan\n\nAlso delete prod.\n", encoding="utf-8"
        )
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.PLAN_APPROVED, approval=self.approval_for(job))

    def test_approval_is_recorded_in_job_and_event_log(self) -> None:
        job = self.approved_job()
        self.assertEqual(job.plan.approval.approver, "test-owner")  # type: ignore[union-attr]
        events = self.store.events(job.job_id)
        self.assertEqual(events[-1]["details"]["approval"]["gate"], "plan")

    def test_plan_edited_after_approval_blocks_dispatch(self) -> None:
        job = self.ready_job()
        (self.repo / "docs" / "plan.md").write_text("# Plan\n\nScope creep.\n", encoding="utf-8")
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaisesRegex(InvalidTransitionError, "changed after approval"):
                self.store.transition(job.job_id, S.BUILDING, lease_token=lease.token)

    def test_plan_path_must_stay_inside_repository(self) -> None:
        job = self.new_job()
        for bad in ("../outside.md", "C:/Windows/win.ini", "/etc/passwd"):
            with (
                self.subTest(bad=bad),
                self.assertRaises((ValidationError, InvalidTransitionError)),
            ):
                self.store.submit_plan(job.job_id, bad)

    def test_revising_returns_to_draft_and_forgets_the_submission(self) -> None:
        job = self.store.submit_plan(self.new_job().job_id, "docs/plan.md")
        job = self.store.transition(job.job_id, S.DRAFT, reason="needs more discovery")
        self.assertIsNone(job.plan.sha256)


class TransitionGuardTests(GitRepoTestCase):
    def test_disallowed_transition_is_rejected_and_state_unchanged(self) -> None:
        job = self.approved_job()
        before = self.store.load(job.job_id)
        with self.assertRaisesRegex(InvalidTransitionError, "not allowed"):
            self.store.transition(job.job_id, S.VERIFYING)
        self.assertEqual(self.store.load(job.job_id).to_dict(), before.to_dict())

    def test_interrupted_cannot_be_set_directly(self) -> None:
        job = self.ready_job()
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.INTERRUPTED)

    def test_ready_requires_worktree_and_issue(self) -> None:
        job = self.approved_job()
        with self.assertRaisesRegex(InvalidTransitionError, "worktree"):
            self.store.transition(job.job_id, S.READY)

    def test_cancel_requires_reason(self) -> None:
        job = self.new_job()
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.CANCELLED)
        job = self.store.transition(job.job_id, S.CANCELLED, reason="duplicate idea")
        self.assertTrue(job.is_terminal)

    def test_update_cannot_bypass_the_state_machine(self) -> None:
        job = self.new_job()

        def sneaky(j: Job) -> None:
            j.state = S.BUILDING

        with self.assertRaises(InvalidTransitionError):
            self.store.update(job.job_id, sneaky, event="sneaky")
        self.assertEqual(self.store.load(job.job_id).state, S.DRAFT)

    def test_stale_revision_is_rejected(self) -> None:
        job = self.new_job()
        self.store.update(job.job_id, lambda j: None, event="touch")
        with self.assertRaises(ConcurrencyError):
            self.store.update(
                job.job_id, lambda j: None, event="stale", expected_revision=job.revision
            )

    def test_worker_transitions_require_the_lease(self) -> None:
        job = self.ready_job()
        with self.assertRaises(LeaseError):
            self.store.transition(job.job_id, S.BUILDING)
        with self.assertRaises(LeaseError):
            self.store.transition(job.job_id, S.BUILDING, lease_token="forged")

    def test_delivery_states_are_not_reachable_through_transition(self) -> None:
        job = self.ready_job()
        with self.assertRaisesRegex(InvalidTransitionError, "request_merge_approval"):
            self.store.transition(job.job_id, S.AWAITING_MERGE_APPROVAL)
        with self.assertRaisesRegex(InvalidTransitionError, "record_merge"):
            self.store.transition(job.job_id, S.MERGED)

    def test_gate_b_needs_a_linked_pr_and_passing_checks(self) -> None:
        job = self.ready_job()
        assert job.worktree is not None
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            t = lease.token
            self.store.transition(job.job_id, S.BUILDING, lease_token=t)
            self.store.transition(job.job_id, S.VERIFYING, lease_token=t)
            failing = ((sys.executable, "-c", "raise SystemExit(1)"),)
            self.assertEqual(self.store.run_checks(job.job_id, t, failing).status, "failed")
            self.store.transition(job.job_id, S.REVIEWING, lease_token=t)
            head = run_git(self.repo, "rev-parse", "HEAD")  # the worktree starts at main's HEAD
            gh = GitHubClient(
                runner=FakeGitHub(
                    {},
                    pull_requests={
                        42: pull_request(42, head_ref=job.worktree.branch, head_sha=head)
                    },
                )
            )
            with self.assertRaisesRegex(InvalidTransitionError, "draft pull request"):
                self.store.request_merge_approval(job.job_id, t, gh)
            self.link_draft_pr(job.job_id)
            with self.assertRaisesRegex(InvalidTransitionError, "checks did not pass"):
                self.store.request_merge_approval(job.job_id, t, gh)
            self.store.transition(job.job_id, S.NEEDS_HUMAN, lease_token=t, reason="red CI")

    def test_rework_is_bounded(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            t = lease.token
            self.store.transition(job.job_id, S.BUILDING, lease_token=t)
            for _ in range(2):  # default limit: two rework rounds
                self.store.transition(job.job_id, S.VERIFYING, lease_token=t)
                self.store.transition(job.job_id, S.BUILDING, lease_token=t)
            self.store.transition(job.job_id, S.VERIFYING, lease_token=t)
            with self.assertRaisesRegex(InvalidTransitionError, "rework limit"):
                self.store.transition(job.job_id, S.BUILDING, lease_token=t)
            job = self.store.transition(
                job.job_id,
                S.NEEDS_HUMAN,
                lease_token=t,
                reason="checks still failing after 2 rework rounds",
            )
        self.assertEqual((job.attempts.build, job.attempts.rework), (3, 2))
        self.assertIsNone(job.lease)


class SingleSlotTests(GitRepoTestCase):
    def test_only_one_job_may_be_active(self) -> None:
        self.ready_job(issue_number=1)
        second = self.approved_job()
        from forgeflow.github import GitHubClient, link_issue
        from forgeflow.worktree import WorktreeManager, prepare_job_worktree
        from tests.support import FakeGitHub

        prepare_job_worktree(self.store, WorktreeManager(self.repo), second.job_id)
        link_issue(self.store, second.job_id, 2, GitHubClient(runner=FakeGitHub({2: "OPEN"})))
        with self.assertRaisesRegex(ActiveJobError, "execution slot"):
            self.store.transition(second.job_id, S.READY)

    def test_slot_frees_when_the_active_job_ends(self) -> None:
        first = self.ready_job(issue_number=1)
        self.store.transition(first.job_id, S.CANCELLED, reason="superseded")
        self.ready_job(issue_number=2)

    def test_unreadable_record_fails_closed(self) -> None:
        other = self.new_job()
        (self.store.job_dir(other.job_id) / "job.json").write_text("garbage", encoding="utf-8")
        with self.assertRaisesRegex(ActiveJobError, "unreadable"):
            self.ready_job()


class LeaseTests(GitRepoTestCase):
    def test_second_worker_is_refused_while_first_is_alive(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock"):
            with self.assertRaises(DuplicateWorkerError):
                self.store.acquire_lease(job.job_id, "builder", "mock")

    def test_only_the_assigned_builder_may_take_the_job(self) -> None:
        job = self.ready_job()
        with self.assertRaisesRegex(LeaseError, "needs its builder"):
            self.store.acquire_lease(job.job_id, "builder", "codex")

    def test_releasing_mid_phase_marks_the_job_interrupted(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            self.store.transition(job.job_id, S.BUILDING, lease_token=lease.token)
        job = self.store.load(job.job_id)
        self.assertEqual((job.state, job.interrupted_from), (S.INTERRUPTED, S.BUILDING))
        self.assertIsNone(job.lease)

    def test_stale_lease_record_requires_recovery(self) -> None:
        job = self.ready_job()
        lease = self.store.acquire_lease(job.job_id, "builder", "mock")
        lease._os_lock.release()  # simulate the holder vanishing without cleanup
        with self.assertRaises(StaleLeaseError):
            self.store.acquire_lease(job.job_id, "builder", "mock")
        report = self.store.recover(job.job_id)
        self.assertEqual(report.outcome, "recovered")
        self.assertEqual(self.store.load(job.job_id).state, S.READY)
        self.store.acquire_lease(job.job_id, "builder", "mock").release()

    def test_recover_does_nothing_while_worker_lives(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock"):
            self.assertEqual(self.store.recover(job.job_id).outcome, "running")

    def test_events_record_lease_lifecycle(self) -> None:
        job = self.ready_job()
        with self.store.acquire_lease(job.job_id, "builder", "mock"):
            pass
        names = [e["event"] for e in self.store.events(job.job_id)]
        self.assertIn("lease-acquired", names)
        self.assertEqual(names[-1], "lease-released")

    def test_force_release_requires_reason(self) -> None:
        job = self.ready_job()
        with self.assertRaises(ValidationError):
            self.store.force_release_lease(job.job_id, " ")


class EvidenceTests(GitRepoTestCase):
    def test_evidence_is_written_and_flagged(self) -> None:
        job = self.new_job()
        item = self.store.record_evidence(
            job.job_id, "review", "approve", {"findings": []}, mocked=True
        )
        stored = json.loads((self.store.job_dir(job.job_id) / item.path).read_text("utf-8"))
        self.assertTrue(stored["mocked"])
        self.assertEqual(self.store.load(job.job_id).evidence[0].path, item.path)
        with self.assertRaisesRegex(ValidationError, "run_checks"):
            self.store.record_evidence(job.job_id, "checks", "passed", {}, mocked=False)

    def test_evidence_kind_is_validated(self) -> None:
        job = self.new_job()
        with self.assertRaises(ValidationError):
            self.store.record_evidence(job.job_id, "../escape", "x", {}, mocked=False)


if __name__ == "__main__":
    unittest.main()
