# ADR 0001: GitHub Projects for approved work; local state for agent execution

- **Status:** Accepted; partly implemented. Local job state is implemented by Checkpoint A ([ADR 0003](0003-runtime-state-and-worktrees.md)). Publishing an approved plan as one issue and Project item is implemented by Checkpoint B ([ADR 0004](0004-planning-workflow-and-gate-a.md)). Pull requests and later Project statuses come with Checkpoint C.
- **Date:** 2026-10-24
- **Deciders:** ForgeFlow project owner

## Context

ForgeFlow needs both a usable cross-device project board and detailed observability for agent runs.

Two extremes are undesirable:

1. Put everything in a new local dashboard, duplicating the features GitHub Projects already offers.
2. Force granular token accounting, command logs, intermediate subagent handoffs, and private runtime artifacts into GitHub Issues/Projects.

GitHub Free Projects can manage public and private repositories, and the factory should be useful for private codebases without exposing them.

## Decision

**Use GitHub Projects as the primary board for approved software implementation work.** An approved plan becomes a task, normally represented by a GitHub Issue and linked to a Project. GitHub PRs and Actions represent review and deterministic delivery checks.

**Use local durable files for technical execution details** such as job state, agent/provider assignment, logs, attempt metadata, local evidence, token counts and estimated costs. Their formats and location will be decided after a minimal state-machine prototype.

Do not build or synchronize a duplicate local Kanban view. A small local observability UI may be introduced later if CLI status and GitHub links do not provide adequate insight.

Brainstorming and design can remain local and interactive before the owner approves an implementation plan. Not every rough idea becomes a GitHub Issue.

## Consequences

**Benefits**
- Native GitHub Issues/PR relationships, hosted Kanban and remote accessibility.
- Less UI and synchronization logic to build and maintain.
- Logs and potentially sensitive agent execution details can stay private and local.
- Preserves the option to add observability without changing the work-management system.

**Costs / tradeoffs**
- Managing approved work depends on GitHub access and relevant API permissions.
- Local run-state recovery and cross-device synchronization need an explicit policy.
- A Project board does not inherently contain detailed provider token/cost telemetry.
- GitHub API rate limits and eventual consistency require bounded polling and idempotent writes.

## Boundaries

- Do not publish private run logs, secrets or repository code to a public ForgeFlow repository.
- Avoid dual-write ownership and reconcile references by stable IDs.
- A local run is not “Done” because an agent says so; check the linked evidence and authoritative GitHub status.
- A GitHub task reaching “Review” is not permission to merge.
- If GitHub Projects is unavailable, a future fallback can be considered separately; it should not silently create a competing default board.

## Future revisit criteria

Revisit only if documented pilot use shows that Projects is unavailable, operationally costly, or unable to support a necessary workflow without unreasonable synchronization complexity.
