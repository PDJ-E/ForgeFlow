# ADR 0003 — Runtime language, local job state, locking and worktrees

- **Status:** Accepted for Checkpoint A (implemented and tested; see [getting started](../getting-started.md))
- **Date:** 2026-10-24 (amended 2026-10-25 after the independent review of PR #3; see [amendments](#amendments))
- **Resolves:** [architecture](../architecture.md) open questions 3 (state schema, locking, resume) and 4 (Windows execution backend); partially 1, 2 and 6
- **Related:** [ADR 0001](0001-github-projects-local-state.md), [MVP scope](../mvp-scope.md)

## Context

ADR 0001 put technical execution state in local files but left the format, location and locking open. The runtime must work on Windows first, with no daemon, no database and no silent installation, and it must survive interruption without duplicating a worker or discarding work.

When this ADR was first written (2026-10-24), the pilot machine had only Python 3.10.0, which reaches upstream end-of-life in October 2026. It also has Git 2.35, GitHub CLI 2.103, Claude Code 2.1.310 and Codex CLI 0.164. On 2026-10-25 Python 3.13.17 was installed alongside 3.10; `py -3.13` selects it, while a bare `python` still resolves to 3.10 because of `PATH` order.

## Decision

### Language and dependencies

**Python, standard library only, minimum 3.13.** Python offers mature subprocess control, `ctypes`/`msvcrt` for Windows primitives, JSON and `unittest` with no runtime dependencies. The alternatives were rejected for these reasons:

- **Node/TypeScript** would add a toolchain and a package tree for the same subprocess and file work.
- **PowerShell** is not portable to the Linux and macOS hosts the providers also support.
- **Go/Rust** add a build step that is premature for a pre-alpha.

The floor was first set to 3.10 to match the machine as found. It was raised to 3.13 once 3.13 was installed, because 3.10 is end-of-life this month and a current floor allows the standard library's newer APIs (`StrEnum`, `tomllib`, `shutil.rmtree(onexc=)`). CI tests 3.13 (the floor) and 3.15 (the newest stable release) on Windows and Ubuntu. `forgeflow/__init__.py` refuses an older interpreter with a message that names it and the `py -3.13` command. Tests keep `requires-python`, Ruff, mypy, CI and the runtime check in agreement. No virtual environment is required: the interpreter is chosen explicitly (`py -3.13` on Windows).

The console script is **`forgeflow`**, not `forge`. `forge` is already a common executable (Foundry), so the short names `/forge` and `$forge` are reserved for the skill adapters.

### Job state

- Location: `<main checkout>/.forge/runtime/jobs/<job-id>/`. The directory writes its own `.gitignore` containing `*`, so it is never committed even in repositories whose `.gitignore` does not know about ForgeFlow. The host repository's files are not edited.
- `job.json` holds a versioned (`schema_version: 2` since Checkpoint B, which still reads version 1), validated snapshot. Every write goes to a temporary file in the same directory, is fsynced and is swapped in with `os.replace`. On Windows the replace is retried with backoff when a reader, indexer or antivirus scanner holds the file open. It never falls back to a non-atomic write.
- `events.jsonl` is an append-only audit trail of transitions, approvals, leases and evidence. A torn final line from a crash is isolated, not propagated.
- `evidence/` holds machine-readable results. Each record carries `mocked: true|false`.
- Each record carries a `revision` counter, so writers holding stale data are rejected (compare-and-swap).

### Lifecycle and gates

States: `draft`, `awaiting_plan_approval`, `plan_approved`, `ready`, `building`, `verifying`, `reviewing`, `awaiting_merge_approval`, `interrupted`, `needs_human`, `merged`, `cancelled`. An explicit transition table plus per-target guards are the only way to change state. Generic updates cannot alter state, approvals, leases, attempt counters, the repository or the worktree. The worktree is recorded once, only at its own location (`record_worktree`).

- **Gate A:** `plan_approved` requires a recorded approval whose hash matches the submitted plan file. Submission stores a byte-exact snapshot (`plans/v<n>.md` since Checkpoint B, `plans/<sha256>.md` before; the hash is kept in `job.json` and re-checked on every read; UTF-8 required), and the builder's task carries exactly that text. It never carries a path to re-read from some checkout. Entering `ready` and every `building` dispatch re-hash both the plan file and the snapshot, so a plan edited after approval cannot be executed.
- **Gate B:** `awaiting_merge_approval` is reached only through `request_merge_approval`. The latest checks must have been run by `run_checks` (ForgeFlow executes the commands itself in the verified worktree, on a clean commit) in the *current* build round, and must have passed. No evidence in that round may be mocked. GitHub must report the linked PR as an open draft from the job's branch whose head is exactly the validated commit. `merged` is reached only through `record_merge`, which requires GitHub to report that PR merged with that same head commit. The store accepts no caller-supplied claim for either state. ForgeFlow has no merge operation.
- **Rework** is bounded by `attempts.max_rework` (default 2). Past the limit, the only path is `needs_human`.

### One job, one writer

- **Execution slot:** at most one job may be in `ready`, `building`, `verifying`, `reviewing`, `interrupted` or `needs_human`. The check fails closed if any job record is unreadable.
- **Issue ownership:** at most one non-terminal job per GitHub issue.
- **Worker lease:** a worker owns a job by holding an OS byte-range lock (`msvcrt.locking` / `fcntl.flock`) on `lease.lock` for as long as it works. The OS releases the lock if the process dies, so liveness does not depend on PIDs. The lease record in `job.json` names the holder and, once spawned, the provider subprocess PID **and its OS start time**, so a reused PID is told apart from the original process. While that subprocess still runs, the lease record is never cleared: releasing the handle keeps it, leaving `building` is refused, and so is entering a state that ends the lease. No second builder can start next to it.
- **Recovery** (`JobStore.recover`) refuses while the lease lock is held, while the recorded provider process is still alive or a live process with its PID cannot be matched by start time (an orphan that outlived its orchestrator), or when the lease came from another host. Otherwise the job moves to `interrupted`, remembers the phase, and leaves the worktree untouched. Resume must re-enter the same phase.
- A short-lived `state.lock` (also an OS lock) serialises every read-modify-write.

### Worktrees

- `.forge/worktrees/<job-id>` (self-ignored) on branch `forge/<job-id>`, created from the resolved commit of `origin/<base>` (fallback: local `<base>`), with no upstream tracking.
- Creation happens only after Gate A. Re-entry reuses a registered worktree on the expected branch, dirty or not.
- An existing non-empty directory, an existing branch, an unrecorded registered worktree, or a prunable entry is refused, with a message for a human. ForgeFlow never prunes, forces, resets, stashes or cleans.
- Removal uses `git worktree remove` without `--force`, only for clean worktrees ForgeFlow created **that contain no ignored files**. Git deletes ignored files such as `.env` without asking, so their presence is a refusal for a human to resolve. The branch is kept.
- Before a builder is dispatched or checks run, `verify_job_worktree` derives the expected path from the repository root and job id and confirms with git that it is a registered worktree on `forge/<job-id>` whose top level is that path. A record pointing at the main checkout or another worktree is refused.

### Subprocesses on Windows

- No shell. Prompts are disabled (`GIT_TERMINAL_PROMPT=0`, `GH_PROMPT_DISABLED=1`, `GCM_INTERACTIVE=never`), and read-only git queries set `GIT_OPTIONAL_LOCKS=0`.
- Every command has a timeout and runs in a container that ends with it. On Windows, the child is created suspended, assigned to a Job Object with `KILL_ON_JOB_CLOSE`, then resumed. A timeout terminates the whole job, which includes descendants whose parent already exited (`taskkill /T` cannot find those). Closing the job after a normal exit removes leftover descendants. On POSIX, the command's process group is killed in both cases. After a kill, output is drained for at most 5 seconds, so `run_command` returns in about `timeout + 10 s` even if something escaped and still holds the pipes. npm-installed CLIs are `.cmd` shims whose real work runs in a grandchild, which is why the whole tree matters.
- PID liveness uses `OpenProcess`/`GetExitCodeProcess`. `os.kill(pid, 0)` is never used on Windows, where signal 0 is `CTRL_C_EVENT`.

### Skill entry points

Thin adapters at `.claude/skills/forge/SKILL.md` (Claude Code, `disable-model-invocation: true`) and `.agents/skills/forge/SKILL.md` (Codex) run only implemented CLI commands. Both were invoked successfully in the real clients on 2026-10-24; see [getting started](../getting-started.md#verified-invocation).

## Consequences

- Nothing to install beyond Python and Git. State is inspectable with any text editor, and `forgeflow status` is read-only.
- State lives with one clone on one machine. Cross-device continuity comes from GitHub (ADR 0001), not from synchronising `.forge/runtime`.
- `git clean -ffdx` in the main checkout can delete nested worktrees and runtime state (a single `-f` skips them). Moving worktrees outside the checkout stays an option if this proves to be a practical risk.
- **PID reuse:** a recorded provider PID now carries its start time, so a reused PID is recognised and does not block recovery. When the start time cannot be read (macOS, or a process the user may not query), a live PID fails closed. `JobStore.force_release_lease` (audited, reason required) is the human override, and it refuses while the recorded process is verifiably still running.
- **POSIX containment:** a descendant that calls `setsid()` leaves the process group and is not killed. `run_command` still returns on time, but that process can survive. Windows Job Objects do not have this gap, because nested processes cannot break away unless the job allows it. Closing it on Linux would need cgroups.
- **Gate A strength:** the approval record and hash binding are enforced in code. Since Checkpoint B, `transition()` refuses `plan_approved`; approvals are captured only by `forgeflow approve` from a human at an interactive console, and only that method is accepted at later boundaries. The threat model and its limits are in [ADR 0004](0004-planning-workflow-and-gate-a.md). This is local, single-user tooling, not a multi-user security boundary.
- If a crash lands between `git worktree add` and recording it, the next attempt refuses the unrecorded worktree, and a human must inspect it. ForgeFlow does not guess ownership.

## Amendments

**2026-10-25: independent review of PR #3.** Eight reproducible defects were fixed before merge. Each has a regression test in `tests/test_review_regressions.py`, and each was shown to fail against the previous code.

1. A generic `update()` could rewrite `job.worktree` or `job.repo`, sending builder writes to the main checkout. Both fields are now protected, and dispatch re-verifies the worktree against git.
2. Releasing a lease, or escalating to `needs_human`, cleared the lease while the provider subprocess ran, so a second builder could start. The lease is now kept while the process runs, identified by PID and start time.
3. `git worktree remove` silently deleted ignored files (`.env`, local data). Removal now refuses when ignored files exist.
4. Gate B accepted mocked or stale check results, and `merged` accepted a caller-supplied dict. Both delivery states now require verification by ForgeFlow and GitHub, as described under Gate B.
5. The builder got a plan path that resolved in a different checkout than the approved one. Builders now get the approved bytes from a snapshot.
6. On Windows, a timeout could wait indefinitely for a descendant whose parent had exited. Process trees are now contained in Job Objects, with a bounded drain.
7. `src/*` authorised `src/../.github/...`. Paths with `.`/`..`/empty segments, `.git`, drives or streams are now refused, and the resolved path (after symlinks and junctions) must match too.
8. Remote URLs with embedded credentials were persisted and shown by `doctor`. User info is now stripped on discovery, when loading records, and in command diagnostics.

The minimum Python version was raised from 3.10 to 3.13 at the same time (see *Language and dependencies*).
