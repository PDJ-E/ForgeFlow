"""Real cross-process interruption tests (AC-08 groundwork).

A child Python process becomes the job's builder, writes uncommitted work into the worktree and
is then killed. No mocks are involved in the ownership, locking or recovery path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any

from forgeflow.errors import DuplicateWorkerError, InvalidTransitionError, StaleLeaseError
from forgeflow.jobs import JobState
from forgeflow.proc import pid_alive
from tests.support import PROJECT_ROOT, GitRepoTestCase, run_git

HELPER = PROJECT_ROOT / "tests" / "helpers" / "hold_lease.py"


class InterruptionTests(GitRepoTestCase):
    def spawn_builder(self, job_id: str, *, build: bool = True) -> subprocess.Popen[str]:
        argv = [sys.executable, str(HELPER), str(self.repo), job_id, "mock"]
        if build:
            argv.append("--build")
        child = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT)),
        )
        self.addCleanup(self._reap, child)
        assert child.stdout is not None
        line = child.stdout.readline().strip()
        if not line.startswith("READY"):
            child.kill()
            _, err = child.communicate(timeout=10)
            self.fail(f"builder child did not start: {line!r} {err}")
        return child

    @staticmethod
    def _reap(child: subprocess.Popen[Any]) -> None:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)

    def kill(self, child: subprocess.Popen[str]) -> None:
        child.kill()
        child.wait(timeout=10)

    def test_live_worker_blocks_a_duplicate_and_survives_recovery_attempts(self) -> None:
        job = self.ready_job()
        self.spawn_builder(job.job_id)
        with self.assertRaises(DuplicateWorkerError):
            self.store.acquire_lease(job.job_id, "builder", "mock")
        self.assertEqual(self.store.recover(job.job_id).outcome, "running")
        self.assertEqual(self.store.load(job.job_id).state, JobState.BUILDING)

    def test_killed_worker_is_recovered_and_resumed_without_losing_work(self) -> None:
        job = self.ready_job()
        child = self.spawn_builder(job.job_id)
        self.kill(child)
        assert job.worktree is not None
        wip = Path(job.worktree.path) / "work-in-progress.txt"

        # Until recovery runs, the stale lease record blocks a new worker.
        with self.assertRaises(StaleLeaseError):
            self.store.acquire_lease(job.job_id, "builder", "mock")

        report = self.store.recover(job.job_id)
        self.assertEqual(report.outcome, "recovered", report.detail)
        job = self.store.load(job.job_id)
        self.assertEqual(
            (job.state, job.interrupted_from), (JobState.INTERRUPTED, JobState.BUILDING)
        )
        self.assertTrue(wip.exists(), "recovery must not discard the dirty worktree")

        with self.store.acquire_lease(job.job_id, "builder", "mock") as lease:
            # Resuming in a different phase than the one interrupted is refused.
            with self.assertRaises(InvalidTransitionError):
                self.store.transition(job.job_id, JobState.VERIFYING, lease_token=lease.token)
            job = self.store.transition(job.job_id, JobState.BUILDING, lease_token=lease.token)
            self.assertEqual(job.attempts.resumes, 1)
            self.assertEqual(job.attempts.build, 2)
            self.assertEqual(
                wip.read_text("utf-8"), "uncommitted work from the interrupted builder\n"
            )
            self.store.transition(
                job.job_id, JobState.NEEDS_HUMAN, lease_token=lease.token, reason="test complete"
            )
        self.assertEqual(run_git(self.repo, "branch", "--show-current"), "main")

    def test_orphaned_provider_process_blocks_recovery(self) -> None:
        job = self.ready_job()
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self._reap, orphan)
        lease = self.store.acquire_lease(job.job_id, "builder", "mock")
        self.store.transition(job.job_id, JobState.BUILDING, lease_token=lease.token)
        lease.set_worker_pid(orphan.pid)
        lease._os_lock.release()  # orchestrator dies; its provider child keeps running

        report = self.store.recover(job.job_id)
        self.assertEqual(report.outcome, "orphaned-worker")
        self.assertEqual(self.store.load(job.job_id).state, JobState.BUILDING)

        orphan.kill()
        orphan.wait(timeout=10)
        deadline = time.monotonic() + 5
        while pid_alive(orphan.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.store.recover(job.job_id).outcome, "recovered")
        self.assertEqual(self.store.load(job.job_id).state, JobState.INTERRUPTED)

    def test_status_reports_lost_worker_without_mutating(self) -> None:
        from forgeflow.cli import _lease_state

        job = self.ready_job()
        child = self.spawn_builder(job.job_id)
        self.assertIn("(running)", _lease_state(self.store.load(job.job_id)))
        self.kill(child)
        before = self.store.load(job.job_id)
        self.assertIn("recovery needed", _lease_state(before))
        self.assertEqual(self.store.load(job.job_id).revision, before.revision)


if __name__ == "__main__":
    unittest.main()
