# AGENTS.md — working on ForgeFlow

Instructions for any coding agent (Claude Code, Codex) changing this repository. Only what the
code cannot tell you. Architecture and rationale live in `docs/` and `docs/decisions/`.

## Commands

Windows: `python` may resolve to 3.10. Always use `py -3.13` (elsewhere: `python3.13`).

| Purpose | Command |
| --- | --- |
| one test module | `py -3.13 -m unittest tests.test_<name> -v` |
| full suite (~4 min) | `py -3.13 -m unittest discover -s tests -t .` |
| lint / format | `py -3.13 -m ruff check .` / `py -3.13 -m ruff format --check .` |
| types (strict) | `py -3.13 -m mypy` |

A change is verified only when all four pass on the commit. Run the narrowest test first, the
full set before every commit.

## Invariants (never weaken to get past a failure)

- Runtime is standard-library Python 3.13. No new runtime dependencies.
- Gate A (plan approval), Gate B (merge) and the single writer per job are human/kernel
  boundaries. Never run `forgeflow approve`, never merge, never edit `.forge/runtime/**`
  by hand to force a state. Use the CLI's transitions.
- Preserve partial work: no `git reset --hard`, `git clean`, `git checkout -- <path>` or
  worktree removal on work you did not create in this session.
- Mocked evidence is never delivery evidence. Tests use fakes; live provider runs are opt-in.
- `store.py` holds the trust invariants. Move pure logic out of it only with its tests intact.
- Do not relax a check, test or permission to make a task pass. Report the blocker instead.

## Working protocol (one task at a time)

Multi-step work has a plan file under `docs/plans/` (see `docs/plans/TEMPLATE.md`). The plan
file is the state; your memory of a previous session is not.

1. **Orient.** `git status`, `git log --oneline -10`, read the plan file and its Progress log.
   Run the full checks once if the log does not record a green state for HEAD.
2. **Pick one task** — the first unchecked one. Do not start a second task in the same commit.
3. **Bound it.** Before editing, confirm the task's `Files:` list. Touching a file outside it
   means the plan is wrong: stop and update the plan (or ask), do not expand silently.
4. **Implement with the loop:** small edit → narrow test → fix. Write the failing test first
   when the task fixes a behaviour.
5. **Verify** with the four checks. Then get a fresh review of the task's diff against its
   `Done when:` criteria (Claude Code: the `reviewer` subagent).
6. **Resolve** every in-scope finding or record why it is invalid. "Known limitations" is not
   a place for unresolved in-scope defects.
7. **Commit** one task per commit (`<area>: <task id> <summary>`), then append to the plan's
   Progress log: task, commit, checks result, findings and their disposition, open questions.
8. Stop when the task is done or a budget is hit; never leave HEAD red without saying so in
   the Progress log.

## Size and quality budgets

- A task should fit in one reviewable commit: about ≤ 400 changed lines and ≤ 6 files,
  tests included. Bigger means split it in the plan first.
- No speculative abstractions, unused parameters, dead code, or comments that restate code.
- Every behaviour change has a test that fails without it.
- Docs change only where behaviour changed. One canonical place per fact; link, do not copy.

## Pull requests

Branch per plan (`feat/…`, `fix/…`, `docs/…`). PR body: what changed, the plan file, checks
run on the head commit, review findings and their disposition. The human merges.
