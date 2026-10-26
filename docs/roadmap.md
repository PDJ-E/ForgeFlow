# ForgeFlow roadmap

**Status:** Phase 1 in progress. Checkpoints A ([PR #3](https://github.com/PDJ-E/ForgeFlow/pull/3)) and B ([PR #5](https://github.com/PDJ-E/ForgeFlow/pull/5)) are merged; Checkpoint C is implemented on its own branch and PR, awaiting review. Checkpoint D is next. Pre-alpha, nothing released.  
**Last reviewed:** 2026-10-26.

The first complete implementation target is **[MVP v0.1](mvp-scope.md)**. Work should proceed in small reviewable PRs and demonstrate real behavior, not merely produce agent-written design claims.

## Phase 0 — Project foundation (done)

**Goal:** establish a credible, public, MIT-licensed project and its design boundaries.

- [x] Public GitHub repository and MIT license
- [x] Initial README and upstream attribution/license inventory
- [x] ADR 0001: GitHub Projects for approved work, local runtime state for execution
- [x] ADR 0002: optional, scoped knowledge sources and Obsidian integration
- [x] Explicit MVP v0.1 scope and acceptance tests
- [x] Review and merge the foundation documentation PR ([PR #1](https://github.com/PDJ-E/ForgeFlow/pull/1), changelog policy in [PR #2](https://github.com/PDJ-E/ForgeFlow/pull/2))
- [x] Choose and configure a minimal GitHub Project for ForgeFlow development (Project #2 ForgeFlow Development, bound with `forgeflow init --project 2`)
- [x] Review branch protection / repository contribution defaults: ruleset `Main_PR_Rule` on `main` requires a pull request and the `CI OK` check and blocks force pushes; repository admins can still bypass it

**Exit:** docs are merged, project workflow exists, no false claim that software is already available.

## Phase 1 — MVP v0.1 (current)

**Goal:** a single job safely moves from an idea to an owner-approved Pull Request, with optional reusable knowledge context. Build in **five small checkpoints** rather than one giant all-or-nothing change.

### Checkpoint A — Environment and execution harness

- [x] Verify Claude Code, Codex CLI, Git, GitHub CLI, Windows and available skills without installing dependencies silently (`forgeflow doctor`; MCP detection deferred to Checkpoint D)
- [x] Confirm real `/forge` / `$forge` invocation mechanics: repository-scoped skills verified in Claude Code 2.1.310 and Codex 0.164.0; installation into other repositories still open
- [x] Implement minimal durable job state, one-job locking, isolated worktree and non-destructive resume ([ADR 0003](decisions/0003-runtime-state-and-worktrees.md))
- [x] Configure/link a GitHub Project and define ID traceability: job ↔ plan version/hash ↔ issue ↔ Project item ↔ branch ↔ PR. Project #2 ForgeFlow Development is bound with `forgeflow init --project 2` (Checkpoint B)

### Checkpoint B — Discovery and plan approval

- [x] Interactive idea exploration (`/forge idea`), durable outcomes, one versioned plan per job (`/forge design`) ([ADR 0004](decisions/0004-planning-workflow-and-gate-a.md))
- [x] Reuse available Anthropic Product Management and Engineering skills where proven accessible (loaded as plugin skills in Claude Code and as `$pm-*` / `$eng-*` user skills in Codex; fallback guidance only when none is installed)
- [x] Gate A: human plan approval at an interactive console before any issue or code; agent tool calls are refused
- [x] Convert the approved plan into one traceable, idempotent GitHub Issue + Project item in Status Ready

### Checkpoint C — Build, QA, independent review and PR

Implemented with [ADR 0005](decisions/0005-build-review-and-delivery.md); `forgeflow run`, `resume`, `rework` and `sync`.

- [x] Select Claude Code or Codex CLI as the sole writable builder (`--builder`, checked before dispatch; invocations verified against Claude Code 2.1.311 and Codex CLI 0.164.0)
- [x] Run project-specific deterministic checks (`.forge/forgeflow.toml`, read from the base commit) and a focused review, optionally by the other provider, on a read-only snapshot
- [x] Limit repair rounds (2 by default), keep evidence and escalate unresolved risks to `needs_human`
- [x] Produce a draft PR; Gate B requires owner approval for merge (ForgeFlow has no merge operation)
- [x] Prove interrupted-job recovery without duplicate workers or lost changes (`tests/test_recovery.py`: a real orchestrator killed mid-build and mid-review)
- [x] ForgeFlow owns Project Status transitions, driven only by verified job events:
  - **Ready → Building → QA → Review → Done**, with **Blocked** for exceptions. The Project's own status-changing workflows are disabled by the owner (2026-10-25), so there is one source of these transitions and no competing automation.
  - **Building:** when a builder is dispatched (job enters `building`). Starting work never closes the issue.
  - **QA:** deterministic checks are running or recorded for the current build round (`verifying`).
  - **Review:** review and Gate B, i.e. a draft PR whose head is the validated commit (`reviewing`, `awaiting_merge_approval`).
  - **Done:** only after `record_merge` observes on GitHub the merge of that PR with the validated head commit, never on an agent's or reviewer's claim.
  - **Blocked:** `needs_human`, exhausted rework or an unresolved risk. The item returns to its previous status when the job resumes.
  - One mapping from job state to Status (`forgeflow.board.STATUS_FOR_STATE`), applied by a store transition listener rather than inside the transition's side effects, so no GitHub call runs under the state lock; never in adapters or separate rules. Each sync is level-triggered and idempotent and re-reads the item, as the Checkpoint B handoff does. GitHub Projects stays the authoritative board for approved work; ForgeFlow keeps no competing local board.

### Checkpoint D — Optional Knowledge Management

- [ ] One project-to-Markdown-root association; read/list/text-search with attribution
- [ ] Propose a documentation edit as a diff; perform a scoped, conflict-checked, recoverable write only after explicit authorization
- [ ] Validate read-only access through an **existing** `mcp-obsidian` integration in the test vault, without altering client global MCP configurations
- [ ] Confirm the factory works without Obsidian or MCP
- [ ] Account for unrestricted global MCP exposure as an explicit security limitation rather than claiming permissions are enforced

### Checkpoint E — Verify the complete vertical slice

- [ ] Run all 12 acceptance scenarios in [MVP scope](mvp-scope.md) in a disposable repository and vault
- [ ] Verify both provider invocation paths where installed; record unsupported combinations honestly
- [ ] Review code, documentation, operational logs, recoverability and secret handling
- [ ] Ship only after human-reviewed PRs and honest release notes

**Exit:** observed, repeatable evidence for the MVP criteria; no automatic merge/deploy and no unapproved vault modifications.

## Phase 2 — Operational polish (after v0.1)

**Goal:** learn from real runs before adding orchestration complexity.

- [ ] Improve model/provider routing based on practical usage
- [ ] Add safe, evidence-backed learning proposals (no autonomous skill rewrites)
- [ ] Add richer costs/tokens visibility where providers expose data
- [ ] Reassess whether a small local observability dashboard is needed
- [ ] Consider a native Obsidian MCP adapter and multi-vault support
- [ ] Refine GitHub Project automation, retries and CI feedback

## Phase 3 — Pilot and open-source hardening

- [ ] Trial one bounded, approved non-production feature of a real project such as Weather Station
- [ ] Measure human time, task duration, token/cost estimates, retries and defects
- [ ] Validate installation/update/uninstall paths on Windows and other declared hosts
- [ ] Re-check third-party attributions and licenses at actual versions used
- [ ] Document compatibility, troubleshooting, threat boundaries and contribution workflow

## Explicitly deferred beyond the first MVP

Do not start with a local Kanban clone, distributed multi-agent swarms, unlimited parallel coding, unattended deployment, production DB modification, a vector database, cross-machine Obsidian synchronization, recursive self-modification, or a custom MCP server.

See [architecture](architecture.md), [MVP v0.1](mvp-scope.md) and the [ADR index](decisions/0001-github-projects-local-state.md) for current boundaries.
