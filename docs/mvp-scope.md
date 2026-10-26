# ForgeFlow MVP v0.1 — scope and acceptance criteria

**Status:** approved scope, **partly built**. Checkpoints A (execution harness) and B (discovery, design, Gate A, GitHub handoff) are merged; C (build, review, PR) is implemented and in review; D (knowledge) and E (acceptance demonstration) are not started. AC-04 to AC-08 and AC-12 are covered by Checkpoint C's tests and its live demonstration ([ADR 0005](decisions/0005-build-review-and-delivery.md#verification)); the formal end-to-end acceptance run is Checkpoint E. See the [roadmap](roadmap.md).  
**Date:** 2026-10-24 (status updated 2026-10-26).  
**Goal:** one safe, observable, repeatable end-to-end workflow from an idea to a **human-reviewed PR**, plus a small optional knowledge bridge.

This document is the intended scope boundary for ForgeFlow's **first complete implementation assignment**, not a prompt. Delivery can use incremental internal checkpoints and multiple PRs; no single agent execution is assumed to complete all features reliably. Claims of readiness must be backed by a tested demonstration.

## Mandatory capabilities for v0.1

### 1. Portable entry point and setup

- Provide a short **`/forge`** experience in Claude Code and a conceptually equivalent **`$forge`** skill in Codex, subject to verifying actual host installation/invocation support. Provide the technically correct fallback invocation if plugin namespaces require one.
- An `init` / setup flow checks Git, repository state, GitHub CLI credentials, available Claude/Codex CLIs and permitted tools; reports missing optional components rather than installing them silently.
- Work from Windows/PowerShell without requiring a Linux-only setup path. Prefer a modest cross-platform runtime, avoiding persistent server infrastructure.
- Preserve existing git, agent, skill and MCP configurations.

### 2. Interactive product discovery and design

- `idea` / `design` represent interactive processes, not single-shot feature generators.
- Where actually available, reuse Anthropic Product Management and Engineering capabilities; do not copy/duplicate their plugin assets.
- Create concise persistent artifacts: idea/discovery notes, an approved scope, acceptance criteria, architecture tradeoffs / ADR if warranted and an actionable plan.
- **Gate A:** no code writing and no issue moved to a runnable Ready state before explicit human approval of the plan.

### 3. GitHub work tracking

- GitHub Issues contain approved, verifiable implementation work; GitHub Projects is the work-management board and authoritative state for approved tasks.
- Support the minimum mapping between local approved plan, issue/project item, run ID, branch and PR. No second local Kanban dashboard.
- During setup, explicitly discover and link the user's Project; handle unavailable permissions/connectivity by showing a blocker rather than silently inventing an alternative source of truth.
- Avoid automatically posting private discovery notes or vault content to public Issues.

### 4. One-job implementation, validation, and delivery

- One active job by default; one writable builder in an isolated Git worktree/branch.
- Permit explicit selection of **Claude Code or Codex CLI** as builder. Detect installed executable and access; no hardcoded assumption about model IDs, quota or availability.
- Execute a small set of repository-configured deterministic quality checks before LLM review and retain machine-readable status/evidence.
- Perform a relevant reviewer pass, optionally by the *other* provider when available, with read-only review permissions where supported. If the requested reviewer is unavailable, request a new authorized choice rather than silently approving.
- Bound correction cycles (default: maximum 2) and return to human on persistent failure.
- Produce a draft PR containing approved scope, change summary, tests, reviewer observations and limitations.
- **Gate B:** never automatically merge or deploy. No direct/force pushes to a protected base branch, no CI bypass.
- Runtime resumes safely after interruption, without duplicating a running worker or discarding a dirty worktree; tests must cover at least one recovery case.

### 5. Local operational record

- Minimal file-based, atomic durable state: job ID, phase, approved spec reference, provider/role, timestamps, worktree/branch, issue/PR reference, checks/review outcomes, retry count and resumability.
- Local logs and sensitive evidence are gitignored. Provide `status` or equivalent read-only report.
- Capture token counts and estimated cost **only when reliably exposed** by a provider; differentiate missing data from zero usage and estimates from actual charges.
- No local dashboard in v0.1.

### 6. Optional Knowledge Management bridge

- Work normally when Obsidian, MCP or a vault is absent.
- Associate one project with one explicit local Markdown root. Restrict read/write access to the selected root. Retrieve a relevant note by name/text and return its source/path with basic freshness information.
- Support a **reviewable documentation change proposal** (before/after or unified diff), explicit user approval, a version/hash freshness check, recoverable original and rejection of path escapes or concurrent modification.
- Verify read-only use of an **already installed** MCP Obsidian adapter when available; no automatic MCP installation or global credential changes.
- The current sandbox uses Local REST API with MCP v5.5.2 and `MarkusPfundstein/mcp-obsidian`; Claude Code and Codex have already performed dummy-note operations through this setup. This is a starting capability demonstration, not authorization for autonomous vault writes.
- Native MCP in the Obsidian plugin may be assessed later; adopting it is **not** a blocker for v0.1.

## Not in v0.1

- Bespoke web dashboard / duplicated local Kanban.
- Multi-job parallel waves, multi-host distributed orchestration or self-hosted control server.
- Fully automated provider selection/optimization or unbounded specialist agent chains.
- Automatic merges or production deployment.
- Self-modifying skills and unattended global configuration changes.
- Comprehensive RAG, embeddings, a vector database, multi-vault synchronization, automatic vault-wide documentation writes or a documentation mirror.
- Deep Sentry/PostHog monitoring and automatic bug filing; record lessons manually or as proposals until the core pipeline has evidence.

## Acceptance demonstration

The first release candidate must pass a supervised end-to-end demonstration in a **throwaway repository and test vault**:

| ID | Observable acceptance criterion |
| --- | --- |
| AC-01 | Setup reports available providers, required git/GitHub capabilities, and optional knowledge sources without overwriting global settings. |
| AC-02 | An exploratory idea remains discussion-only and cannot dispatch code. |
| AC-03 | An approved plan produces a traceable actionable GitHub Issue/Project item. |
| AC-04 | Exactly one builder is assigned to an isolated worktree; implementation does not touch main directly. |
| AC-05 | Configured tests/checks execute and their results are recorded; failure blocks approval. |
| AC-06 | A reviewer can reject a verifiable issue and trigger bounded rework; reviewer evidence appears in the job/PR record. |
| AC-07 | A draft PR is created and cannot merge without the owner’s explicit approval. |
| AC-08 | Simulated interruption and retry preserve work and do not duplicate agents. |
| AC-09 | A Markdown note is found with source information; a proposed edit is shown but not written until approved. |
| AC-10 | A stale file or an out-of-scope target is rejected safely, with the original content preserved. |
| AC-11 | Disabling the Obsidian MCP server does not prevent local Markdown functionality or regular non-knowledge tasks. |
| AC-12 | The same prototype can be invoked with Claude Code or Codex where both are installed, or reports a precise missing capability. |

The scenario is **not considered complete** merely because an LLM says it passed; verify actual command outputs, repository branches, PR URLs, files and state.

## Suggested build sequence

1. **Harness:** host/environment checks, single-job state contract, worktrees, GitHub linkage, dry-run test.
2. **Discovery and approval:** interactive PM/Engineering handoff, persistent specs, Gate A.
3. **Builder and review:** selectable Claude/Codex, deterministic checks, repair limits, draft PR, Gate B, recovery tests.
4. **Knowledge adapter:** scoped Markdown reader/search and approved diff/write, then optional existing-MCP read checks.
5. **Verification and docs:** acceptance demonstration, Windows testing, safety regression checks and explicit remaining limitations.

This is intentionally a **vertical slice**, not a promise of all roadmap features at once. Progress must be auditable in individual PRs, each subject to human review.

## Pre-implementation checks

Status as of 2026-10-25:

- Validate exact `/forge` plugin/skill packaging and `$forge` discovery paths on installed CLI versions. **Partly done:** repository-scoped skills are verified in Claude Code 2.1.310 and Codex CLI 0.164.0 ([getting started](getting-started.md#verified-invocation)); installing them for other repositories is still open.
- Confirm GitHub Project creation/linking and access using the intended repository and account. **Done:** Project #2 is bound with `forgeflow init --project 2`, and a live handoff created and tracked an issue on it ([ADR 0004](decisions/0004-planning-workflow-and-gate-a.md)).
- Confirm whether installed Anthropic Product Management / Engineering plugins can be invoked from the relevant Claude Code worker context. **Done for the interactive hosts** (ADR 0004). Builders and reviewers deliberately do not use them: the Claude builder runs with `--restricted` (no plugins, settings or MCP) and works from the approved plan, which already carries the design (ADR 0005).
- Decide state lock/atomic write behavior on Windows. **Done:** [ADR 0003](decisions/0003-runtime-state-and-worktrees.md).
- Confirm safe test repo/vault locations, permissions and minimal test commands. **Open:** a disposable test vault is still needed for Checkpoints D and E.

These checks are **inputs to implementation**, not grounds for the builder to invent unsupported capabilities. Each checkpoint is delivered as its own reviewed PR rather than from a single master implementation prompt.
