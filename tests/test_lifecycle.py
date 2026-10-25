"""Simulated end-to-end lifecycle with MockProvider.

MOCKED: provider execution, deterministic-check results, GitHub responses and the PR link.
REAL: git repository and worktree, job store, locks, leases, transitions and evidence files.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from forgeflow.dispatch import run_builder
from forgeflow.errors import InvalidTransitionError, ProviderError
from forgeflow.jobs import JobState
from forgeflow.providers import ClaudeCodeProvider, MockProvider, build_task
from tests.support import GitRepoTestCase, run_git

S = JobState
CRITERIA = ("greet() returns 'hello'",)


class SimulatedLifecycleTests(GitRepoTestCase):
    def test_idea_to_merge_observation(self) -> None:
        job = self.ready_job()
        builder = MockProvider(
            "mock", writes={"src/greet.py": "def greet():\n    return 'hello'\n"}
        )
        task = build_task(
            self.store.load(job.job_id),
            role="builder",
            acceptance_criteria=CRITERIA,
            allowed_paths=("src/*", "tests/*"),
            validation_commands=(("python", "-m", "unittest"),),
        )

        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            result = run_builder(self.store, lease, builder, task)
            self.assertTrue(result.mocked)
            self.assertEqual(self.store.load(job.job_id).state, S.VERIFYING)

            # Deterministic checks fail first: bounded rework round 1.
            self.store.record_evidence(
                job.job_id, "checks", "failed", {"simulated": True}, mocked=True
            )
            self.store.transition(job.job_id, S.BUILDING, lease_token=lease.token)
            self.store.transition(job.job_id, S.VERIFYING, lease_token=lease.token)
            self.store.record_evidence(
                job.job_id, "checks", "passed", {"simulated": True}, mocked=True
            )
            self.store.transition(job.job_id, S.REVIEWING, lease_token=lease.token)

            reviewer = MockProvider("mock-reviewer", verdict="approve")
            review_task = build_task(
                self.store.load(job.job_id), role="reviewer", acceptance_criteria=CRITERIA
            )
            review = reviewer.execute(review_task, lease)
            self.store.record_evidence(
                job.job_id,
                "review",
                review.verdict or "none",
                review.to_dict(),
                mocked=review.mocked,
            )
            self.link_draft_pr(job.job_id)
            self.store.transition(job.job_id, S.AWAITING_MERGE_APPROVAL, lease_token=lease.token)

        job = self.store.load(job.job_id)
        self.assertEqual(job.state, S.AWAITING_MERGE_APPROVAL)
        self.assertIsNone(job.lease)
        self.assertEqual((job.attempts.build, job.attempts.rework), (2, 1))
        self.assertTrue(all(e.mocked for e in job.evidence))
        assert job.worktree is not None
        self.assertTrue((Path(job.worktree.path) / "src" / "greet.py").exists())
        self.assertFalse((self.repo / "src").exists(), "builder output must not reach main")

        job = self.store.transition(
            job.job_id,
            S.MERGED,
            merge_observation={
                "source": "github",
                "pr_number": 42,
                "merged_at": "2026-10-25T00:00:00Z",
            },
        )
        self.assertTrue(job.is_terminal)
        events = [e["event"] for e in self.store.events(job.job_id)]
        self.assertEqual(events[0], "created")
        self.assertIn("transition:plan_approved", events)
        self.assertEqual(events[-1], "transition:merged")
        self.assertEqual(run_git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")

    def test_failed_build_escalates_to_a_human(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store.load(job.job_id),
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
            self.store.load(job.job_id),
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
            self.store.load(job.job_id),
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
            self.store.load(job.job_id),
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
        task = build_task(
            self.store.load(job.job_id), role="reviewer", acceptance_criteria=CRITERIA
        )
        self.assertNotIn("write", task.allowed_operations)
        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            with self.assertRaises(ProviderError):
                MockProvider("mock-reviewer", writes={"src/x.py": "x"}).execute(task, lease)

    def test_draft_job_cannot_produce_a_task(self) -> None:
        job = self.new_job()
        with self.assertRaises(Exception):
            build_task(job, role="builder", acceptance_criteria=CRITERIA, allowed_paths=("*",))
        with self.assertRaises(InvalidTransitionError):
            self.store.transition(job.job_id, S.READY)


if __name__ == "__main__":
    unittest.main()
