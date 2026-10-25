# ADR 0004 — Discovery, design, Gate A and GitHub handoff

- **Status:** Accepted for Checkpoint B (implemented and tested; see [getting started](../getting-started.md))
- **Date:** 2026-10-25
- **Resolves:** [architecture](../architecture.md) open questions 2 (plugin skills in the real hosts) and 5 (Project fields in the MVP); refines 1 (host entry points)
- **Related:** [ADR 0001](0001-github-projects-local-state.md), [ADR 0003](0003-runtime-state-and-worktrees.md), [MVP scope](../mvp-scope.md)

## Context

Checkpoint A built a job store with Gate A as a hash-bound approval record, but anything that could call the library could write that record (ADR 0003, *Gate A strength*). Checkpoint B adds the steps before execution: discuss an idea, turn it into a plan, have a human approve one exact version, and publish the approved work to GitHub. No code may be written or dispatched before that approval.

The constraints are local single-user software on Windows first, stdlib-only Python, no daemon or database, and two hosts (Claude Code, Codex) that must share one implementation.

## Decision

### Discovery: outcomes, not transcripts

`forgeflow idea new` creates a `draft` job (builder `unassigned`) and a `discovery.json` beside `job.json`. The conversation stays in the host. ForgeFlow keeps only its outcomes: problems, users, goals, requirements, constraints, assumptions, alternatives, decisions, risks, questions and non-goals. Each item has a status (`proposed`, `accepted`, `rejected`, `superseded`) and a history. Refining an item supersedes it, and reinstating an earlier item retires its replacement. Nothing is deleted. Discovery changes only while the job is a draft, each change is logged and advances the job revision, and the file never leaves the self-ignored runtime directory.

### Design: one plan document per job

`forgeflow design new` writes `.forge/runtime/jobs/<job>/plan.md` from the discovery. It is the only design document; there is no separate spec, task list or draft ADR. `design check` enforces only what later stages rely on:

- one `# Title`, which becomes the issue title;
- these sections: Problem and objective, Scope, Acceptance criteria, Verification and Tasks, with the rest recommended;
- `### T<n>: <title>` tasks, each with `- [ ]` criteria;
- no `TODO(forgeflow)` placeholders;
- at most 60,000 characters, because GitHub caps issue bodies at 65,536.

`design submit` pins the exact bytes as plan **version** *n* (`plan.version`, snapshot `plans/<sha256>.md`). Resubmitting supersedes a pending version. `design revise` withdraws a submitted or approved version to `draft`. Revising an approved plan (`plan_approved -> draft`, a new transition) is allowed only before anything was published or a worktree exists, and it needs a reason.

### Gate A: a human at an interactive console

- `JobStore.transition()` refuses `plan_approved`. The only path is `forgeflow.approval.approve_plan`, the code behind `forgeflow approve`.
- It runs only when **both** stdin and stdout are an interactive console. On Windows that means `GetConsoleMode` succeeds on both handles; `os.isatty` is not used, because it reports `NUL` as a terminal. On POSIX it means `isatty` on both. Otherwise it exits with code 3 before showing or recording anything.
- It shows the full plan from the snapshot, the version, the sha256, the task summary, what approval authorises (the target repository, its visibility and the Project/Status) and what it does not authorise. It then asks `Approve plan vN of Jn (sha256 …)? [y/N]`, after discarding any console input typed before the question. Only `y` or `yes` approves; everything else, including Enter, end of input and Ctrl+C, cancels without writing.
- The decision is committed against the job revision that was displayed (compare-and-swap), after the store re-hashes the plan file and the snapshot. An edit or resubmission made while the human reads is refused.
- The record names the method `interactive-console` and the approved version. The store refuses any other method when entering `plan_approved`, and again at every later boundary: handoff, `ready` and each `building` dispatch. So an approval written by hand into `job.json` with another method stops at the next boundary.

**Threat model.** This separates a human at their own terminal from an agent acting through its ordinary non-interactive tools: the Claude Code Bash/PowerShell tools, `codex exec`, piped input, or a direct library call to `transition()`. It also stops model-generated claims such as "the user approved this", which are not inputs to ForgeFlow at all.

It does **not** stop a same-user process that deliberately defeats it. Such a process could drive a pseudo-console (ConPTY, `script`, `pexpect`), call the private recording function, write `job.json` with the expected method, or automate the screen or keyboard. Operating-system users and processes give no stronger local separation without elevation or a second factor. ForgeFlow is local single-user software, not an authentication service. Users who run agents with unrestricted tool permissions should know an agent determined to forge an approval can do so. The audit log and the issue's trace footer make such a forgery visible, but they do not prevent it.

The confirmation guards against accidents, not against an adversary. The first implementation asked for a random one-time code; it was replaced by `y/N` after the live test because it added friction without security. Anything able to drive a real console can also read a code off the screen. Its one real benefit, ignoring keys typed or pasted before the prompt, is kept by flushing pending input (`FlushConsoleInputBuffer` on Windows, `tcflush` on POSIX) before asking.

### Handoff: GitHub becomes the source of truth

The handoff publishes one approved plan as **one issue** (one job, one issue, later one branch and one PR). The issue body is the approved snapshot verbatim, plus a trace footer and a hidden marker `forgeflow:trace job=<id> plan_sha256=<hash>`. The issue goes on the Project bound by `forgeflow init --project N`, in the Status option chosen there (default `Ready`). The binding lives in `.forge/runtime/config.json` (local, never committed). ForgeFlow never creates Projects, fields, options, labels or repository settings. Discovery notes are never published.

Idempotency and recovery:

1. The intent (plan hash, marker) is recorded in `github.handoff` before any write.
2. The issue is reused if linked. Otherwise it is found by marker among the repository's 100 newest issues, using a list query rather than the eventually consistent search API, and only then created.
3. The Project item is reused if the issue is already on the Project, and otherwise added.
4. Status is set only on an item with no status, so a status a human set on the board is never overridden. The item is then read back and recorded.

Gate A is re-verified before every write, a per-job OS lock stops concurrent handoffs, and missing scopes, permissions, fields, options or connectivity produce specific errors. A rerun finishes an interrupted handoff. Linking an existing issue (`--issue N`) posts the approved plan as a comment once, also found again by its marker, and never edits the issue body.

The job stays `plan_approved` with no worktree, and its builder stays `unassigned`. A lease cannot be taken for an unassigned builder. Checkpoint C assigns the builder, prepares the worktree and moves the job to `ready`.

`approve` runs the handoff right after recording the approval, in the human's terminal. If it fails, the approval stands and `forgeflow handoff <job>` retries safely.

### Short job references

People refer to jobs as `J1`, `J2`, …; the full id (`ff-YYYYMMDD-xxxxxx`) remains the identifier for directories, branches (`forge/<id>`), the event log and the GitHub trace marker. References are allocated under the state lock from a counter in `.forge/runtime/refs.json`. The counter is saved before a reference is used, so a crash may skip a number but never hands one out twice, and a deleted job keeps its number. The next number is never below one already on disk. Every command accepts either form, case-insensitively. Unknown references are refused with the known ones listed, and ambiguous ones (only possible through hand edits) are refused too. Jobs created before references existed receive theirs, in creation order, the next time a writing command runs; `status` stays read-only. References are local to one clone, which matches where job state lives (ADR 0003); anything shared across machines or published to GitHub uses the full id and the issue number. Plan tasks keep `T1`, `T2`, … inside each plan.

### Hosts: one runtime, thin adapters

`/forge` (`.claude/skills/forge/SKILL.md`) and `$forge` (`.agents/skills/forge/SKILL.md`) hold the conversation and call the same CLI commands; a test keeps their command tables identical. Neither adapter runs `approve`. Both tell the human to run it in their own terminal.

Product Management and Engineering skills are discovered, not assumed: `forgeflow skills --host claude-code|codex` reads `claude plugin list --json`, or for Codex its `config.toml` plugins plus plain skill folders (`<repo>/.agents/skills`, `~/.agents/skills`, `$CODEX_HOME/skills`). Skill folders are matched by the provenance in their `SKILL.md` front matter (`source-package`, `source-skill`), not by folder name. Each skill is reported under its canonical name (`product-management:write-spec`) and the exact name the host invokes it by (`$pm-write-spec` in Codex). The adapters load a listed skill only if it is also in their own session's skill list, and otherwise use short built-in fallback guidance.

## Verification in the real hosts (2026-10-25)

Claude Code 2.1.310 and Codex CLI 0.164.0 on Windows 11, with a disposable repository.

- **Claude Code** (`claude -p`, resumed for four turns):
  - `/forge idea` discovered and loaded `product-management:product-brainstorming`.
  - It recorded, rejected, superseded and then reinstated decisions.
  - `/forge design` loaded `engineering:testing-strategy` and produced a plan that passed `design check`. It then submitted the plan.
  - Asked to "approve it for me", it declined and gave the human-only instructions.
  - Told to run `forgeflow approve` directly, its Bash tool got exit 3 and the job stayed `awaiting_plan_approval`.
- **Codex** (`codex exec`, resumed):
  - First run, before the skills were installed: `$forge idea` found no Product Management or Engineering skills and used the fallback guidance. It recorded outcomes, designed and submitted a plan, and declined to approve.
  - After the owner installed Codex adaptations of the Engineering, Product Management and Productivity skills in `~/.agents/skills` (22 skills: `pm-*`, `eng-*`, `productivity-*`, each carrying its Anthropic provenance), `forgeflow skills --host codex` reports the same canonical skills as Claude Code. In a fresh run, `$forge idea` selected `$pm-brainstorm` from that output, loaded its `SKILL.md` and references, and ran discovery with it, without the fallback.
  - A direct `forgeflow approve` was refused.
  - **Finding:** with this machine's `[windows] sandbox = "elevated"`, Codex runs commands as the local user `CodexSandboxOffline`. That user cannot read a Python installed only for the current user, so neither `python` nor `py -3.13` runs ForgeFlow. The run above used `codex exec -c 'windows.sandbox="unelevated"'`, a per-invocation override. Installing Python 3.13 for all users is the alternative. ForgeFlow changes no Codex configuration. The owner later set `sandbox = "unelevated"`; a plain `codex exec -s workspace-write` then ran `py -3.13` (3.13.3) without any override.
- **Live GitHub handoff** (PDJ-E/ForgeFlow and ForgeFlow Development, authorised by the owner, with a synthetic, clearly labelled plan):
  - The owner approved job `ff-20261025-232896` at a console.
  - The handoff created issue #4, added it to the Project and set Status Ready. The job recorded the issue, the item and the handoff.
  - The handoff was re-run twice, once through a logging runner. It made five read-only queries and no write: no duplicate issue or item.
  - The job, created before short references, received `J1` on that run and resolved from `J1`, `j1` and its full id.
  - Issue #4 was then closed with a verification comment and its item archived.
  - The Project's own "Item closed" workflow then changed the archived item's Status to `Building` (`github-project-automation[bot]`, three seconds after the close). ForgeFlow made no write. No other issue, pull request, branch, Project field or setting changed.
- **Local channels:** `forgeflow approve` was refused from Git Bash with a pipe, with piped input, under `winpty` (stdin not a tty) and from the PowerShell tool. The PowerShell tool's stdin is `NUL`; there `sys.stdin.isatty()` returned `True`, but `GetConsoleMode` returned `False`.

## Consequences

- The plan, its version and hash, the approval and the issue are linked in both directions: the job records the issue and Project item, and the issue carries the job id and plan hash.
- The job schema is version 2 (plan version and submission time, approved version, handoff record). Version 1 records still load and are upgraded when next written.
- A Checkpoint A job could link an issue while still a draft; linking now requires an approved plan.
- GitHub Project workflows ("Item added to project", "Item closed") run after ForgeFlow and can change the Status it set. ForgeFlow never overrides a Status it finds already set, and it reports the Status it observes. After the live test, the owner disabled all seven built-in workflows on ForgeFlow Development (verified 2026-10-25). From Checkpoint C on, ForgeFlow alone moves Status (Ready → Building → QA → Review → Done, Blocked for exceptions) from verified job events; see the [roadmap](../roadmap.md).
- In the issue footer, ForgeFlow links the Project by its URL, because GitHub turns "#N" into a link to issue or pull request N. `design check` warns when a plan writes "Project #N".
- Approval needs a real terminal, which is the intended friction. A Claude Code `!` command or any agent tool call cannot approve.
- Windows `MAX_PATH`: a plan snapshot's path includes a 64-character hash, so a repository nested very deep (about 150 characters of root path or more) can exceed 260 characters unless Windows long paths are enabled.

## Revisit

Revisit if a stronger human-presence signal becomes practical, such as Windows Hello consent or a hardware key, or if ForgeFlow becomes multi-user. Also revisit if plans regularly outgrow one issue; GitHub sub-issues per task would then be the next step.
