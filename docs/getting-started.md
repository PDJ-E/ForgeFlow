# Getting started — Checkpoints A and B (Windows first)

> **Status:** pre-alpha. Checkpoint A is a tested **execution harness**: environment inspection, durable job state, worktree isolation, provider detection, GitHub issue linkage and read-only status. Checkpoint B adds the **planning workflow**: interactive discovery, one engineering plan per job, human plan approval (Gate A) and publishing the approved plan as a GitHub issue on your Project. ForgeFlow still does **not** run Claude or Codex on your code, create branches or PRs, or merge anything. See [what is not implemented](#not-implemented-yet).

## Requirements

| Tool | Required | Notes |
| --- | --- | --- |
| Python 3.13+ | yes | CI tests 3.13 and 3.15. No third-party runtime packages. |
| Git 2.31+ | yes | For `git rev-parse --path-format` and worktrees. |
| GitHub CLI (`gh`) | for GitHub linkage | Authenticated. Project discovery also needs the `read:project` scope. |
| Claude Code / Codex CLI | optional | Hosts for `/forge` and `$forge`. ForgeFlow never runs them as builders yet. |

ForgeFlow never installs, upgrades or configures any of these.

## Choose the interpreter explicitly

No virtual environment is required: ForgeFlow has no runtime dependencies and runs straight from a checkout. Several Pythons are often installed side by side, though, so **name the interpreter rather than relying on `python`**:

| Platform | Command prefix | Why |
| --- | --- | --- |
| Windows | `py -3.13` | The Python launcher picks the installed 3.13 regardless of `PATH` order. |
| Linux / macOS | `python3.13` | Or any `python3` that reports 3.13+. |

Check what a prefix resolves to before using it:

```powershell
py -3.13 -c "import sys; print(sys.executable, sys.version)"
py -0p    # every Python the launcher knows; '*' marks the default for a bare `py`
```

On the pilot machine (2026-10-25), `python` still resolves to **3.10.0** because `Python310` comes first on `PATH`, while `py` and `py -3.13` resolve to 3.13.17. Under 3.10, ForgeFlow stops with `ForgeFlow requires Python 3.13 or newer, but <path> is Python 3.10.0` and names the command to use. Nothing has to be uninstalled.

## Run from a checkout (PowerShell)

```powershell
git clone https://github.com/PDJ-E/ForgeFlow.git
cd ForgeFlow
py -3.13 -m forgeflow doctor          # read-only environment report; exit 1 only if a required check fails
py -3.13 -m forgeflow doctor --json   # machine-readable
py -3.13 -m forgeflow doctor --offline  # skip checks that contact GitHub
py -3.13 -m forgeflow status          # local jobs (read-only)
py -3.13 -m forgeflow status <job-id> # one job, with recent events
```

`doctor` prints the interpreter it ran under (`python ... (C:\...\python.exe)`). To inspect another repository: `py -3.13 -m forgeflow --repo C:\path\to\repo doctor`.

A `forgeflow` console command is optional. If you want one, install the checkout for that interpreter yourself, for example `py -3.13 -m pip install --user -e C:\Dev\ForgeFlow`. ForgeFlow never installs itself.

The console script is `forgeflow` because `forge` collides with other widely installed tools. The short names are the skills `/forge` and `$forge`, described below.

## `/forge` and `$forge`

| Host | File | Invocation |
| --- | --- | --- |
| Claude Code | `.claude/skills/forge/SKILL.md` | `/forge idea <text>`, `/forge design <job>`, `/forge status`, ... |
| Codex CLI | `.agents/skills/forge/SKILL.md` | `$forge idea <text>`, `$forge design <job>`, `$forge status`, ... |

Both adapters are **repository-scoped**: they are discovered when the client runs inside this repository. They hold the conversation and call the same `python -m forgeflow ...` commands (a test keeps their command tables identical), rerunning once with `py -3.13` (Windows) or `python3.13` if `python` is older than 3.13. Neither ever runs `approve`. They report `run`, `review`, `rework`, `resume` and `learn` as not implemented and never emulate them by hand. The Claude adapter sets `disable-model-invocation: true`, so it runs only when you type `/forge`. Installing the skills for other repositories (user-level or packaged) is a later checkpoint, and no existing user-level skills are touched.

### Verified invocation

Checked on 2026-10-24 with Claude Code 2.1.310 and Codex CLI 0.164.0 on Windows 11. Each probe was one short real model call.

- **Claude Code:** `claude -p "/forge status" --allowedTools "Bash(python -m forgeflow *)"` resolved the repository skill and ran exactly `python -m forgeflow status`.
- **Codex CLI:** `codex exec -s read-only --ephemeral '$forge status'` loaded the repository skill and ran exactly `python -m forgeflow status` in the read-only sandbox.

Pitfalls found while verifying, which Checkpoint C's provider runner must handle:

- **Git Bash rewrites `/` arguments.** Git for Windows (MSYS) converts arguments that start with `/`. Running `claude -p "/forge status"` from Git Bash delivers `C:/Program Files/Git/forge status` to Claude. Set `MSYS_NO_PATHCONV=1` for that command, or use PowerShell. Typing `/forge` inside an interactive Claude Code session is unaffected.
- **`codex exec` waits on an open stdin.** When stdin is a pipe rather than a terminal, Codex prints `Reading additional input from stdin...` and blocks. Close stdin (`< NUL` in cmd, `< /dev/null` in Git Bash). ForgeFlow's runner always uses an explicit stdin.
- In single-quoted shells, write `'$forge'` so the shell does not expand `$forge` as a variable.

## Plan an idea (Checkpoint B)

The flow is **idea → discovery → design → your approval → GitHub issue on the Project**. Nothing in it writes code or starts a builder. Design rationale and the threat model: [ADR 0004](decisions/0004-planning-workflow-and-gate-a.md).

### 1. Bind the GitHub Project (once per clone)

```powershell
py -3.13 -m forgeflow init                # checks the setup and lists Projects; writes nothing
py -3.13 -m forgeflow init --project 2    # binds Project #2; approved work goes to Status "Ready"
```

`init --project` reads the Project and checks that its **Status** field has the option ForgeFlow will use (`--ready-status NAME` picks another). It records the binding in `.forge/runtime/config.json` (local, never committed) and changes nothing on GitHub. Writing to the Project needs the `project` token scope: `gh auth refresh -s project`.

### 2. Discuss the idea

In Claude Code type `/forge idea <your idea>`; in Codex, `$forge idea <your idea>`. The agent asks a few proportional questions, compares approaches and records only the **outcomes** with `forgeflow idea add/mark`: requirements, decisions, rejected options and why, open questions. Resume any time with `/forge idea resume <job>` (it reads `forgeflow idea show <job>`). If Anthropic's Product Management skills are available in the host (`forgeflow skills --host claude-code|codex`), the agent uses them; otherwise it uses short built-in guidance.

The same steps work without an agent:

```powershell
py -3.13 -m forgeflow idea new "Add --version" --idea "I can't tell which build I'm running"
py -3.13 -m forgeflow idea add <job> decision "Read the installed version" --status accepted
py -3.13 -m forgeflow idea add <job> alternative "Hardcode it" --status rejected --note "drifts"
py -3.13 -m forgeflow idea show <job> --all
```

### 3. Design the plan

`/forge design <job>` (or `forgeflow design new <job>`) creates **one** plan file, `.forge/runtime/jobs/<job>/plan.md`, prefilled from the discovery. Edit it with the agent, which uses the Engineering skills when available. `forgeflow design check <job>` lists what is missing. When you are content: `forgeflow design submit <job>` pins those exact bytes as plan version *n*. Changing your mind later: `forgeflow design revise <job> --reason "..."`, then submit again (a new version).

The approved plan is published **verbatim** as the issue body. Keep private notes in the discovery, which never leaves your machine.

### 4. Approve it yourself (Gate A)

Open **your own** terminal (PowerShell or Windows Terminal) in the repository and run:

```powershell
py -3.13 -m forgeflow approve <job>
```

It prints the plan, its version and sha256, the target repository and its visibility, the Project and Status, and what approval does **not** allow. Type the one-time code it shows to approve; anything else cancels. It then publishes the issue and Project item (step 5). Add `--no-handoff` to only record the approval, or `--issue N` to link an existing issue instead of creating one.

`approve` refuses (exit code 3, nothing recorded) unless both its input and output are an interactive console. So it cannot be run by Claude Code's Bash or PowerShell tools, by `codex exec`, through a pipe, or from Claude Code's `!` prefix. That is deliberate. A changed plan, or a resubmission while you read, is refused as well. The limits of this protection are in [ADR 0004](decisions/0004-planning-workflow-and-gate-a.md#gate-a-a-human-at-an-interactive-console).

### 5. Hand off to GitHub

`approve` runs the handoff for you. `py -3.13 -m forgeflow handoff <job>` re-runs it safely: it finds what an interrupted run already did (by a trace marker in the issue) and never duplicates the issue or the Project item. `--dry-run` shows what would be published. The result is one issue in your repository, on the bound Project, in the Ready status (a status you already set on the board is left alone). `forgeflow status <job>` shows the plan version, the approval, the issue and the Project item. The job stays `plan_approved` with no branch, worktree or builder until Checkpoint C.

### Host notes

- **Claude Code:** verified end to end on 2026-10-25 (see [ADR 0004](decisions/0004-planning-workflow-and-gate-a.md#verification-in-the-real-hosts-2026-10-25)). Approve in a separate terminal window, not with `!`.
- **Codex on Windows:** with `[windows] sandbox = "elevated"` in `~/.codex/config.toml`, Codex runs commands as the local user `CodexSandboxOffline`, which cannot read a Python installed only for your user. `$forge` then cannot run ForgeFlow at all. Either start Codex with `-c 'windows.sandbox="unelevated"'` (verified), or install Python 3.13 for all users. ForgeFlow does not change your Codex configuration. `init` and `handoff` need network access, which Codex's sandbox may block; run them in your own terminal (approval already performs the handoff).
- **Windows path length:** plan snapshots are named by their 64-character hash. If the repository's path is very long (roughly 150+ characters), enable Windows long paths or use a shorter location.

## Running the tests

`unittest` (standard library) is the official runner; nothing needs to be installed.

```powershell
py -3.13 -m unittest discover -s tests -t . -v   # the official suite, stdlib only
py -3.13 -m pytest                                # optional convenience, if pytest is installed
```

The suite creates throwaway git repositories under `%TEMP%` with an isolated git configuration (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`), so your global hooks, signing and `autocrlf` settings neither affect it nor are affected by it.

| Test module | What is real | What is mocked |
| --- | --- | --- |
| `test_fsutil`, `test_proc` | Files, OS locks, child processes, process-tree kill | — |
| `test_jobs`, `test_store` | Store, locks, leases, transitions, evidence | Plan approvals are test fixtures (`method: test-fixture`) |
| `test_worktree` | git repositories, worktrees, branches, user's dirty files | `origin/main` is a local ref, not a network remote |
| `test_interruption` | A child process that owns a job and is **killed**; recovery and resume | — |
| `test_lifecycle` | Store, worktree, gates, rework bounds, check commands | **Provider execution, GitHub responses and the PR link** |
| `test_review_regressions` | One class per finding of the PR #3 review: worktrees, child processes, leases, check commands, git | GitHub responses (fake `gh`), provider execution (`MockProvider`) |
| `test_python_version` | `pyproject.toml`, CI matrix, and `py -3.10` if installed | — |
| `test_github` | Remote parsing, store linkage | `gh` responses (fake runner) |
| `test_providers` | Task and policy validation | CLI output (scripted runner) |
| `test_environment_cli` | git, CLI output, read-only guarantees, adapter consistency | `gh` responses, provider CLIs |
| `test_discovery`, `test_design` | Store, files, versions, hashes, interrupted writes | — |
| `test_approval` | Gate A: ceremony, refusals, races, a **real non-interactive child process**, dispatch prevention | The human at the console (`ScriptedConsole`, patched in only by tests) |
| `test_handoff` | Store, idempotency and recovery logic | **All GitHub calls** (`tests/fake_github.py`, a stateful fake `gh`) |
| `test_hostskills` | Plugin and skill discovery | `claude plugin list` output, a temporary Codex home |

Opt-in tests that make **real, read-only** calls:

```powershell
$env:FORGEFLOW_LIVE_GITHUB = "1"     # gh api graphql: a public issue (default cli/cli#2) and PR cli/cli#1
$env:FORGEFLOW_LIVE_ISSUE = "owner/name#N"   # optional target override
$env:FORGEFLOW_LIVE_PROVIDERS = "1"  # claude/codex --version, --help, auth status (no model call)
py -3.13 -m unittest tests.test_github tests.test_handoff tests.test_providers -v
# test_handoff's live part reads a Project: FORGEFLOW_LIVE_PROJECT=owner#N (default PDJ-E#2)
```

### Developer tools (optional)

Ruff and mypy are pinned in the `lint` dependency group of `pyproject.toml`; they are never runtime dependencies. If they are missing, install them **for your user and that interpreter only** (pip 25.1+ reads the group; nothing is installed system-wide and no venv is needed):

```powershell
py -3.13 -m pip install --user --group lint        # ruff==0.17.2, mypy==2.4.0
py -3.13 -m pip install --user --group dev         # the same plus pytest
py -3.13 -m ruff check .; py -3.13 -m ruff format --check .; py -3.13 -m mypy
```

ForgeFlow never installs or upgrades these itself. CI installs the same `lint` group on Python 3.13.

## Where state lives

```text
<repo>/.forge/runtime/jobs/<job-id>/job.json      snapshot (atomic, revisioned)
<repo>/.forge/runtime/jobs/<job-id>/events.jsonl  audit trail
<repo>/.forge/runtime/config.json                  bound GitHub Project (forgeflow init)
<repo>/.forge/runtime/jobs/<job-id>/discovery.json outcomes of the idea discussion (never published)
<repo>/.forge/runtime/jobs/<job-id>/plan.md        the job's plan, edited during design
<repo>/.forge/runtime/jobs/<job-id>/plans/        byte-exact snapshots of submitted plan versions
<repo>/.forge/runtime/jobs/<job-id>/evidence/     machine-readable results
<repo>/.forge/worktrees/<job-id>/                  isolated worktree on branch forge/<job-id>
```

Both directories write their own `.gitignore` (`*`) and never appear in `git status`. Run logs and evidence stay local. The design is in [ADR 0003](decisions/0003-runtime-state-and-worktrees.md).

## GitHub Project access

`doctor` reports whether the configured GitHub Project is reachable. It needs a token with `read:project`:

```powershell
gh auth refresh -s read:project
```

Verified on 2026-10-25: the token's scopes include `read:project`, and `doctor` reports `Projects visible: #2 ForgeFlow Development`. (On 2026-10-24 the scope was missing and the lookup failed with GraphQL `INSUFFICIENT_SCOPES`. That is no longer a blocker.)

ForgeFlow never creates or modifies Projects, fields, options, labels or repository settings. Since Checkpoint B it creates exactly one issue per approved plan (or comments once on an issue you link), adds it to the bound Project and sets its Status, only after Gate A. That needs the `project` scope (`gh auth refresh -s project`).

## Not implemented yet

`run`, `review`, `rework`, `resume` with real providers, and builder assignment and worktree preparation for approved jobs (Checkpoint C); knowledge sources and Obsidian (Checkpoint D); `learn` (after v0.1). These CLI subcommands exist only to say so: they exit with code 2 and change nothing.
