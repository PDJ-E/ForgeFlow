"""Delivery: one push, one draft PR, Gate B, and a merge recorded only from GitHub.

REAL: the commit (made by hand, no provider), the check commands (run by ``run_checks``), the
push (to a local bare repository standing in for ``origin``) and every store guard.
MOCKED: GitHub (FakeGitHubWorld, which reads PR heads from that bare repository) and the
review record, a hand-written fixture marked as such in its payload.
"""

from __future__ import annotations

import unittest

from forgeflow.board import BoardListener
from forgeflow.delivery import deliver
from forgeflow.errors import GitError, GitHubError, InvalidTransitionError
from forgeflow.jobs import Job, JobState
from forgeflow.repoconfig import load
from forgeflow.store import LeaseHandle
from forgeflow.worktree import WorktreeManager, prepare_job_worktree
from tests.fake_github import SimulatedCrash
from tests.pipeline_support import PipelineTestCase
from tests.support import run_git

S = JobState


class DeliveryTests(PipelineTestCase):
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

    def reviewed(self, content: str = "hello\n", *, review: str = "approve") -> str:
        """A hand-made build round that passed the real checks and has a review fixture."""
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
        self.store.record_evidence(
            self.job_id,
            "review",
            review,
            {"verdict": review, "summary": "FIXTURE: written by the test", "findings": []},
            mocked=False,
            commit=head,
        )
        return head

    def deliver(self, lease: LeaseHandle | None = None) -> Job:
        return deliver(self.store, lease or self.lease, self.client, sleep=lambda _: None).job

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


if __name__ == "__main__":
    unittest.main()
