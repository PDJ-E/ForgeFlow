"""Child process for interruption tests: become the job's builder, start work, then hang.

Usage: python hold_lease.py <repo_root> <job_id> <provider> [--build]

Prints ``READY <pid>`` once it owns the job, then sleeps until killed. With ``--build`` it also
enters BUILDING and writes uncommitted work into the job's worktree first.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from forgeflow.jobs import JobState
from forgeflow.store import JobStore


def main() -> None:
    repo_root, job_id, provider = sys.argv[1], sys.argv[2], sys.argv[3]
    build = "--build" in sys.argv[4:]
    store = JobStore.for_repo(Path(repo_root), lock_timeout=5)
    lease = store.acquire_lease(job_id, "builder", provider)
    if build:
        job = store.transition(job_id, JobState.BUILDING, lease_token=lease.token)
        assert job.worktree is not None
        (Path(job.worktree.path) / "work-in-progress.txt").write_text(
            "uncommitted work from the interrupted builder\n", encoding="utf-8"
        )
    print(f"READY {os.getpid()}", flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
