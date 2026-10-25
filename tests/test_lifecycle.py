"""Simulated end-to-end lifecycle with MockProvider.

MOCKED: provider execution, GitHub responses and the PR link.
REAL: git repository and worktree, job store, locks, leases, transitions, evidence files and the
deterministic check commands (run by ``run_checks``).

A simulated build can go through every phase but can never be delivered: Gate B refuses a
build round that contains mocked evidence. The delivery path itself is covered without a
provider in ``test_review_regressions.DeliveryEvidenceTests``.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from forgeflow.dispatch import run_builder
from forgeflow.errors import InvalidTransitionError, ProviderError
from forgeflow.github import GitHubClient
from forgeflow.jobs import JobState
from forgeflow.providers import ClaudeCodeProvider, MockProvider, build_task
from tests.support import FakeGitHub, GitRepoTestCase, pull_request, run_git

S = JobState
CRITERIA = ("greet() returns 'hello'",)
FAILING = ((sys.executable, "-c", "raise SystemExit(1)"),)
PASSING = ((sys.executable, "-c", "pass"),)


class SimulatedLifecycleTests(GitRepoTestCase):
    def commit_all(self, worktree: Path, message: str) -> str:
        run_git(worktree, "add", "-A")
        run_git(worktree, "commit", "-q", "-m", message)
        return run_git(worktree, "rev-parse", "HEAD")

    def test_simulated_build_runs_every_phase_but_is_never_delivered(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store,
            job.job_id,
            role="builder",
            acceptance_criteria=CRITERIA,
            allowed_paths=("src/*", "tests/*"),
            validation_commands=PASSING,
        )
        worktree = Path(task.worktree_path)

        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            first = MockProvider("mock", writes={"src/greet.py": "def greet():\n    return 1\n"})
            self.assertTrue(run_builder(self.store, lease, first, task).mocked)
            self.assertEqual(self.store.load(job.job_id).state, S.VERIFYING)
            self.commit_all(worktree, "first attempt")

            # Deterministic checks fail first: bounded rework round 1.
            self.assertEqual(
                self.store.run_checks(job.job_id, lease.token, FAILING).status, "failed"
            )
            fix = MockProvider(
                "mock", writes={"src/greet.py": "def greet():\n    return 'hello'\n"}
            )
            run_builder(self.store, lease, fix, task)
            head = self.commit_all(worktree, "fix")
            checks = self.store.run_checks(job.job_id, lease.token, PASSING)
            self.assertEqual((checks.status, checks.mocked, checks.commit), ("passed", False, head))
            self.store.transition(job.job_id, S.REVIEWING, lease_token=lease.token)

            reviewer = MockProvider("mock-reviewer", verdict="approve")
            review_task = build_task(
                self.store, job.job_id, role="reviewer", acceptance_criteria=CRITERIA
            )
            review = reviewer.execute(review_task, lease)
            self.store.record_evidence(
                job.job_id, "review", review.verdict or "none", review.to_dict(), mocked=True
            )
            self.link_draft_pr(job.job_id)
            github = GitHubClient(
                runner=FakeGitHub(
                    {}, pull_requests={42: pull_request(42, head_ref=task.branch, head_sha=head)}
                )
            )
            with self.assertRaisesRegex(InvalidTransitionError, "mocked evidence"):
                self.store.request_merge_approval(job.job_id, lease.token, github)
            self.store.transition(
                job.job_id, S.NEEDS_HUMAN, lease_token=lease.token, reason="simulation complete"
            )

        job = self.store.load(job.job_id)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        self.assertIsNone(job.lease)
        self.assertEqual((job.attempts.build, job.attempts.rework), (2, 1))
        self.assertEqual(
            [(e.kind, e.mocked) for e in job.evidence],
            [
                ("provider-result", True),
                ("checks", False),
                ("provider-result", True),
                ("checks", False),
                ("review", True),
            ],
        )
        self.assertFalse((self.repo / "src").exists(), "builder output must not reach main")
        events = [e["event"] for e in self.store.events(job.job_id)]
        self.assertEqual(events[0], "created")
        self.assertIn("transition:plan_approved", events)
        self.assertNotIn("transition:awaiting_merge_approval", events)
        self.assertEqual(run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_failed_build_escalates_to_a_human(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store,
            job.job_id,
            role="builder",
            acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            run_builder(self.store, lease, MockProvider("mock", status="failed"), task)
        self.assertEqual(self.store.load(job.job_id).state, S.NEEDS_HUMAN)

    def test_builder_writes_are_confined_to_allowed_paths(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store,
            job.job_id,
            role="builder",
            acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )
        rogue = MockProvider("mock", writes={".github/workflows/evil.yml": "x"})
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaises(ProviderError):
                run_builder(self.store, lease, rogue, task)
        job = self.store.load(job.job_id)
        self.assertEqual(job.state, S.NEEDS_HUMAN)
        assert job.worktree is not None
        self.assertFalse((Path(job.worktree.path) / ".github").exists())

    def test_only_the_assigned_builder_is_dispatched(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store,
            job.job_id,
            role="builder",
            acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaisesRegex(ProviderError, "not this job's builder"):
                run_builder(self.store, lease, MockProvider("claude"), task)
        self.assertEqual(self.store.load(job.job_id).state, S.READY)

    def test_real_providers_do_not_execute_yet(self) -> None:
        job = self.ready_job(builder="claude", reviewer="codex")
        task = build_task(
            self.store,
            job.job_id,
            role="builder",
            acceptance_criteria=CRITERIA,
            allowed_paths=("src/*",),
        )
        with self.store.acquire_lease(job.job_id, "builder", "claude") as lease:
            with self.assertRaisesRegex(ProviderError, "Checkpoint C"):
                run_builder(self.store, lease, ClaudeCodeProvider(), task)
        self.assertEqual(self.store.load(job.job_id).state, S.NEEDS_HUMAN)

    def test_reviewer_task_cannot_write(self) -> None:
        job = self.ready_job()
        task = build_task(self.store, job.job_id, role="reviewer", acceptance_criteria=CRITERIA)
        self.assertNotIn("write", task.allowed_operations)
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaises(ProviderError):
                MockProvider("mock-reviewer", writes={"src/x.py": "x"}).execute(task, lease)

    def test_draft_job_cannot_produce_a_task(self) -> None:
        job = self.new_job()
        with self.assertRaises(Exception):
            build_task(
                self.store,
                job.job_id,
                role="builder",
                acceptance_criteria=CRITERIA,
                allowed_paths=("*",),
            )
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.READY)


if __name__ == "__main__":
    unittest.main()
