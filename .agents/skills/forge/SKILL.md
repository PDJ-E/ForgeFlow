---
name: forge
description: ForgeFlow entry point - interactive product discovery (idea), engineering design, handing an approved plan to GitHub, running an approved job (build, checks, review, draft PR), job status and environment checks. Use only when the user explicitly invokes $forge.
---

# $forge - ForgeFlow (Checkpoint C)

The operation is the text the user wrote after `$forge`.

ForgeFlow's logic lives in its Python CLI; this skill only holds the conversation and calls it.
Run commands from the repository root and report their output faithfully, warnings included.

## Commands

| Operation | Command |
| --- | --- |
| environment check | `python -m forgeflow doctor` |
| jobs / one job | `python -m forgeflow status` / `python -m forgeflow status <job>` |
| bind the GitHub Project | `python -m forgeflow init` (lists Projects), then `python -m forgeflow init --project <N>` only if the user names N |
| skills available here | `python -m forgeflow skills --host codex` |
| start discovery | `python -m forgeflow idea new "<title>" --idea "<user's words>"` |
| record an outcome | `python -m forgeflow idea add <job> <kind> "<text>" [--status accepted|rejected] [--note "why"] [--supersedes <id>]` |
| accept / reject / reinstate | `python -m forgeflow idea mark <job> <id> accepted|rejected|proposed [--note "..."]` |
| resume discovery | `python -m forgeflow idea show <job>` |
| create the plan | `python -m forgeflow design new <job>` |
| validate / view | `python -m forgeflow design check <job>` / `python -m forgeflow design show <job>` |
| submit for approval | `python -m forgeflow design submit <job>` |
| withdraw to revise | `python -m forgeflow design revise <job> --reason "..."` |
| publish an approved plan | `python -m forgeflow handoff <job>` (`--dry-run` previews) |
| build, check, review, open a draft PR | `python -m forgeflow run <job> --builder claude|codex --reviewer claude|codex` |
| continue after an interruption | `python -m forgeflow resume <job>` |
| another round on the draft PR | `python -m forgeflow rework <job> --reason "..."` |
| record a merge, sync the board | `python -m forgeflow sync <job>` |

`<job>` is the short reference ForgeFlow prints (for example `J3`) or the full id
(`ff-YYYYMMDD-xxxxxx`). Use the short reference when talking to the user.

Kinds: problem, user, goal, requirement, constraint, assumption, alternative, decision, risk,
question, non-goal.

Not implemented yet: `learn`. Say so and stop.

Codex notes: discovery and design write only inside `.forge/runtime/` of this workspace, which
the default workspace-write sandbox allows. `init`, `doctor` and `handoff` contact GitHub; if the
sandbox blocks the network, say so and ask the user to run that command in their own terminal
(approval already publishes the plan, so `handoff` is normally only a retry). If neither
`python` nor `py -3.13` can run ForgeFlow here, say so and stop: Codex's elevated Windows
sandbox runs commands as a separate user that cannot see a Python installed only for the
current user (see `docs/getting-started.md`). Do not install anything.

## idea - interactive discovery

1. `idea <text>` starts a job with `idea new`; `idea resume <job>` runs `idea show <job>` and
   continues from the recorded outcomes, not from memory.
2. Run `python -m forgeflow skills --host codex` once. It lists each skill by the exact name to
   invoke it with (for example `$pm-brainstorm`). If it names a skill for `idea` **and** that
   skill is available in this session, invoke it by that name and use it as your
   thinking-partner guidance. Otherwise use the fallback below.
3. Converse. Keep it proportional: a small change needs a few questions, not a questionnaire.
   Explore the problem and users, surface assumptions and constraints, compare two or three
   approaches with their trade-offs, and let the user refine or reject them.
4. Record only conclusions worth resuming from, as they are reached: an accepted requirement,
   a rejected alternative stated as the option itself with `--status rejected --note "<reason>"`,
   a decision. Use `--supersedes` to refine an item and
   `idea mark` to reject one or return to an earlier one. Do not record chit-chat.
5. When the user is satisfied, suggest `$forge design <job>`.

Fallback guidance (no Product Management skill): ask who has the problem and what they do
today; separate symptoms from causes; name the riskiest assumption; offer at least one simpler
alternative; state what is out of scope.

## design - engineering plan

1. `python -m forgeflow design new <job>` creates one plan file from the discovery and prints its
   path. It is the only design document: do not create separate spec, task or ADR files.
2. Run `skills` (as above) for `design`. Invoke the listed skills that are available in this
   session by their exact names (for example `$pm-write-spec`, `$eng-system-design`,
   `$eng-testing-strategy`) to strengthen the solution, trade-offs, risks and verification;
   for any that are missing, cover it yourself:
   components and data flow, at least one alternative with its trade-off, failure modes, and
   which tests prove each acceptance criterion. Read the repository's existing docs and ADRs.
3. Fill the plan with the user: replace every `TODO(forgeflow)` placeholder; keep required
   sections; each `### T<n>: <title>` task needs `- [ ]` acceptance criteria. Show the user the
   changes and revise until they are content. Run `design check` and fix what it reports.
4. Submit only when the user says it is ready: `python -m forgeflow design submit <job>`.
   The approved plan is published verbatim to GitHub, so keep private notes out of it.

## approve - Gate A (the human does this, not you)

Never run `forgeflow approve` yourself and never say the plan is approved because the user
said so in chat. Tell the user to open their own terminal (PowerShell, Windows Terminal or a
shell) in this repository and run:

    py -3.13 -m forgeflow approve <job>        (python3.13 -m forgeflow approve <job> elsewhere)

It shows the exact plan version, its hash and what approval authorises, asks
`Approve plan vN of Jn? [y/N]`, then records the approval and publishes the issue to the bound
GitHub Project.
If it is run without an interactive console it exits with code 3 and records nothing.

## run - build, checks, review and a draft PR (Checkpoint C)

Only for a job whose plan a human approved and handed off. Run it only when the user asks and
names **both** the builder and the reviewer; never pick them yourself. The reviewer may be the
other provider (cross-review) or the same one.

    python -m forgeflow run <job> --builder claude --reviewer codex

ForgeFlow does everything itself: it prepares the job's worktree, runs the builder there, checks
and commits its changes, runs the repository's checks (`.forge/forgeflow.toml`), has the
reviewer judge a read-only snapshot, reworks at most twice, pushes the job branch and opens a
**draft** pull request. A build can take many minutes; tell the user it is running. Report the
outcome it prints: the PR link (Gate B: the owner merges, never you), or why it stopped for a
human (exit code 4) and what it says to do next. `run`, `resume` and `rework` also need the
network and start other agent CLIs, so suggest running them in the user's own terminal when
this session cannot.

- `resume <job>`: after an interruption; same providers, never a second builder.
- `rework <job> --reason "..."`: only when the user asks for changes to a delivered draft PR.
- `sync <job>`: after the owner merged the PR on GitHub (records it, Project Status Done), or to
  bring the Project Status in line with the job.
- A job that stopped for a human continues only when the user gives the reason:
  `run <job> --reason "..."` (optionally with other `--builder` / `--reviewer`).

## handoff

Only after approval: `python -m forgeflow handoff <job>` publishes (or finishes publishing) the
approved plan as one GitHub issue on the bound Project in its Ready status. It is safe to rerun
and never creates duplicates. It refuses unapproved or changed plans.

## Rules

- Never write or change the job's code yourself, and never create worktrees, branches, commits, issues,
  Project items or PRs by hand
  (no `gh issue create`), or dispatch any builder. Only ForgeFlow commands publish to GitHub.
- Edit only the job's plan file, and only during `design`.
- ForgeFlow needs Python 3.13+. If a command fails with `ForgeFlow requires Python 3.13 or newer`,
  rerun it once with `py -3.13 -m forgeflow` (Windows) or `python3.13 -m forgeflow`, and say
  which interpreter ran it. If it still fails, report the error and point to
  `docs/getting-started.md`. Do not install anything.
