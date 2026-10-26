"""Interrupting a running job and recovering it (AC-08).

A real child process runs the pipeline and is killed while its provider subprocess works.
REAL: both processes, the provider's process tree, OS locks, leases, git, the worktree, the
checks. MOCKED: the model output (tests/helpers/fake_cli.py), so nothing is delivered.

Shown here: no duplicate provider while one runs; the provider dies with its orchestrator on
Windows (Job Object) and blocks recovery as an orphan on POSIX until stopped; uncommitted work
survives and is committed by the next round; a resume re-enters the interrupted phase without
rebuilding or re-reviewing what is done.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from typing import Any

from forgeflow.errors import DuplicateWorkerError
from forgeflow.jobs import JobState
from forgeflow.proc import pid_alive
from tests.pipeline_support import APPROVE, PipelineTestCase
from tests.support import PROJECT_ROOT, run_git

S = JobState
CHILD = PROJECT_ROOT / "tests" / "helpers" / "run_job_child.py"


def _wait(predicate: Any, seconds: float = 30) -> bool:
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.1)
    return True


class RecoveryTests(PipelineTestCase):
    def spawn(self, job_id: str, builder: list[str], reviewer: list[str]) -> subprocess.Popen[str]:
        child = subprocess.Popen(
            [sys.executable, str(CHILD), str(self.repo), job_id, builder[3], reviewer[3]],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(PROJECT_ROOT)),
        )

        def reap() -> None:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=30)

        self.addCleanup(reap)
        return child

    def provider_running(self, job_id: str, command: list[str]) -> int:
        """Wait until the job's lease records a provider PID that matches the fake CLI's log."""

        def started() -> bool:
            lease = self.store.load(job_id).lease
            starts = [r for r in self.fake_cli_log(command) if r["event"] == "start"]
            return bool(lease and lease.worker_pid and starts)

        self.assertTrue(_wait(started), "the provider never started")
        return int([r for r in self.fake_cli_log(command) if r["event"] == "start"][-1]["pid"])

    def kill_orchestrator(
        self, child: subprocess.Popen[str], provider_pid: int, job_id: str
    ) -> None:
        child.kill()
        child.wait(timeout=30)
        if sys.platform == "win32":
            # The provider was in the orchestrator's kill-on-close Job Object: it died with it.
            self.assertTrue(_wait(lambda: not pid_alive(provider_pid), 15), "provider survived")
        else:
            # On POSIX the provider has its own session and outlives the orchestrator; recovery
            # refuses until a human stops it (ADR 0003, POSIX containment).
            self.assertEqual(self.store.recover(job_id).outcome, "orphaned-worker")
            os.kill(provider_pid, 9)
            self.assertTrue(_wait(lambda: not pid_alive(provider_pid), 15))

    def test_a_killed_build_is_resumed_without_a_duplicate_and_without_losing_work(self) -> None:
        job = self.handed_off_job()
        partial = {"writes": {"partial.txt": "half done\n"}, "sleep": 120}
        hello = {"writes": {"greet.txt": "hello\n"}}
        factory, builder, reviewer = self.providers([partial, hello], {"review": APPROVE})
        child = self.spawn(job.job_id, builder, reviewer)
        provider_pid = self.provider_running(job.job_id, builder)

        # While it runs, a second run is refused and no second provider starts.
        with self.assertRaises(DuplicateWorkerError):
            self.run_pipeline(job.job_id, factory)
        self.assertEqual(len([r for r in self.fake_cli_log(builder) if r["event"] == "start"]), 1)
        self.assertEqual(self.store.recover(job.job_id).outcome, "running")

        self.kill_orchestrator(child, provider_pid, job.job_id)
        worktree = self.worktree(self.store.load(job.job_id))
        self.assertEqual((worktree / "partial.txt").read_text("utf-8"), "half done\n")

        job = self.run_pipeline(job.job_id, factory)  # recovers, then resumes the build
        self.assertEqual(job.attempts.resumes, 1)
        self.assertEqual(job.attempts.build, 2)
        starts = [r for r in self.fake_cli_log(builder) if r["event"] == "start"]
        self.assertEqual(len(starts), 2)
        self.assertIn("interrupted", starts[1]["prompt"])
        # The interrupted round's partial file is kept and committed with the resumed work.
        committed = run_git(worktree, "show", "--name-only", "--format=", "HEAD").split()
        self.assertEqual(sorted(committed), ["greet.txt", "partial.txt"])
        self.assertEqual(run_git(worktree, "status", "--porcelain"), "")
        # Checks ran once, after the resume, on the resumed round's commit.
        checks = [e for e in job.evidence if e.kind == "checks"]
        self.assertEqual([(c.status, c.build_round) for c in checks], [("passed", 2)])
        events = [e["event"] for e in self.store.events(job.job_id)]
        self.assertIn("recovered", events)
        self.assertEqual(job.state, S.NEEDS_HUMAN)  # simulated providers are never delivered

    def test_a_killed_review_resumes_in_review_without_rebuilding(self) -> None:
        job = self.handed_off_job()
        hello = {"writes": {"greet.txt": "hello\n"}}
        slow_review = {"review": APPROVE, "sleep": 120}
        factory, builder, reviewer = self.providers(hello, [slow_review, {"review": APPROVE}])
        child = self.spawn(job.job_id, builder, reviewer)
        provider_pid = self.provider_running(job.job_id, reviewer)
        self.assertEqual(self.store.load(job.job_id).state, S.REVIEWING)
        self.kill_orchestrator(child, provider_pid, job.job_id)

        recovered = self.store.recover(job.job_id)
        self.assertEqual(recovered.outcome, "recovered")
        job = self.store.load(job.job_id)
        self.assertEqual((job.state, job.interrupted_from), (S.INTERRUPTED, S.REVIEWING))
        job = self.run_pipeline(job.job_id, factory)
        self.assertEqual(len([r for r in self.fake_cli_log(builder) if r["event"] == "start"]), 1)
        self.assertEqual(len([r for r in self.fake_cli_log(reviewer) if r["event"] == "start"]), 2)
        self.assertEqual(job.attempts.build, 1)
        self.assertEqual([e.status for e in job.evidence if e.kind == "review"], ["approve"])

    def test_the_lease_records_the_provider_while_it_runs(self) -> None:
        job = self.handed_off_job()
        slow = {"writes": {"greet.txt": "hello\n"}, "sleep": 120}
        _, builder, reviewer = self.providers(slow, {"review": APPROVE})
        child = self.spawn(job.job_id, builder, reviewer)
        pid = self.provider_running(job.job_id, builder)
        lease = self.store.load(job.job_id).lease
        assert lease is not None
        self.assertEqual(lease.worker_pid, pid)  # the fake CLI is the direct child here
        self.assertTrue(pid_alive(pid))
        self.assertIsNotNone(lease.worker_started_at)
        self.assertEqual(lease.role, "builder")
        self.assertEqual(lease.holder_pid, child.pid)
        record = json.loads((self.store.job_dir(job.job_id) / "job.json").read_text("utf-8"))
        self.assertEqual(record["state"], "building")
        self.kill_orchestrator(child, pid, job.job_id)


if __name__ == "__main__":
    unittest.main()
