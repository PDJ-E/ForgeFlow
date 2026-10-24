# ForgeFlow — architecture and design boundaries

**Status:** design proposal, not an implementation contract.  
**Last reviewed:** 2026-10-24.

ForgeFlow is a human-governed, multi-model software engineering workflow. It should connect idea exploration, product definition, architecture decisions, verified implementation, independent review, and learning while minimizing unnecessary agents and operational complexity.

## Agreed direction

1. **ForgeFlow** is the project name. Aim for a short **`/forge`** user-facing entry point in Claude Code and an equivalent **`$forge`** skill in Codex when technically supported. Namespacing, installation, and collisions are to be verified.
2. **Product thinking comes before implementation.** Discovery and Design are interactive. A human must explicitly approve the scope and plan before dispatching code-writing agents.
3. **Claude Code and Codex CLI** should be usable as builders and reviewers through a clear provider abstraction. Independent cross-provider review is desirable for significant changes but must be configurable, not a mandatory extra call for every trivial edit.
4. **Anthropic Engineering and Product Management** are external, reusable capabilities. Verify that the actual Claude Code host can access the installed plugin skills. Data and Productivity are optional, context-sensitive.
5. **GitHub Projects is the source of truth for approved implementation work.** GitHub Issues hold actionable requirements; Pull Requests and CI record delivered changes. Do not replicate the GitHub Kanban board locally.
6. **The local runtime is the source of truth for execution details**: job/agent transitions, logs, locally measured token usage, reviewer evidence, and transient worktree metadata. Link to GitHub issue/PR identifiers without maintaining a competing task board. See [ADR 0001](decisions/0001-github-projects-local-state.md).
7. **A local observability dashboard is optional, not an MVP prerequisite.** Introduce one only if GitHub Projects and local diagnostic commands fail to answer recurring operational questions.
8. **Human approval is mandatory for merge.** Deployment is a separate, explicitly authorized concern; a successful review or PR does not authorize production deployment.
9. **Continuous improvement is proposal-driven.** Lessons are drawn from verified problems, recurring review findings, and corrections. Changes to shared skills, policies, or agent instructions require review and approval.

## Proposed workflow

```text
DISCOVER (interactive PM exploration)
     |
DESIGN (requirements + engineering + ADRs)
     |
     +-- human rejects/revises --> back to DESIGN
     |
HUMAN PLAN APPROVAL
     |
GitHub Issue + Project Ready card
     |
BUILD (one writable owner: Claude or Codex; isolated worktree)
     |
DETERMINISTIC CHECKS (configured for the target repo)
     |
QA + RELEVANT REVIEW (possibly independent other provider)
     |
     +-- actionable failure --> bounded builder rework (default max 2)
     +-- unresolved risk  --> human escalation
     |
Pull Request + evidence + risk summary
     |
HUMAN MERGE APPROVAL
     |
Post-run LEARN (optional, bounded, reviewable proposals)
```

The factory must distinguish **approving a plan** from **approving the actual PR**. Neither approval should silently grant permission for future unrelated work.

## Responsibility boundaries

| Concern | Intended authoritative location |
| --- | --- |
| Initial discussion / unapproved ideas | Local design artifacts and interactive sessions, later optional Obsidian references |
| Architectural decisions / project-facing specs | Version-controlled Markdown (where appropriate) |
| Approved feature backlog and workflow status | GitHub Projects + Issues |
| Source history, review, integration | Git branches, commits, PRs, required CI |
| Agent execution state, logs, usage, transient evidence | Local runtime files (schema TBD) |
| Long-lived knowledge | Project docs; optional user-approved Obsidian synchronization |
| Provider credentials | Host environment/secret managers; never repo files |

A durable cross-system identifier should link approved plans, GitHub Issues, runs, and PRs. Avoid bidirectional synchronization that creates conflicting owners.

## Provider and agent boundaries

- Introduce an execution contract: job id, approved requirements reference, repository/ref, constrained paths, allowed tools, test commands, output/evidence format and timeout/cost limits.
- Keep a single writer per task/worktree; the reviewer analyzes committed changes in a separate read-only context where practicable.
- Support manual routing and four conceptual policies: Claude-only, Codex-only, Claude-build/Codex-review, Codex-build/Claude-review.
- Treat provider/model names and prices as configuration, not permanent constants.
- Fail safely when a provider, plugin, MCP server, auth token, or model is missing. An unavailable provider does not authorize bypassing review.
- Begin with **one active job**; higher concurrency requires evidence that state locking and worktree isolation are reliable.

## Verification and security

- Run deterministic, project-specific checks before AI review. No “green” claim without reproducible evidence.
- Use scoped credentials; keep logs and artifacts out of the public repository by default.
- Disallow silent direct pushes to protected branches, force pushes, production data writes, destructive migrations, and unsupervised deployment.
- Review external Issue text, dependency docs, and tool responses as untrusted input.
- Escalate material auth, finance, privacy, migration, deployment, and architectural contract changes.
- Avoid silently bypassing CI because runners or budgets are unavailable.

## Documentation tools

Use Context7 or official version-specific documentation only where external SDK/library behavior warrants it, with a fallback when Context7 is not installed. Read local source and existing ADRs for project-owned behavior.

## Open implementation questions

These are intentionally **not settled**:

1. How will ForgeFlow package the short `/forge` alias and the Codex `$forge` equivalent reliably across installations?
2. Which Anthropic Knowledge Work Plugin capabilities are discoverable and callable in the actual Claude Code environment and within spawned agents?
3. What is the minimum durable local run-state schema, locking strategy, and interruption/resume protocol?
4. What is the portable Windows execution backend for process management and terminal integrations?
5. Which GitHub Projects fields and automations belong in the MVP, and which should remain optional?
6. How can token usage and estimated costs be reported honestly across different subscriptions, CLIs, and provider APIs?
7. What is the minimum credible cross-provider review contract and escalation policy?
8. What conditions should trigger collection of learning proposals, and how will changes be evaluated before adoption?
9. What is the smallest end-to-end pilot that demonstrates value without touching production services?

See [roadmap](roadmap.md) for the proposed sequencing. Do not resolve these by assumption in a generative build prompt; verify and record decisions in ADRs or Issues.
