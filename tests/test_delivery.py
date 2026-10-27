"""Delivery: one push, one draft PR, Gate B, and a merge recorded only from GitHub.

REAL: the commit (made by hand, no provider), the check commands (run by ``run_checks``), the
push (to a local bare repository standing in for ``origin``) and every store guard.
MOCKED: GitHub (FakeGitHubWorld, which reads PR heads from that bare repository) and the
review record, a hand-written fixture marked as such in its payload.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from forgeflow.board import BoardListener
from forgeflow.delivery import deliver
from forgeflow.dispatch import review_access_verified, run_reviewer, verified_approval
from forgeflow.errors import GitError, GitHubError, InvalidTransitionError
from forgeflow.jobs import Job, JobState
from forgeflow.pipeline import checks_summary
from forgeflow.providers import build_task
from forgeflow.repoconfig import load
from forgeflow.store import LeaseHandle
from forgeflow.worktree import WorktreeManager, prepare_job_worktree
from tests.fake_github import SimulatedCrash
from tests.pipeline_support import APPROVE, PipelineTestCase
from tests.support import run_git

S = JobState
VERIFIED: dict[str, Any] = {"version": 1, "verified": True, "method": "snapshot-token"}
# What the live run (2026-10-26) recorded: a well-formed approval with no proof of access.
LEGACY_SUMMARY = "additional file inspection was unavailable due to access restrictions"


class DeliveryCase(PipelineTestCase):
    """A handed-off job, its worktree prepared and the builder lease held by the test."""

    def setUp(self) -> None:
        super().setUp()
        job = self.handed_off_job()
        self.job_id = job.job_id
        self.store.assign_providers(self.job_id, "claude", "codex")
        prepare_job_worktree(self.store, WorktreeManager(self.repo), self.job_id)
        self.store.transition(self.job_id, S.READY)
        self.board = BoardListener(self.store, self.client, self.binding)
        self.store.listeners.append(self.board)
        self.lease = self.store.acquire_lease(self.job_id, "builder", "claude")
        self.addCleanup(self.lease.release)

    def reviewed(
        self,
        content: str = "hello\n",
        *,
        review: str = "approve",
        access: dict[str, Any] | None = VERIFIED,
    ) -> str:
        """A hand-made build round that passed the real checks and has a review fixture.

        ``access`` is the ``review_access`` record as ForgeFlow writes it after verifying a
        reviewer; ``None`` reproduces a review recorded before that check existed.
        """
        head = self.checked(content)
        payload: dict[str, Any] = {
            "verdict": review,
            "summary": "FIXTURE: written by the test",
            "findings": [],
        }
        if access is not None:
            payload["review_access"] = access
        self.store.record_evidence(
            self.job_id, "review", review, payload, mocked=False, commit=head
        )
        return head

    def checked(self, content: str = "hello\n") -> str:
        """A hand-made build round whose real checks passed; the job is left reviewing."""
        t = self.lease.token
        self.store.transition(self.job_id, S.BUILDING, lease_token=t)
        worktree = self.worktree(self.store.load(self.job_id))
        (worktree / "greet.txt").write_text(content, encoding="utf-8")
        run_git(worktree, "commit", "-qam", f"build {content.strip()}")
        head = run_git(worktree, "rev-parse", "HEAD")
        self.store.transition(self.job_id, S.VERIFYING, lease_token=t)
        config = load(self.repo, self.store.load(self.job_id).worktree.base_sha)  # type: ignore[union-attr]
        checks = self.store.run_checks(self.job_id, t, config.argv_list, names=config.names)
        self.assertEqual((checks.status, checks.mocked), ("passed", False))
        self.store.transition(self.job_id, S.REVIEWING, lease_token=t)
        return head

    def deliver(self, lease: LeaseHandle | None = None) -> Job:
        return deliver(self.store, lease or self.lease, self.client, sleep=lambda _: None).job


class DeliveryTests(DeliveryCase):
    def test_one_push_one_draft_pr_and_gate_b(self) -> None:
        head = self.reviewed()
        job = self.deliver()
        self.assertEqual(job.state, S.AWAITING_MERGE_APPROVAL)
        branch = f"forge/{self.job_id}"
        self.assertEqual(run_git(self.remote, "rev-parse", f"refs/heads/{branch}"), head)
        self.assertEqual(
            run_git(self.remote, "for-each-ref", "--format=%(refname)"), f"refs/heads/{branch}"
        )
        [pr] = self.world.pulls.values()
        self.assertTrue(pr["draft"])
        self.assertEqual((pr["head"], pr["base"]), (branch, "main"))
        body = pr["body"]
        for expected in (
            "Closes #1",
            f"commit `{head}`",
            "| greeting | passed | 0 |",
            "Gate B",
            f"forgeflow:trace job={self.job_id}",
            "FIXTURE",
        ):
            self.assertIn(expected, body)
        self.assertNotIn(str(self.repo), body, "local paths are never published")
        assert job.github.pull_request is not None
        self.assertEqual(job.github.pull_request.number, pr["number"])
        self.assertEqual(self.board_status(job), "Review")
        self.assertIsNone(job.lease)
        # Gate B is the human's: nothing in ForgeFlow merges.
        self.assertFalse(pr["merged"])
        self.assertNotIn("merge", " ".join(self.world.writes))

    def test_an_interrupted_delivery_reuses_its_pr(self) -> None:
        self.reviewed()
        self.world.crash_after = {"create_pr"}
        with self.assertRaises(SimulatedCrash):
            self.deliver()
        self.assertEqual(self.store.load(self.job_id).state, S.REVIEWING)
        job = self.deliver()
        self.assertEqual(job.state, S.AWAITING_MERGE_APPROVAL)
        self.assertEqual(self.world.writes.count("create_pr"), 1)
        self.assertEqual(len(self.world.pulls), 1)

    def test_a_simulated_round_is_never_pushed(self) -> None:
        self.reviewed()
        self.store.record_evidence(self.job_id, "provider-result", "succeeded", {}, mocked=True)
        with self.assertRaisesRegex(InvalidTransitionError, "mocked"):
            self.deliver()
        self.assertEqual(run_git(self.remote, "for-each-ref"), "")
        self.assertEqual(self.world.pulls, {})

    def test_delivery_needs_an_approving_review_of_the_validated_commit(self) -> None:
        self.reviewed(review="request-changes")
        with self.assertRaisesRegex(InvalidTransitionError, "approving review"):
            self.deliver()
        self.assertEqual(run_git(self.remote, "for-each-ref"), "")

    def test_stale_checks_from_an_earlier_round_do_not_deliver(self) -> None:
        self.reviewed()
        t = self.lease.token
        self.store.transition(self.job_id, S.BUILDING, lease_token=t)  # rework round, no checks
        self.store.transition(self.job_id, S.VERIFYING, lease_token=t)
        self.store.transition(self.job_id, S.REVIEWING, lease_token=t)
        with self.assertRaisesRegex(Exception, "no passing deterministic checks"):
            self.deliver()
        self.assertEqual(self.world.pulls, {})

    def test_a_diverged_remote_branch_is_never_force_pushed(self) -> None:
        self.reviewed()
        other = self.tmp / "other"
        run_git(self.tmp, "clone", "-q", str(self.remote), str(other))
        run_git(other, "checkout", "-q", "-b", f"forge/{self.job_id}")
        (other / "intruder.txt").write_text("someone else\n", encoding="utf-8")
        run_git(other, "add", "-A")
        run_git(other, "-c", "user.name=x", "-c", "user.email=x@x", "commit", "-qm", "theirs")
        run_git(other, "push", "-q", "origin", f"forge/{self.job_id}")
        theirs = run_git(other, "rev-parse", "HEAD")
        with self.assertRaisesRegex(GitError, "never force-pushes"):
            self.deliver()
        self.assertEqual(
            run_git(self.remote, "rev-parse", f"refs/heads/forge/{self.job_id}"), theirs
        )

    def test_a_closed_pr_for_the_branch_is_not_replaced(self) -> None:
        self.reviewed()
        self.deliver()
        self.world.pulls[next(iter(self.world.pulls))]["state"] = "closed"
        self.tamper(self.job_id, lambda d: d.update(state="reviewing", lease=None))
        self.lease.release()
        self.tamper(self.job_id, lambda d: d.update(state="ready"))
        lease = self.store.acquire_lease(self.job_id, "builder", "claude")
        self.addCleanup(lease.release)
        self.tamper(self.job_id, lambda d: d.update(state="reviewing"))
        with self.assertRaisesRegex(GitHubError, "closed"):
            self.deliver(lease)
        self.assertEqual(len(self.world.pulls), 1)

    def test_merge_is_recorded_only_when_github_reports_it(self) -> None:
        self.reviewed()
        job = self.deliver()
        assert job.github.pull_request is not None
        number = job.github.pull_request.number
        with self.assertRaisesRegex(InvalidTransitionError, "not merged"):
            self.store.record_merge(self.job_id, self.client)
        self.assertEqual(self.board_status(job), "Review")
        self.world.merge(number)  # the owner merges on GitHub
        job = self.store.record_merge(self.job_id, self.client)
        self.assertEqual(job.state, S.MERGED)
        self.assertEqual(self.board_status(job), "Done")
        self.assertEqual({i["state"] for i in self.world.issues.values()}, {"OPEN"})

    def test_rework_after_gate_b_redelivers_to_the_same_pr_as_a_draft(self) -> None:
        self.reviewed()
        job = self.deliver()
        number = job.github.pull_request.number  # type: ignore[union-attr]
        self.world.pulls[number]["draft"] = False  # the owner marked it ready for review
        self.lease.release()
        lease = self.store.acquire_lease(self.job_id, "builder", "claude")
        self.addCleanup(lease.release)
        self.lease = lease
        head = self.reviewed("hello again\n")
        job = self.deliver(lease)
        self.assertEqual(job.state, S.AWAITING_MERGE_APPROVAL)
        self.assertEqual(len(self.world.pulls), 1)
        self.assertTrue(self.world.pulls[number]["draft"])
        self.assertIn("convert_to_draft", self.world.writes)
        self.assertEqual(run_git(self.remote, "rev-parse", f"refs/heads/forge/{self.job_id}"), head)
        self.assertEqual(job.attempts.rework, 1)


class ApprovalProofTests(DeliveryCase):
    """An approval counts only with ForgeFlow's own record of verified snapshot access.

    Regression for approvals recorded before that check existed (the live run's diff-only
    Codex approval) and for reviewers that claim verification themselves. Resume tests use
    the fake CLI as reviewer, so their fresh reviews are mocked and never delivered.
    """

    def interrupt(self) -> None:
        """The orchestrator stops while reviewing: the job is interrupted in review."""
        self.lease.release()
        job = self.store.load(self.job_id)
        self.assertEqual((job.state, job.interrupted_from), (S.INTERRUPTED, S.REVIEWING))

    def github_writes(self) -> list[str]:
        return [w for w in self.world.writes if w in ("create_pr", "update_pr", "convert_to_draft")]

    def reviews(self) -> list[Any]:
        return [e for e in self.store.load(self.job_id).evidence if e.kind == "review"]

    def test_a_legacy_approval_is_never_delivered(self) -> None:
        for access in (None, {"version": 99, "verified": True}, {"version": 1, "verified": False}):
            with self.subTest(access):
                self.setUp()
                self.reviewed(access=access)
                with self.assertRaisesRegex(InvalidTransitionError, "no verified approving"):
                    self.deliver()
                self.assertEqual(self.store.load(self.job_id).state, S.REVIEWING)
                self.assertEqual(run_git(self.remote, "for-each-ref"), "", "nothing pushed")
                self.assertEqual((self.world.pulls, self.github_writes()), ({}, []))

    def test_resume_reviews_a_legacy_approval_again(self) -> None:
        self.reviewed(access=None)
        self.interrupt()
        factory, builder, reviewer = self.providers({"writes": {}}, {"review": APPROVE})
        job = self.run_pipeline(self.job_id, factory)
        self.assertIn("unverified; reviewing it again", " ".join(self.lines))
        self.assertEqual(len([r for r in self.fake_cli_log(reviewer) if r["event"] == "start"]), 1)
        self.assertEqual(self.fake_cli_log(builder), [], "a review problem is not rework")
        old, fresh = self.reviews()
        self.assertFalse(review_access_verified(self.store, job, old), "history is not rewritten")
        self.assertTrue(review_access_verified(self.store, job, fresh))
        self.assertEqual(job.attempts.rework, 0)
        self.assertEqual(job.state, S.NEEDS_HUMAN)  # the fresh review is simulated
        self.assertEqual(self.world.pulls, {})

    def test_a_failed_fresh_inspection_blocks_without_rework(self) -> None:
        self.reviewed(access=None)
        self.interrupt()
        unreadable = {"review": APPROVE, "cannot_read": True}
        factory, builder, _ = self.providers({"writes": {}}, unreadable)
        job = self.run_pipeline(self.job_id, factory)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        self.assertEqual([r.status for r in self.reviews()], ["approve", "blocked"])
        self.assertEqual((job.attempts.rework, job.attempts.build), (0, 1))
        self.assertEqual(self.fake_cli_log(builder), [])
        self.assertEqual(run_git(self.remote, "for-each-ref"), "")
        self.assertEqual((self.world.pulls, self.github_writes()), ({}, []))
        with self.assertRaisesRegex(InvalidTransitionError, "Gate B"):
            self.store.request_merge_approval(self.job_id, "x", self.client)

    def fresh_review(self, step: dict[str, Any]) -> tuple[Any, list[str]]:
        """Run ForgeFlow's own reviewer path (fake CLI) on the checked commit."""
        self.checked()
        factory, _, reviewer = self.providers({"writes": {}}, step)
        job = self.store.load(self.job_id)
        task = build_task(self.store, self.job_id, role="reviewer", acceptance_criteria=("ok",))
        _, evidence = run_reviewer(
            self.store,
            self.lease,
            factory("codex"),
            task,
            checks_summary=checks_summary(self.store, job),
        )
        return evidence, reviewer

    def test_a_verified_approval_is_reused_after_an_interruption(self) -> None:
        evidence, first_reviewer = self.fresh_review({"review": APPROVE})
        job = self.store.load(self.job_id)
        self.assertTrue(review_access_verified(self.store, job, evidence))
        self.interrupt()
        factory, builder, reviewer = self.providers({"writes": {}}, {"review": APPROVE})
        job = self.run_pipeline(self.job_id, factory)
        self.assertIn("verified approving review; not reviewing it again", " ".join(self.lines))
        self.assertEqual(self.fake_cli_log(reviewer), [], "not reviewed again")
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.fake_cli_log(builder), [])
        self.assertEqual(
            len([r for r in self.fake_cli_log(first_reviewer) if r["event"] == "start"]), 1
        )

    def test_a_reviewer_cannot_claim_verification_itself(self) -> None:
        claimed = {**APPROVE, "review_access": {"version": 1, "verified": True}}
        evidence, _ = self.fresh_review({"review": claimed, "cannot_read": True})
        job = self.store.load(self.job_id)
        self.assertEqual(evidence.status, "blocked")
        self.assertFalse(review_access_verified(self.store, job, evidence))
        record = json.loads((self.store.job_dir(self.job_id) / evidence.path).read_text("utf-8"))
        payload = record["payload"]
        self.assertEqual(payload["review"]["review_access"], {"version": 1, "verified": True})
        self.assertEqual(payload["review_access"]["verified"], False)  # ForgeFlow's own finding
        self.assertIsNone(verified_approval(self.store, job, evidence.commit))
        with self.assertRaises(InvalidTransitionError):  # mocked here, and unverified anyway
            self.deliver()
        self.assertEqual(run_git(self.remote, "for-each-ref"), "")
        self.assertEqual((self.world.pulls, self.github_writes()), ({}, []))


if __name__ == "__main__":
    unittest.main()
