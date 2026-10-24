# ForgeFlow roadmap

**Status:** draft / design-phase plan — scope and priorities may change.

This roadmap describes outcomes, not promises of implemented functionality. ForgeFlow has no released factory runtime yet. Each phase should end with a demonstrable, reviewable checkpoint.

## Phase 0 — Project foundation (current)

**Goal:** create a trustworthy public repository that explains the product and its boundaries.

- [x] Public GitHub repository and MIT license
- [x] Project introduction and explicit pre-alpha status
- [x] Third-party inspiration and licensing inventory
- [x] Initial architecture and GitHub/local-state decision
- [ ] Review and merge these project-foundation documents
- [ ] Define GitHub contribution/branch protection defaults
- [ ] Decide minimum initial GitHub Projects board and fields

**Exit criteria:** design and licensing boundaries are readable to an independent contributor; no claims of functionality that does not exist.

## Phase 1 — Minimal end-to-end vertical slice

**Goal:** prove the workflow with one disposable test repository before scaling.

- [ ] Verify Claude Code, Codex CLI, plugin, and host capabilities (including Windows)
- [ ] Define and test the durable job contract and minimal local state transitions
- [ ] Implement a minimal supported command entry point, with syntax confirmed on actual hosts
- [ ] Accept an explicitly approved, small Issue/task
- [ ] Run one builder in an isolated worktree
- [ ] Execute required tests/checks
- [ ] Produce a reviewable draft Pull Request and evidence
- [ ] Require human approval for merge
- [ ] Demonstrate interrupted-job recovery

**Exit criteria:** one small task completes to a human-reviewed PR with a replayable audit trail; no automatic merge, no secrets leakage.

## Phase 2 — Product discovery and engineering design

**Goal:** make ForgeFlow helpful *before* coding begins.

- [ ] Integrate installed Product Management / Engineering skills when genuinely available
- [ ] Persist discovery, product scope, ADRs and acceptance criteria in approved formats
- [ ] Add explicit human plan-approval boundary
- [ ] Convert approved plans to GitHub Issues and Project Ready work
- [ ] Handle changed requirements without silent re-approval

**Exit criteria:** a human can debate alternatives, approve a design, and create a traceable implementation job without auto-dispatch.

## Phase 3 — Multi-model implementation and review

**Goal:** allow Claude Code and Codex CLI to fulfill distinct, safe roles.

- [ ] Provider adapters and configurable model routing
- [ ] Single-writer worktree permissions and crash-safe recovery
- [ ] Optional independent cross-provider review
- [ ] Deterministic checks before relevant AI review
- [ ] Bounded corrective iterations and human escalation
- [ ] Evidence-rich PR summaries and usage estimates

**Exit criteria:** demonstrate both provider directions on representative changes and handle one unavailable-provider scenario without losing state or bypassing gates.

## Phase 4 — Observability and learning

**Goal:** turn verified delivery history into actionable, reviewable improvements.

- [ ] Summarize runs, statuses, retries, durations, and available token usage locally
- [ ] Gather recurring defects or review findings without automatically creating noise
- [ ] Propose improvement changes with evidence and a regression check
- [ ] Require human review before modifying factory skills/policies
- [ ] Evaluate whether a small local diagnostic dashboard is justified

**Exit criteria:** a recurrent failure leads to a reviewable preventive test or process change; the system never silently rewrites shared instructions.

## Phase 5 — Real-world pilot and release hardening

**Goal:** prove value on a bounded non-production feature of a real project.

- [ ] Execute one approved feature on a project such as Weather Station
- [ ] Measure human effort, cycle time, retries, defects, and inference usage
- [ ] Document installer behavior, dependency checks, security boundaries, Windows compatibility, troubleshooting, and rollback
- [ ] Validate license and attribution notices for actual vendored code and assets
- [ ] Produce an honest compatibility matrix and release notes

**Exit criteria:** other developers can reproduce the documented installation and pilot scenario using the declared requirements.

## Scope discipline

Not goals for the first milestone: a bespoke Kanban UI, unlimited agent swarms, autonomous production deployment, recursive self-editing, generalized project analytics, or universal support for every programming language and host.

See [architecture.md](architecture.md) for unresolved questions and [ADR 0001](decisions/0001-github-projects-local-state.md) for the agreed work/state ownership.
