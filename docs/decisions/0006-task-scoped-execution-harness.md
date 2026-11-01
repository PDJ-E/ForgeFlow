# ADR 0006: Task-scoped execution harness

- **Status:** Proposed (2026-11-01)
- **Supersedes in part:** ADR 0005 (one builder invocation per round; builder without a shell)
- **Plans:** [`harness-v2a`](../plans/2026-11-01-harness-v2a.md), [`harness-v2b`](../plans/2026-11-01-harness-v2b.md)

## Context

J5 (`ff-20261031-1f069e`, Checkpoint C.1) failed in a way the current harness cannot prevent
or explain:

- The approved plan had 6 tasks, ~25 criteria and touched ~12 modules. It was sent to **one**
  `claude -p` invocation with tools `Read,Edit,Write,Glob,Grep` and a 1,800 s timeout.
- The builder edited 16 files (+1,524/−272, plus a new 891-line `forgeflow/review.py` and a
  195-line test helper) **without running a single test**: it has no shell, and checks run
  only after the whole round. It was still writing tests when it was killed. Nothing was
  committed; T6 and two named test modules were never started.
- The timeout evidence records `usage: unavailable`, no turn count and no last activity,
  because `--output-format json` only reports at the end. A killed run leaves no trace.
- Upstream, discovery for J5 accumulated 73 items (56 KB, 9 superseded) and a 21 KB plan.
  Nothing measures or limits either.

The root causes are structural, not the timeout value:

1. **The unit of execution is the whole plan.** Progress is all-or-nothing.
2. **The feedback loop is outside the agent.** It writes blind and learns of failures a round
   later.
3. **No budgets except wall-clock time,** and no telemetry when that budget kills the run.
4. **Plans have no size contract,** so an oversized job passes every gate.

External guidance points the same way: Anthropic's long-running harness work recommends one
feature per session, a structured task list, a progress file and git commits per unit
([effective harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents));
its planner/generator/evaluator design keeps a separate evaluator because agents over-rate
their own work ([harness design](https://www.anthropic.com/engineering/harness-design-long-running-apps));
and Claude Code's guidance is to give the agent a check it can run, enforce hard rules with
hooks/deny rules rather than prose, and bound unattended runs with `--max-turns` /
`--max-budget-usd` ([best practices](https://code.claude.com/docs/en/best-practices)).

## Decision

ForgeFlow, not a model, orchestrates. It is a deterministic workflow over the plan's tasks
(Anthropic's "workflow" rather than "agent"); models are workers and reviewers inside it.

1. **Task-scoped rounds.** A build round iterates the plan's `### T<n>` tasks in order. Each
   task is one fresh builder invocation with a sliced prompt: objective, requirements, scope,
   the current task in full, the titles and status of the other tasks, and open findings for
   that task. The full approved plan still travels in `ProviderTask.plan_text` so the hash
   check is unchanged; only the rendered prompt is sliced.
2. **Commit and fast-check per task.** After each task ForgeFlow runs the scope check and
   commits (`forgeflow: J<n> T<k> round <r>`), then runs the configured checks. A task whose
   checks fail gets bounded in-task retries; completed tasks stay committed. A `task-result`
   evidence record (task, commit, status, usage) lets `resume` continue at the first
   unfinished task. The full checks and the model review still run once on the final commit;
   Gate A/B, clearance, scope and protected paths are unchanged.
3. **In-loop verification for the builder.** The Claude builder gets `Bash`, restricted with
   `--allowedTools` to the exact argv of the configured checks (plus read-only git), and
   `--permission-mode dontAsk` refuses everything else. Rationale: ForgeFlow already executes
   builder-written code when it runs the checks, so letting the builder run the same commands
   earlier adds feedback, not a new capability. Network, git writes and arbitrary commands
   stay refused. Codex keeps its own sandbox and remains outside the critical path.
4. **Budgets and telemetry.** Every invocation gets `--max-turns` and, where supported,
   `--max-budget-usd`, configured in `.forge/forgeflow.toml` `[build]` with a per-task timeout.
   Output switches to `--output-format stream-json --verbose`; ForgeFlow writes the event
   stream to the job's evidence and derives usage, turns, tools used and the last activity,
   so a timeout or budget stop is diagnosable. Hitting a budget is a distinct result status,
   not a generic failure. `--max-turns` is passed when `claude --help` lists it **or** the
   detected version is ≥ 2.1.316: that version enforces it (verified live: `error_max_turns`,
   "Reached maximum number of turns (2)") and the official CLI docs document it, but its
   `--help` does not list it. `--max-budget-usd` is passed only when `--help` lists it.
5. **Plan size contract.** `design check` requires a `Files:` line per task and reports
   errors above limits (defaults: ≤ 6 tasks per plan, ≤ 6 criteria per task, ≤ 8 files per
   task) and a warning above a plan-size threshold well under `MAX_PLAN_CHARS`. Oversized
   work is split into jobs before Gate A, not discovered at build time.
6. **Discovery hygiene.** `idea show` and the design scaffold render only live (accepted or
   proposed, not superseded/rejected) items; `idea` warns when a job's live items exceed a
   threshold. History stays in `discovery.json`.
7. **Context isolation of the repository's own agent files.** The repository now carries
   `CLAUDE.md`, `AGENTS.md`, `.claude/settings.json` (hooks, permissions) and `.claude/agents/`
   for interactive development. Builder/reviewer subprocesses must not load them implicitly:
   ForgeFlow decides their context. This must be verified for `--restricted` and enforced
   (for example `--bare`, `--setting-sources`, or explicit flags), and these paths are added
   to `protected_paths` so a builder cannot plant hooks for a later round.

Read-only subagents inside the builder (exploration) and per-task model review are deferred:
they cost tokens on every task and have no evidence of need yet. Revisit with telemetry.

## Consequences

- A timeout or budget stop loses at most one task's work, and the record says which.
- More invocations per job; each is smaller. Cost is measured per task from the stream.
- Plans need a `Files:` line per task; older plans still load, and `run` on a plan without it
  falls back to one task containing the whole plan (today's behaviour) with a warning.
- The builder's shell is an allowlist of known commands, documented here and in `doctor`.

## Verification

Deterministic tests with fake CLIs for every point above (argv per role, stream parsing,
budget status, per-task commits and resume, sizing errors, slicing). One opt-in live run with
`--builder claude --reviewer claude` on a small two-task plan, recorded in this ADR.
