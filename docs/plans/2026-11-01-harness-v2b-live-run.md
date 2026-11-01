# Harness v2b — live validation run (prepared, not executed)

- **For:** [`2026-11-01-harness-v2b.md`](2026-11-01-harness-v2b.md) T5; ADR 0006 becomes
  `Accepted` only after this run, with its record.
- **Status:** prepared. No job exists yet. Nothing below has been run.
- **Needs the owner:** authorisation to create the job; Gate A (`forgeflow approve`, the owner
  at the console), which also publishes the GitHub issue and Project card; the authorisation
  to `run`; and Gate B (the owner merges or closes the draft PR).

## Why this job

It is small, real and independent of v2b's code: the job's worktree is cut from `origin/main`,
which does not contain v2b yet, while the orchestrator runs from this branch. It has two tasks
with disjoint `Files:`, so the run shows two builder invocations, two task commits
(`forgeflow: J<n> T1 round 1`, `… T2 round 1`), two `task-result` records, then the final
checks, the review and a draft PR. The builder's shell runs this repository's four checks.

## Steps (owner-authorised)

```powershell
py -3.13 -m forgeflow idea new "Show the approved plan's tasks in forgeflow status"
py -3.13 -m forgeflow design new J<n>        # then replace plan.md with the plan below
py -3.13 -m forgeflow design check J<n>
py -3.13 -m forgeflow design submit J<n>
py -3.13 -m forgeflow approve J<n>           # Gate A: the owner, in their own console; publishes the issue
py -3.13 -m forgeflow run J<n> --builder claude --reviewer claude
py -3.13 -m forgeflow status J<n> --json     # all evidence, including task-round and task-result
git log --format='%h %s' origin/main..forge/<job-id>   # the per-task commits
```

To record per task: commit, check results, turns, cost (API-equivalent) and duration, from
the `task-result` and `provider-result` evidence and the stream files under the job's
`evidence/`.

Watch for: this repository's only test command is the full suite (~4 min), longer than the
Claude Code Bash tool's default 2-minute timeout, so the builder must raise the tool's timeout
to run it (the stream shows whether it did). Each task's checks also take ~4 min, the docs-only
T2 included.

## The plan

```markdown
# Show the approved plan's tasks in forgeflow status

## Problem and objective

`forgeflow status <job>` shows the plan's path, version and hash, but not what the plan asks
for. To see the tasks of an approved plan the owner has to open the plan file or the issue.

Objective:

- `forgeflow status <job>` lists the approved plan's tasks with their criteria and file counts.

## Requirements

### Functional

- For a job with an approved plan, the job detail of `forgeflow status <job>` shows one line
  per `### T<n>` task: id, title, number of criteria and number of files.
- Jobs without an approved plan show no task lines and no error.

### Non-functional

- `status` stays read-only: it writes nothing and changes no revision.
- Standard library only; no new dependency.

## Proposed solution

Reuse `design.check_text(text).summary_lines()` on `store.approved_plan_text(job)` inside the
job detail in `forgeflow/cli.py`, and indent its lines under a `plan tasks` heading. A plan
whose snapshot cannot be read shows `plan tasks     unavailable (<reason>)`.

## Alternatives and trade-offs

- A new `forgeflow plan show` command: more surface for the same information; rejected.
- Parsing the issue body on GitHub: needs the network for a local view; rejected.

## Scope

### In scope

- The job detail of `forgeflow status <job>` (text output) and its test; the user guide.

### Out of scope

- The `--json` output, the job list, and any change to plan validation.

## Dependencies and risks

- The plan snapshot may be missing or altered: `approved_plan_text` raises; status must
  report that instead of failing.

## Acceptance criteria

- [ ] `forgeflow status <job>` lists the approved plan's tasks for an approved job.
- [ ] A job without an approved plan, or with an unreadable snapshot, still shows its status.

## Verification

- Unit tests in `tests/test_environment_cli.py` through `cli.main`, and the repository's four
  checks.

## Tasks

### T1: List the plan's tasks in the job detail

Add the `plan tasks` lines to the job detail in `forgeflow/cli.py`.

- **Files:** `forgeflow/cli.py`, `tests/test_environment_cli.py`
- [ ] `status <job>` of an approved job prints a `plan tasks` heading and one line per task
  with its criteria and file counts, tested through `cli.main`
- [ ] a draft job and a job whose plan snapshot cannot be read still print their status, with
  no task lines or with `unavailable`, and `status` changes no revision

### T2: Document it

- **Files:** `docs/getting-started.md`, `CHANGELOG.md`
- [ ] the guide's `forgeflow status` description mentions the plan's tasks
- [ ] `CHANGELOG.md` has one Unreleased line for it
```
