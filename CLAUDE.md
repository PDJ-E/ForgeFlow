@AGENTS.md

## Claude Code specifics

- You are the only writer in a session. Subagents in `.claude/agents/` are read-only helpers:
  - `verifier` — runs the checks and returns a short pass/fail report. Use it for the full
    suite so its output stays out of your context.
  - `reviewer` — fresh-context review of one task's diff against its `Done when:` criteria.
    Run it after the checks pass and before committing.
  - Built-in `Explore` for broad code searches.
- Do not delegate implementation to subagents and do not run writers in parallel.
- `.claude/settings.json` enforces the hard rules (deny list, Stop hook with lint/format/type
  checks on changed Python files). If a hook blocks you, fix the cause; do not edit the hook.
- ForgeFlow's own builder runs `claude -p --restricted` inside job worktrees. Anything added
  under `.claude/` must not leak into that subprocess; see ADR 0006.
