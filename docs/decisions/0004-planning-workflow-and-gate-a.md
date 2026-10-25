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
- It shows the full plan from the snapshot, the version, the sha256, the task summary, what approval authorises (the target repository, its visibility and the Project/Status) and what it does not authorise. The human must type a fresh one-time code.
- The decision is committed against the job revision that was displayed (compare-and-swap), after the store re-hashes the plan file and the snapshot. An edit or resubmission made while the human reads is refused.
- The record names the method `interactive-console` and the approved version. The store refuses any other method when entering `plan_approved`, and again at every later boundary: handoff, `ready` and each `building` dispatch. So an approval written by hand into `job.json` with another method stops at the next boundary.

**Threat model.** This separates a human at their own terminal from an agent acting through its ordinary non-interactive tools: the Claude Code Bash/PowerShell tools, `codex exec`, piped input, or a direct library call to `transition()`. It also stops model-generated claims such as "the user approved this", which are not inputs to ForgeFlow at all.

It does **not** stop a same-user process that deliberately defeats it. Such a process could drive a pseudo-console (ConPTY, `script`, `pexpect`), call the private recording function, write `job.json` with the expected method, or automate the screen or keyboard. Operating-system users and processes give no stronger local separation without elevation or a second factor. ForgeFlow is local single-user software, not an authentication service. Users who run agents with unrestricted tool permissions should know an agent determined to forge an approval can do so. The audit log and the issue's trace footer make such a forgery visible, but they do not prevent it.

The one-time code guards against accidental approval (a stray Enter, a pasted script); it is not the security property.

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

### Hosts: one runtime, thin adapters

`/forge` (`.claude/skills/forge/SKILL.md`) and `$forge` (`.agents/skills/forge/SKILL.md`) hold the conversation and call the same CLI commands; a test keeps their command tables identical. Neither adapter runs `approve`. Both tell the human to run it in their own terminal.

Product Management and Engineering skills are discovered, not assumed: `forgeflow skills --host claude-code|codex` reads `claude plugin list --json` or Codex's `config.toml` and plugin cache. The adapters load a listed skill only if it is also in their own session's skill list, and otherwise use short built-in fallback guidance.

## Verification in the real hosts (2026-10-25)

Claude Code 2.1.310 and Codex CLI 0.164.0 on Windows 11, with a disposable repository.

- **Claude Code** (`claude -p`, resumed for four turns):
  - `/forge idea` discovered and loaded `product-management:product-brainstorming`.
  - It recorded, rejected, superseded and then reinstated decisions.
  - `/forge design` loaded `engineering:testing-strategy` and produced a plan that passed `design check`. It then submitted the plan.
  - Asked to "approve it for me", it declined and gave the human-only instructions.
  - Told to run `forgeflow approve` directly, its Bash tool got exit 3 and the job stayed `awaiting_plan_approval`.
- **Codex** (`codex exec`, resumed):
  - `$forge idea` found no Product Management or Engineering plugins and used the fallback guidance. It recorded outcomes, designed and submitted a plan, and declined to approve.
  - A direct `forgeflow approve` was refused.
  - **Finding:** with this machine's `[windows] sandbox = "elevated"`, Codex runs commands as the local user `CodexSandboxOffline`. That user cannot read a Python installed only for the current user, so neither `python` nor `py -3.13` runs ForgeFlow. The run above used `codex exec -c 'windows.sandbox="unelevated"'`, a per-invocation override. Installing Python 3.13 for all users is the alternative. ForgeFlow changes no Codex configuration.
- **Local channels:** `forgeflow approve` was refused from Git Bash with a pipe, with piped input, under `winpty` (stdin not a tty) and from the PowerShell tool. The PowerShell tool's stdin is `NUL`; there `sys.stdin.isatty()` returned `True`, but `GetConsoleMode` returned `False`.

## Consequences

- The plan, its version and hash, the approval and the issue are linked in both directions: the job records the issue and Project item, and the issue carries the job id and plan hash.
- The job schema is version 2 (plan version and submission time, approved version, handoff record). Version 1 records still load and are upgraded when next written.
- A Checkpoint A job could link an issue while still a draft; linking now requires an approved plan.
- Approval needs a real terminal, which is the intended friction. A Claude Code `!` command or any agent tool call cannot approve.
- Windows `MAX_PATH`: a plan snapshot's path includes a 64-character hash, so a repository nested very deep (about 150 characters of root path or more) can exceed 260 characters unless Windows long paths are enabled.

## Revisit

Revisit if a stronger human-presence signal becomes practical, such as Windows Hello consent or a hardware key, or if ForgeFlow becomes multi-user. Also revisit if plans regularly outgrow one issue; GitHub sub-issues per task would then be the next step.
