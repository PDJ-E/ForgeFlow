# Changelog

All notable changes to **ForgeFlow** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and published releases will follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) when applicable.

> **Project status:** design / pre-alpha. No runnable ForgeFlow release or version tag has been published. `v0.1` currently refers to the **planned MVP scope**, not a released product.

## [Unreleased]

### Added

- Established the open-source ForgeFlow repository and MIT license.
- Published the initial project vision, design principles and architecture documentation.
- Defined [ADR 0001](docs/decisions/0001-github-projects-local-state.md): GitHub Projects as the board for approved work, with local state for agent execution.
- Defined [ADR 0002](docs/decisions/0002-knowledge-sources-obsidian.md): optional, project-scoped Markdown knowledge and Obsidian MCP integration with approved writes.
- Documented the first [MVP v0.1 scope](docs/mvp-scope.md), observable acceptance criteria and [roadmap](docs/roadmap.md).
- Documented upstream inspirations, licenses and reuse boundaries in [ATTRIBUTION.md](ATTRIBUTION.md).

All the above documentation was introduced as the project's initial foundation in [PR #1](https://github.com/PDJ-E/ForgeFlow/pull/1).

- **Checkpoint A: execution harness** (pre-alpha, unreleased). This is a stdlib-only Python package (`forgeflow`, Python 3.13+) providing:
  - `forgeflow doctor`: read-only inspection of Python, Git, the repository, GitHub CLI authentication and Project scope, Claude Code and Codex CLI (version, auth, CLI options) and the Anthropic Knowledge Work plugins. It installs and changes nothing.
  - `forgeflow status [job-id]`: read-only view of local jobs, including worker liveness and recent events.
  - A durable job store under `.forge/runtime/` (self-gitignored), with atomic Windows-safe writes, a revisioned schema, an append-only audit log and evidence records flagged as mocked or real.
  - An explicit state machine with Gate A (approval bound to the plan's hash and a byte-exact plan snapshot, re-checked at dispatch), Gate B (a draft PR whose head on GitHub is the commit ForgeFlow's own check run validated; `merged` only records a merge GitHub reports for that commit), and bounded rework (default 2).
  - One execution slot, one job per issue and one OS-locked worker lease per job, with recovery that detects killed workers and orphaned provider processes without discarding work.
  - Isolated worktrees on `forge/<job-id>` branches that refuse to reuse or remove unknown, dirty or user-owned worktrees and never touch `main`.
  - A provider contract for Claude Code and Codex with real capability detection, plus a clearly labelled mock provider. **Real provider execution is not implemented.**
  - Read-only linkage of a job to an existing GitHub issue and its Project items, reporting missing token scopes as explicit blockers.
  - Repository-scoped `/forge` (Claude Code) and `$forge` (Codex) skills, limited to `doctor` and `status`.
  - [ADR 0003](docs/decisions/0003-runtime-state-and-worktrees.md), the [getting started](docs/getting-started.md) guide, a test suite (unit, cross-process interruption and mocked lifecycle tests) and CI on Windows and Ubuntu.

- **Checkpoint B: discovery, design and plan approval** (pre-alpha, unreleased):
  - `forgeflow idea` and `/forge idea` / `$forge idea`: interactive discovery whose outcomes (requirements, decisions, rejected options, questions, risks...) are kept with a status history in a private `discovery.json`, can be refined, rejected or reinstated, and resume across sessions.
  - `forgeflow design` and `/forge design`: one Markdown plan per job, prefilled from the discovery, validated (`design check`), and pinned as numbered, hashed versions (`design submit`, `design revise`).
  - `forgeflow approve` (Gate A): shows the exact plan version, its sha256 and what approval does and does not authorise, and records the approval only after the human types a one-time code at an interactive console. It is bound to the version shown and refused if the plan changed.
  - `forgeflow handoff`: publishes the approved plan as one GitHub issue on the bound Project in its Ready status, with a trace marker; idempotent across retries and interruptions, never overrides a status set on the board, and never publishes discovery notes.
  - `forgeflow init` binds a GitHub Project after checking its Status field; `forgeflow skills` discovers the Product Management and Engineering skills each host can use.
  - [ADR 0004](docs/decisions/0004-planning-workflow-and-gate-a.md), with the Gate A threat model and verification in the real Claude Code and Codex hosts.

**No real Claude/Codex builder execution, branch or PR creation, dashboard or knowledge adapter has been implemented yet.**

### Changed

- **Python 3.13 is now the minimum supported version** (previously 3.10, chosen when the pilot machine had only 3.10). `requires-python`, Ruff (`py313`), mypy (`3.13`), `forgeflow doctor` and a runtime check in `forgeflow/__init__.py` agree, and a test keeps them in agreement. Older interpreters get a message naming the interpreter and the `py -3.13` command. CI now tests Python 3.13 and 3.15 on Windows and Ubuntu, and a `CI OK` job aggregates the results for branch protection.
- Developer tools (Ruff, mypy, optional pytest) are pinned in `pyproject.toml` dependency groups (`lint`, `dev`) and are never runtime dependencies. No virtual environment is required. The docs name the interpreter explicitly (`py -3.13` on Windows) because a bare `python` may resolve to an older install.
- GitHub Project access is verified. With `read:project` granted, `doctor` reports the linked Project (#2 ForgeFlow Development) instead of the earlier scope blocker.
- The job record is schema version 2 (plan version, approved version, handoff record). Version 1 records still load and are upgraded on their next write.
- Linking a GitHub issue to a job now requires an approved plan; drafts stay discussion-only.
- Jobs started by discovery have no builder (`unassigned`), and no worker lease can be taken for them until a builder is assigned (Checkpoint C).
- The `/forge` and `$forge` adapters cover the planning workflow, share one command table (checked by a test) and never run `approve`.

### Security

- Gate A can no longer be passed through the library: `transition()` refuses `plan_approved`, and only approvals captured by `forgeflow approve` at an interactive console (Windows `GetConsoleMode` on both stdin and stdout, which also rejects `NUL`) are accepted, then re-checked at handoff, `ready` and every dispatch. Agent tool calls in Claude Code and Codex were verified to be refused. The limits of this local protection are documented in ADR 0004.

Fixes for eight defects found by an independent review of [PR #3](https://github.com/PDJ-E/ForgeFlow/pull/3), each covered by a regression test that failed against the previous code:

- Job updates can no longer redirect the worktree or repository. Dispatch and check runs verify with git that they target the job's own registered worktree, never the main checkout or another worktree.
- A worker lease is no longer released while its provider subprocess still runs, so no second builder can start next to it. The provider is identified by PID and start time, so a reused PID is no longer mistaken for it.
- Removing a worktree now refuses when it contains ignored files such as `.env`, which `git worktree remove` would delete silently.
- Delivery states require verified evidence. Check results come only from `run_checks`, which runs the commands itself on a clean commit in the current build round. Mocked builds cannot reach Gate B, and a merge is recorded only when GitHub reports it for the validated commit.
- Builders receive the exact approved plan bytes from a snapshot, not a path that could resolve to a different file.
- On Windows, command timeouts are bounded and descendants are cleaned up even after their parent exits (Job Objects). Output draining after a kill is capped.
- Allowed path patterns can no longer be escaped with `..`, `.git`, drive or stream syntax, or symlinks and junctions.
- Credentials embedded in remote URLs are no longer stored in job records or shown in `doctor` output and command diagnostics.

## Maintenance policy

- **Unreleased** describes changes merged into `main` since the last release. A merged PR is **not** itself a product release.
- Add an entry to this file in the same PR as each notable user-facing change (new behavior, material fixes, breaking changes, significant security or configuration updates).
- Use the relevant categories: **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, and **Security**. Omit empty categories.
- When a release is actually tagged, move its changes into a dated `[x.y.z] - YYYY-MM-DD` section. Do not invent a release version or date ahead of publication.
- Record meaningful outcomes rather than listing every internal commit. Link to the PR or Issue when it improves traceability.
- Do not include secrets, sensitive user information or private run logs.
