# ADR 0003 — Runtime language, local job state, locking and worktrees

- **Status:** Accepted for Checkpoint A (implemented and tested; see [getting started](../getting-started.md))
- **Date:** 2026-10-24
- **Resolves:** [architecture](../architecture.md) open questions 3 (state schema, locking, resume) and 4 (Windows execution backend); partially 1, 2 and 6
- **Related:** [ADR 0001](0001-github-projects-local-state.md), [MVP scope](../mvp-scope.md)

## Context

ADR 0001 put technical execution state in local files but left the format, location and locking open. The runtime must work on Windows first, with no daemon, no database and no silent installation, and it must survive interruption without duplicating a worker or discarding work.

The pilot machine has Python 3.10.0. 3.10 reaches upstream end-of-life in October 2026, and there is no 3.11+ install yet. It also has Git 2.35, GitHub CLI 2.103, Claude Code 2.1.310 and Codex CLI 0.164.

## Decision

### Language and dependencies

**Python, standard library only, minimum 3.10.** Python offers mature subprocess control, `ctypes`/`msvcrt` for Windows primitives, JSON and `unittest` with no runtime dependencies. The alternatives were rejected for these reasons:

- **Node/TypeScript** would add a toolchain and a package tree for the same subprocess and file work.
- **PowerShell** is not portable to the Linux and macOS hosts the providers also support.
- **Go/Rust** add a build step that is premature for a pre-alpha.

The 3.10 floor matches the machine as found, and code avoids 3.11-only APIs (`tomllib`, `StrEnum`). CI tests 3.10 and 3.13; 3.12+ is recommended. Raising the floor is a one-line change once 3.10 support is no longer needed.

The console script is **`forgeflow`**, not `forge`. `forge` is already a common executable (Foundry), so the short names `/forge` and `$forge` are reserved for the skill adapters.

### Job state

- Location: `<main checkout>/.forge/runtime/jobs/<job-id>/`. The directory writes its own `.gitignore` containing `*`, so it is never committed even in repositories whose `.gitignore` does not know about ForgeFlow. The host repository's files are not edited.
- `job.json` holds a versioned (`schema_version: 1`), validated snapshot. Every write goes to a temporary file in the same directory, is fsynced and is swapped in with `os.replace`. On Windows the replace is retried with backoff when a reader, indexer or antivirus scanner holds the file open. It never falls back to a non-atomic write.
- `events.jsonl` is an append-only audit trail of transitions, approvals, leases and evidence. A torn final line from a crash is isolated, not propagated.
- `evidence/` holds machine-readable results. Each record carries `mocked: true|false`.
- Each record carries a `revision` counter, so writers holding stale data are rejected (compare-and-swap).

### Lifecycle and gates

States: `draft`, `awaiting_plan_approval`, `plan_approved`, `ready`, `building`, `verifying`, `reviewing`, `awaiting_merge_approval`, `interrupted`, `needs_human`, `merged`, `cancelled`. An explicit transition table plus per-target guards are the only way to change state. Generic updates cannot alter state, approvals, leases or attempt counters.

- **Gate A:** `plan_approved` requires a recorded approval whose hash matches the submitted plan file. Entering `ready` and every `building` dispatch re-hash the plan, so a plan edited after approval cannot be executed.
- **Gate B:** `awaiting_merge_approval` requires a linked *draft* PR and passing latest checks. `merged` only records a merge *observed on GitHub*. ForgeFlow has no merge operation.
- **Rework** is bounded by `attempts.max_rework` (default 2). Past the limit, the only path is `needs_human`.

### One job, one writer

- **Execution slot:** at most one job may be in `ready`, `building`, `verifying`, `reviewing`, `interrupted` or `needs_human`. The check fails closed if any job record is unreadable.
- **Issue ownership:** at most one non-terminal job per GitHub issue.
- **Worker lease:** a worker owns a job by holding an OS byte-range lock (`msvcrt.locking` / `fcntl.flock`) on `lease.lock` for as long as it works. The OS releases the lock if the process dies, so liveness does not depend on PIDs. The lease record in `job.json` names the holder and, once spawned, the provider subprocess PID.
- **Recovery** (`JobStore.recover`) refuses while the lease lock is held, while a recorded provider PID is still alive (an orphan that outlived its orchestrator), or when the lease came from another host. Otherwise the job moves to `interrupted`, remembers the phase, and leaves the worktree untouched. Resume must re-enter the same phase.
- A short-lived `state.lock` (also an OS lock) serialises every read-modify-write.

### Worktrees

- `.forge/worktrees/<job-id>` (self-ignored) on branch `forge/<job-id>`, created from the resolved commit of `origin/<base>` (fallback: local `<base>`), with no upstream tracking.
- Creation happens only after Gate A. Re-entry reuses a registered worktree on the expected branch, dirty or not.
- An existing non-empty directory, an existing branch, an unrecorded registered worktree, or a prunable entry is refused, with a message for a human. ForgeFlow never prunes, forces, resets, stashes or cleans.
- Removal uses `git worktree remove` without `--force`, only for clean worktrees ForgeFlow created. The branch is kept.

### Subprocesses on Windows

- No shell. Prompts are disabled (`GIT_TERMINAL_PROMPT=0`, `GH_PROMPT_DISABLED=1`, `GCM_INTERACTIVE=never`), and read-only git queries set `GIT_OPTIONAL_LOCKS=0`.
- Every command has a timeout. On timeout, the **process tree** is killed (`taskkill /T /F`; process groups on POSIX), because npm-installed CLIs are `.cmd` shims whose real work runs in a grandchild.
- PID liveness uses `OpenProcess`/`GetExitCodeProcess`. `os.kill(pid, 0)` is never used on Windows, where signal 0 is `CTRL_C_EVENT`.

### Skill entry points

Thin adapters at `.claude/skills/forge/SKILL.md` (Claude Code, `disable-model-invocation: true`) and `.agents/skills/forge/SKILL.md` (Codex) run only implemented CLI commands. Both were invoked successfully in the real clients on 2026-10-24; see [getting started](../getting-started.md#verified-invocation).

## Consequences

- Nothing to install beyond Python and Git. State is inspectable with any text editor, and `forgeflow status` is read-only.
- State lives with one clone on one machine. Cross-device continuity comes from GitHub (ADR 0001), not from synchronising `.forge/runtime`.
- `git clean -ffdx` in the main checkout can delete nested worktrees and runtime state (a single `-f` skips them). Moving worktrees outside the checkout stays an option if this proves to be a practical risk.
- **PID reuse:** an unrelated process that reuses a recorded provider PID makes recovery fail closed. `JobStore.force_release_lease` (audited, reason required) is the human override.
- **Gate A strength:** the approval record and hash binding are enforced in code. Capturing the approval through a channel an agent cannot drive (Checkpoint B) is not done yet, so the library API can still be called by any local process. This is local, single-user tooling, not a multi-user security boundary.
- If a crash lands between `git worktree add` and recording it, the next attempt refuses the unrecorded worktree, and a human must inspect it. ForgeFlow does not guess ownership.
