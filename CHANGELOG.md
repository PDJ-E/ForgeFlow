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

- **Checkpoint A: execution harness** (pre-alpha, unreleased). This is a stdlib-only Python package (`forgeflow`, Python 3.10+) providing:
  - `forgeflow doctor`: read-only inspection of Python, Git, the repository, GitHub CLI authentication and Project scope, Claude Code and Codex CLI (version, auth, CLI options) and the Anthropic Knowledge Work plugins. It installs and changes nothing.
  - `forgeflow status [job-id]`: read-only view of local jobs, including worker liveness and recent events.
  - A durable job store under `.forge/runtime/` (self-gitignored), with atomic Windows-safe writes, a revisioned schema, an append-only audit log and evidence records flagged as mocked or real.
  - An explicit state machine with Gate A (approval bound to the plan's hash, re-checked at dispatch), Gate B (draft PR plus passing checks; `merged` only records a merge observed on GitHub), and bounded rework (default 2).
  - One execution slot, one job per issue and one OS-locked worker lease per job, with recovery that detects killed workers and orphaned provider processes without discarding work.
  - Isolated worktrees on `forge/<job-id>` branches that refuse to reuse or remove unknown, dirty or user-owned worktrees and never touch `main`.
  - A provider contract for Claude Code and Codex with real capability detection, plus a clearly labelled mock provider. **Real provider execution is not implemented.**
  - Read-only linkage of a job to an existing GitHub issue and its Project items, reporting missing token scopes as explicit blockers.
  - Repository-scoped `/forge` (Claude Code) and `$forge` (Codex) skills, limited to `doctor` and `status`.
  - [ADR 0003](docs/decisions/0003-runtime-state-and-worktrees.md), the [getting started](docs/getting-started.md) guide, a test suite (unit, cross-process interruption and mocked lifecycle tests) and CI on Windows and Ubuntu.

**No discovery flow, plan-approval UI, real Claude/Codex execution, issue/PR creation, dashboard or knowledge adapter has been implemented yet.**

## Maintenance policy

- **Unreleased** describes changes merged into `main` since the last release. A merged PR is **not** itself a product release.
- Add an entry to this file in the same PR as each notable user-facing change (new behavior, material fixes, breaking changes, significant security or configuration updates).
- Use the relevant categories: **Added**, **Changed**, **Deprecated**, **Removed**, **Fixed**, and **Security**. Omit empty categories.
- When a release is actually tagged, move its changes into a dated `[x.y.z] - YYYY-MM-DD` section. Do not invent a release version or date ahead of publication.
- Record meaningful outcomes rather than listing every internal commit. Link to the PR or Issue when it improves traceability.
- Do not include secrets, sensitive user information or private run logs.
