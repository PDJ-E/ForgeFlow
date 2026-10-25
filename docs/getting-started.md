# Getting started — Checkpoint A (Windows first)

> **Status:** pre-alpha. Checkpoint A is a tested **execution harness**: environment inspection, durable job state, worktree isolation, provider detection, GitHub issue linkage and read-only status. It does **not** run Claude or Codex on your code, create issues or PRs, or merge anything. See [what is not implemented](#not-implemented-yet).

## Requirements

| Tool | Required | Notes |
| --- | --- | --- |
| Python 3.10+ | yes | 3.12+ recommended; 3.10 reaches end of life in October 2026. No third-party packages. |
| Git 2.31+ | yes | For `git rev-parse --path-format` and worktrees. |
| GitHub CLI (`gh`) | for GitHub linkage | Authenticated. Project discovery also needs the `read:project` scope. |
| Claude Code / Codex CLI | optional | Detected and reported only; neither is executed in Checkpoint A. |

ForgeFlow never installs, upgrades or configures any of these.

## Run from a checkout (PowerShell)

```powershell
git clone https://github.com/PDJ-E/ForgeFlow.git
cd ForgeFlow
python -m forgeflow doctor          # read-only environment report; exit 1 only if a required check fails
python -m forgeflow doctor --json   # machine-readable
python -m forgeflow doctor --offline  # skip checks that contact GitHub
python -m forgeflow status          # local jobs (read-only)
python -m forgeflow status <job-id> # one job, with recent events
```

Other repositories: inspect one with `python -m forgeflow --repo C:\path\to\repo doctor`. To get a `forgeflow` command, install into a virtual environment you control:

```powershell
py -3.13 -m venv .venv        # or: python -m venv .venv
.\.venv\Scripts\python -m pip install -e C:\Dev\ForgeFlow
.\.venv\Scripts\forgeflow --version
```

The console script is `forgeflow` because `forge` collides with other widely installed tools. The short names are the skills `/forge` and `$forge`, described below.

## `/forge` and `$forge`

| Host | File | Invocation |
| --- | --- | --- |
| Claude Code | `.claude/skills/forge/SKILL.md` | `/forge status`, `/forge doctor` |
| Codex CLI | `.agents/skills/forge/SKILL.md` | `$forge status`, `$forge doctor` |

Both adapters are **repository-scoped**: they are discovered when the client runs inside this repository. They only call `python -m forgeflow doctor|status`. They report every other operation as not implemented, and they never emulate one by hand. The Claude adapter sets `disable-model-invocation: true`, so it runs only when you type `/forge`. Installing the skills for other repositories (user-level or packaged) is a later checkpoint, and no existing user-level skills are touched.

### Verified invocation

Checked on 2026-10-24 with Claude Code 2.1.310 and Codex CLI 0.164.0 on Windows 11. Each probe was one short real model call.

- **Claude Code:** `claude -p "/forge status" --allowedTools "Bash(python -m forgeflow *)"` resolved the repository skill and ran exactly `python -m forgeflow status`.
- **Codex CLI:** `codex exec -s read-only --ephemeral '$forge status'` loaded the repository skill and ran exactly `python -m forgeflow status` in the read-only sandbox.

Pitfalls found while verifying, which Checkpoint C's provider runner must handle:

- **Git Bash rewrites `/` arguments.** Git for Windows (MSYS) converts arguments that start with `/`. Running `claude -p "/forge status"` from Git Bash delivers `C:/Program Files/Git/forge status` to Claude. Set `MSYS_NO_PATHCONV=1` for that command, or use PowerShell. Typing `/forge` inside an interactive Claude Code session is unaffected.
- **`codex exec` waits on an open stdin.** When stdin is a pipe rather than a terminal, Codex prints `Reading additional input from stdin...` and blocks. Close stdin (`< NUL` in cmd, `< /dev/null` in Git Bash). ForgeFlow's runner always uses an explicit stdin.
- In single-quoted shells, write `'$forge'` so the shell does not expand `$forge` as a variable.

## Running the tests

```powershell
python -m unittest discover -s tests -t . -v   # stdlib only
python -m pytest                                # equivalent, if pytest is installed
```

The suite creates throwaway git repositories under `%TEMP%` with an isolated git configuration (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_NOSYSTEM`), so your global hooks, signing and `autocrlf` settings neither affect it nor are affected by it.

| Test module | What is real | What is mocked |
| --- | --- | --- |
| `test_fsutil`, `test_proc` | Files, OS locks, child processes, process-tree kill | — |
| `test_jobs`, `test_store` | Store, locks, leases, transitions, evidence | Plan approvals are test fixtures (`method: test-fixture`) |
| `test_worktree` | git repositories, worktrees, branches, user's dirty files | `origin/main` is a local ref, not a network remote |
| `test_interruption` | A child process that owns a job and is **killed**; recovery and resume | — |
| `test_lifecycle` | Store, worktree, gates, rework bounds | **Provider execution, check results, GitHub responses and the PR link** |
| `test_github` | Remote parsing, store linkage | `gh` responses (fake runner) |
| `test_providers` | Task and policy validation | CLI output (scripted runner) |
| `test_environment_cli` | git, CLI output, read-only guarantees | `gh` responses, provider CLIs |

Opt-in tests that make **real, read-only** calls:

```powershell
$env:FORGEFLOW_LIVE_GITHUB = "1"     # gh api graphql against a public issue (default cli/cli#2)
$env:FORGEFLOW_LIVE_ISSUE = "owner/name#N"   # optional target override
$env:FORGEFLOW_LIVE_PROVIDERS = "1"  # claude/codex --version, --help, auth status (no model call)
python -m unittest tests.test_github tests.test_providers -v
```

Static checks, as run in CI, from a virtual environment:

```powershell
python -m pip install ruff==0.17.2 mypy==2.4.0
python -m ruff check .; python -m ruff format --check .; python -m mypy
```

## Where state lives

```text
<repo>/.forge/runtime/jobs/<job-id>/job.json      snapshot (atomic, revisioned)
<repo>/.forge/runtime/jobs/<job-id>/events.jsonl  audit trail
<repo>/.forge/runtime/jobs/<job-id>/evidence/     machine-readable results
<repo>/.forge/worktrees/<job-id>/                  isolated worktree on branch forge/<job-id>
```

Both directories write their own `.gitignore` (`*`) and never appear in `git status`. Run logs and evidence stay local. The design is in [ADR 0003](decisions/0003-runtime-state-and-worktrees.md).

## GitHub Project access

`doctor` reports whether the configured GitHub Project is reachable. It needs a token with `read:project`:

```powershell
gh auth refresh -s read:project
```

ForgeFlow never creates or modifies Projects, fields, issues or labels in Checkpoint A.

## Not implemented yet

`init`, `idea`, `design`, `approve` (Checkpoint B); `run`, `review`, `rework`, `resume` with real providers (Checkpoint C); knowledge sources and Obsidian (Checkpoint D); `learn` (after v0.1). These CLI subcommands exist only to say so: they exit with code 2 and change nothing.
