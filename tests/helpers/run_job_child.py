"""Child process for recovery tests: run a job through the pipeline, with fake CLIs.

Usage: python run_job_child.py <repo_root> <job_id> <builder_script> <reviewer_script>

The builder is Claude and the reviewer Codex, both through tests/helpers/fake_cli.py, so their
results are mocked. The test kills this process while a provider runs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from forgeflow.pipeline import RunOptions, run_job
from forgeflow.providers import ClaudeCodeProvider, CodexProvider, Provider

FAKE_CLI = Path(__file__).with_name("fake_cli.py")


def main() -> None:
    repo, job_id, builder_script, reviewer_script = sys.argv[1:5]
    made: dict[str, Provider] = {
        "claude": ClaudeCodeProvider(
            command=[sys.executable, str(FAKE_CLI), "claude", builder_script]
        ),
        "codex": CodexProvider(command=[sys.executable, str(FAKE_CLI), "codex", reviewer_script]),
    }
    from forgeflow.store import JobStore

    store = JobStore.for_repo(Path(repo), lock_timeout=5)
    run_job(
        store,
        job_id,
        RunOptions(builder="claude", reviewer="codex"),
        providers=made.__getitem__,
        report=lambda line: print(line, flush=True),
    )


if __name__ == "__main__":
    main()
