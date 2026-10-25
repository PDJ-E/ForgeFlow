---
name: forge
description: ForgeFlow software factory entry point. Checkpoint A supports only read-only environment inspection (doctor) and local job status. Use only when the user explicitly invokes /forge.
argument-hint: "[doctor | status [job-id]]"
disable-model-invocation: true
---

# /forge — ForgeFlow (Checkpoint A)

Requested operation: `$ARGUMENTS`

Run exactly one command from the repository root and report its output faithfully, including every warning:

| Operation | Command |
| --- | --- |
| `doctor` | `python -m forgeflow doctor` |
| `status` (or no argument) | `python -m forgeflow status` |
| `status <job-id>` | `python -m forgeflow status <job-id>` |

Not implemented yet: `init`, `idea`, `design`, `approve`, `run`, `review`, `rework`, `resume`, `learn`.
For any of these, say that the operation is not available in this checkpoint and stop.

Rules:

- Do not emulate an unimplemented operation by hand: do not create jobs, worktrees, branches, issues, Project items or PRs, and do not edit files.
- Do not approve plans or merges on the user's behalf.
- If `python -m forgeflow` fails with `ModuleNotFoundError`, report that ForgeFlow is not installed for this Python interpreter and point to `docs/getting-started.md`. Do not install anything.
