# ADR 0005 — Build, checks, independent review, bounded rework and delivery

- **Status:** Accepted for Checkpoint C (implemented and tested; see [getting started](../getting-started.md#run-an-approved-job-checkpoint-c))
- **Date:** 2026-10-26
- **Resolves:** [architecture](../architecture.md) open questions 7 (the cross-provider review contract and escalation policy) and 5 for Status transitions; refines 6 (usage and cost reporting)
- **Related:** [ADR 0001](0001-github-projects-local-state.md), [ADR 0003](0003-runtime-state-and-worktrees.md), [ADR 0004](0004-planning-workflow-and-gate-a.md), [MVP scope](../mvp-scope.md)

## Context

Checkpoints A and B left an approved plan on a GitHub issue, in Status Ready, with a store that already enforced Gate A at dispatch, round-bound non-mocked check evidence, Gate B against a GitHub-observed draft PR, one execution slot, OS-locked leases and provider PIDs with start times. Real providers raised `ProviderError`. Checkpoint C turns that into a working path:

**approved job → chosen builder → isolated build → deterministic checks → independent review → bounded rework → draft PR → human merge → done.**

The constraints did not change: Windows first, stdlib-only Python 3.13, no daemon or database, both hosts behind one implementation, and nothing installed or reconfigured on the user's behalf.

## Decision

### Providers: verified invocations, not assumed ones

Every flag below was checked against the installed CLIs (Claude Code 2.1.311 and Codex CLI 0.164.0, Windows 11) with small real calls in scratch repositories before the code was written. `forgeflow run` re-checks each provider before anything is dispatched: installed, logged in, every option it relies on present in `--help`, and a sandbox that works for this workspace. A failure is a `ProviderUnavailableError` naming the reason, raised before a worktree is created; an unavailable reviewer is never skipped.

| Role | Claude Code | Codex CLI |
| --- | --- | --- |
| Builder | `claude -p --output-format json --no-session-persistence --restricted --strict-mcp-config --permission-prompts none --tools Read,Edit,Write,Glob,Grep --permission-mode acceptEdits` | `codex exec --json --ephemeral -C <worktree> -s workspace-write -c approval_policy="never" -o <file> -` |
| Reviewer | the same with `--tools Read,Glob,Grep --permission-mode dontAsk --json-schema <schema>` | `-s read-only --output-schema <file> --skip-git-repo-check` |

What the probes showed:

- **Claude builder:** a write inside the working directory succeeds; a write to `..\outside` is denied and listed in `permission_denials`. With `--restricted` and no Bash tool the builder cannot run commands at all, and user, project and local settings, hooks and (with `--strict-mcp-config`) MCP servers are not loaded.
- **Claude reviewer:** has no edit tool; its answer arrives in `structured_output`.
- **Codex reviewer:** `read-only` rejected an attempted edit (`patch rejected: writing is blocked by read-only sandbox`); the schema-shaped answer is the `-o` file.
- **Codex builder:** `workspace-write` writes in a linked worktree under `.forge/worktrees`. In a repository under `%TEMP%` it refused every write and every command (`windows unelevated restricted-token sandbox cannot enforce split writable root sets directly; refusing to run unsandboxed`). ForgeFlow's preflight refuses that location on Windows, and the same message from a run is reported as `blocked`, not retried and not worked around.
- **Codex MCP:** Codex starts the user's MCP servers and plugins (Obsidian, GitHub, context7, ...). `-c mcp_servers={}` has no effect, and per-server `enabled=false` overrides fail for plugin-provided servers. ForgeFlow does not edit the Codex configuration, so this exposure is a **recorded limitation**, repeated in every PR a Codex builder or reviewer worked on.

The prompt travels on stdin (Windows limits a command line to about 32,000 characters; a plan may be 60,000). The provider subprocess runs through `proc.run_tracked`, which reports its PID once it sits in its kill-on-close Job Object (Windows) or process group (POSIX); the lease records the PID and start time before the provider does any work and clears them when the process tree is gone. A run that exceeds its timeout (default 1,800 s) is killed with its whole tree.

A provider built with a substitute `command` (the test double `tests/helpers/fake_cli.py`) reports every result as `mocked: true`, like `MockProvider`.

### The builder writes; ForgeFlow commits

Providers never commit, push or open PRs. After a builder exits, ForgeFlow asks git (`status --porcelain -z`, renames with both names), not the provider, what changed:

- a path outside `build.allowed_paths` or inside `build.protected_paths` (default `.forge/*`, `.github/workflows/*`) is a **scope violation**: nothing is committed, nothing is reverted, the job goes to `needs_human`;
- otherwise ForgeFlow stages and commits the changes itself (`forgeflow: J3 build round 2`, with the job id and plan hash in the message; repository hooks run as usual) and records a `build-commit` evidence;
- no change at all is a failed build in the first round, and "nothing more to do" in a rework round.

This one rule replaced several provider-specific ones: the Codex sandbox cannot write `.git` of a linked worktree anyway, a Claude builder has no command tool, and the scope check judges the actual diff whatever the provider claims.

### Checks come from a commit the builder cannot change

The checks are configured in the committed file `.forge/forgeflow.toml` (`[[checks.commands]]` with `name` and `argv`, `{python}` for the interpreter running ForgeFlow, `timeout_seconds`, and the `[build]` path patterns). It is read with `git show` from the **job's base commit**, or, when the base has none yet, from the **main checkout's HEAD**, and never from the builder's worktree, so a builder cannot weaken what judges its work. The source (path, commit, which of the two) is recorded with every check evidence. Without a configuration ForgeFlow refuses to build; it never invents checks.

`run_checks` (Checkpoint A) runs the commands itself on the round's clean commit and records argv, exit code, duration and output tails per command. Nothing reuses check results across rounds: delivery, review and Gate B each require the *current* round's passing, non-mocked checks on the commit at HEAD.

### Independent review on a snapshot

The reviewer never runs in the builder's worktree. `run_reviewer` exports exactly the validated commit with `git archive` into a fresh temporary directory (no `.git`, no worktree link), gives the reviewer the approved plan, its acceptance criteria, the diff from the base and the check summary, and deletes the directory afterwards. Afterwards ForgeFlow re-verifies that the build worktree did not move. A reviewer that ignored its read-only instructions could only change a copy.

**Access is proven, not assumed.** Each snapshot gets a file `.forgeflow-review-token` holding a fresh random value that appears nowhere in the prompt; the prompt names only the file. The reviewer must return that value as `snapshot_token` and state `inspection_complete`. A wrong or missing token, `inspection_complete: false`, or a sandbox refusal makes the review **`blocked`**: no verdict, no approval, no rework round (an infrastructure problem is not the builder's to fix), and the job goes to a human with "Nothing was approved". This was added after the live run below, where a reviewer answered a well-formed "approve" without having read the snapshot.

**Only ForgeFlow's own record counts.** After comparing the token, ForgeFlow writes `review_access: {version: 1, verified: true|false, method: snapshot-token}` into the review evidence, as the last key of the payload, outside the provider's answer, so nothing a reviewer returns can supply it. One validator, `dispatch.verified_approval`, decides whether an approval may be used: the latest review of the current round, of exactly the validated commit, approving, with that record at a supported version and `verified: true`. Both consumers use it: resume (skip a review already done) and `delivery.deliver` (before any push or PR write). Evidence without the record, from before this check, or with another version, counts as **unverified**: resume reviews the same commit again (a fresh review that cannot read the snapshot blocks, without rework), and delivery refuses. Historical evidence is never rewritten; `forgeflow status` shows each review's access as verified or NOT verified.

The review answer must match a strict JSON schema (`verdict`, `summary`, `findings` with `severity` `blocking|major|minor`, `file`, `issue`, `suggestion`, plus `snapshot_token` and `inspection_complete`). ForgeFlow, not the reviewer, decides the verdict from the findings: changes are requested only for a blocking or major finding. A "request-changes" that lists only minor notes is delivered with those notes in the PR's limitations; an "approve" that lists a blocking finding is reworked. A reviewer that fails, times out, returns no valid answer or cannot run escalates to a human with "Nothing was approved"; there is no fallback reviewer and no assumed approval.

Cross-provider review (`--builder claude --reviewer codex` or the reverse) is the documented default for significant work; the same provider in its read-only mode is allowed when the human chooses it.

### Bounded rework and escalation

`attempts.max_rework` (default 2) bounds automatic rounds after failed checks, review findings or a human rework request on the draft PR. Past it, the job goes to `needs_human` (Blocked on the board) with the reason. Each round's builder prompt carries what the previous round left to fix, rebuilt from the evidence files (failed commands with the end of their output, actionable findings, the human's note), so a resumed or continued job sees the same feedback.

A human continues an escalated job with `forgeflow run <job> --reason "..."` (optionally with another builder or reviewer). That decision grants a fresh, still bounded budget (`attempts.rework` resets, `attempts.human_resumes` counts the decisions, `attempts.build` keeps the total) and the reason reaches the builder.

### Delivery and Gate B

`forgeflow.delivery.deliver` runs only while reviewing, under the lease, for the commit whose checks passed **and** whose review approved it, and only when no evidence of the round is mocked: a simulated build is never pushed. It records its intent (`github.delivery`) first, pushes the commit to `refs/heads/forge/<job-id>` on `origin` without force (a rejected push stops for a human), then reuses the linked PR, else the one GitHub lists for the branch, else opens a **draft**. A closed or merged PR for the branch is a human matter, never replaced by a second one. A PR a human marked ready is turned back into a draft before Gate B, because Gate B is the human's decision on exactly this commit.

The PR body carries the approved scope and "Closes #N", the builder's summary and diffstat, the check table (commit, command, result, exit code, duration, marked as real evidence), the reviewer's verdict and findings, the rounds, the known limitations (minor findings, provider notes, the Codex MCP limitation, where the checks ran) and the traceability (job id, plan hash, branch, base, validated commit, trace marker). Local paths are removed before publishing.

ForgeFlow then waits until GitHub reports the validated commit as the head of the draft and enters `awaiting_merge_approval` through `JobStore.request_merge_approval`, which checks everything again (ADR 0003). ForgeFlow has no merge operation. `forgeflow sync` records `merged` only through `JobStore.record_merge`, when GitHub reports the PR merged with that head commit.

### The Project Status: one table, level-triggered

`forgeflow.board.STATUS_FOR_STATE` is the only mapping from job state to Project Status:

| Job state | Status |
| --- | --- |
| `plan_approved`, `ready` | Ready (the option chosen at `init`) |
| `building` | Building |
| `verifying` | QA |
| `reviewing`, `awaiting_merge_approval` | Review |
| `needs_human` | Blocked |
| `merged` | Done |
| `interrupted`, `cancelled`, `draft`, `awaiting_plan_approval` | unchanged |

A store transition listener calls `board.sync` after every committed transition, outside the state lock. The sync derives the Status from the job's *current* state, reads the item, writes only when it differs and reads it back, so repeating it or running it after an interruption converges on the same board (`forgeflow sync` does it on demand). A failed sync never undoes or blocks a transition; it is recorded in `github.board` and retried by the next one. The owner disabled the Project's own workflows (ADR 0004), so ForgeFlow is the only writer of Status on its items; a Status dragged by hand is overwritten and the event log keeps the old value. ForgeFlow never closes an issue; "Closes #N" in a merged PR is GitHub acting on the human's merge. A missing option is reported, never created.

The roadmap placed this mapping in the store's transition side effects. It lives in its own module, called by a store listener, because side effects run under the state lock and GitHub calls must not.

### Recovery

`run_job` starts by calling `JobStore.recover` when the record shows a lease or a worker state, and refuses while a worker holds the lease, while the recorded provider still runs (on POSIX it may outlive a killed orchestrator), or when the lease is from another host. It then re-enters the interrupted phase: an interrupted build is dispatched again in the same worktree, with its partial work still there and a note in the prompt; interrupted checks run again; an interrupted review runs again unless the commit already has an approving review this round, in which case only delivery is retried. A delivery that fails on GitHub or a push leaves the job reviewing, and the released lease marks it interrupted, so `forgeflow resume` retries the delivery idempotently instead of rebuilding.

A review interrupted mid-way is now resumed by the job's **builder** lease, because the orchestrator owns the job throughout and the reviewer is its read-only subprocess. A reviewer lease (ADR 0003) is still accepted there and still cannot enter `building`.

### Records

The job schema is version 3: `github.delivery` (marker, branch, pushed commit, PR number), `github.board` (state, Status read back, time, error) and `attempts.human_resumes`. Versions 1 and 2 still load and are upgraded on the next write. Evidence kinds added: `build-commit`, `scope-violation`, `review`, `review-error`, `human-feedback`; `review` and `build-commit` carry the commit they are about.

## Verification

Automated (unittest, 328 tests, all in disposable local repositories; GitHub mocked by a stateful fake that reads PR heads from a real local bare repository):

- `test_pipeline`: every phase, rework after failed checks and after review findings, the limit, a human continuing with a reason, scope violations kept uncommitted, an unavailable reviewer blocking before dispatch, a failing reviewer never counting as approval, a reviewer unable to touch the build, the Codex sandbox refusal, Gate A and the handoff first, explicit providers, missing checks, and that a simulated build is never pushed.
- `test_delivery`: real commits, real checks and real pushes; one draft PR, idempotent after a crash right after creating it, no force push over a diverged branch, no second PR for a closed one, rework redelivered to the same PR as a draft, merge recorded only when GitHub reports it, the board at Review then Done.
- `test_recovery`: a real child orchestrator killed while its provider works. On Windows the provider dies with it (Job Object); on POSIX it is reported as an orphan until stopped. No second provider starts while one runs; the partial work is committed by the resumed round; a killed review resumes in review without rebuilding.
- `test_board`, `test_repoconfig`, `test_providers` (the real provider classes driving a fake CLI as a real child process: argv, stdin, PID tracking, timeout kill, parsing), `test_cli_run`.

Live, with real providers: see [the live demonstration](#live-demonstration-2026-10-26).

### Live demonstration (2026-10-26)

Authorised by the owner for PDJ-E/ForgeFlow and Project ForgeFlow Development, with a small real fix: job **J2** (`ff-20261026-4cc2ad`), "Doctor header shows the version instead of a checkpoint".

- **Gate A:** the owner ran `forgeflow approve J2` in their own terminal (method `interactive-console`, plan v1, sha256 `f411ca2e…`). The handoff created [issue #7](https://github.com/PDJ-E/ForgeFlow/issues/7) on the Project in Ready.
- **Run:** `forgeflow run J2 --builder claude --reviewer codex`, from this branch's code, against base `ec696d4` (`origin/main`). The checks came from `.forge/forgeflow.toml` at the main checkout's HEAD (`daf9bbf`), because the base had none yet; the evidence records that.
- **Build (real Claude Code 2.1.311):** about 22 s; changed `forgeflow/cli.py` and `tests/test_environment_cli.py` only; it said it ran no checks because it has no shell, as designed. ForgeFlow committed `64c5374` on `forge/ff-20261026-4cc2ad`. Usage: 60,245 input / 2,109 output tokens, API-equivalent estimate USD 0.13.
- **Checks (real, run by ForgeFlow on `64c5374`):** unit tests exit 0 (75 s), `ruff check` 0, `ruff format --check` 0, `mypy` 0.
- **Review (real Codex CLI 0.164.0, read-only): UNVERIFIED.** It answered "approve", no findings, but said "additional file inspection was unavailable due to access restrictions": it judged only the diff and check results in its prompt, and ForgeFlow accepted that as an approval. Two defects, both fixed after the run:
  1. *Snapshot ACL:* `tempfile.mkdtemp` gives the directory an owner-only ACL on Windows (Python 3.13), which Codex's sandbox identity (`CodexSandboxUsers`) cannot read. Reproduced with ForgeFlow's reviewer arguments ("Access is denied"); snapshots now use a plain `mkdir` with the inherited ACL (`test_the_review_snapshot_keeps_inherited_permissions`).
  2. *False approval:* the verdict was derived from the findings alone, so an uninspected review passed. Reviews now must prove access with the snapshot token, and an incomplete review is `blocked` without rework (`test_an_approval_without_snapshot_access_is_not_an_approval`, `test_review_access_must_be_proven`; a sandbox refusal blocks a reviewer as well as a builder).
  The build, the commit and the four check results of J2 are unaffected. The PR's review section and the job's `review` evidence still hold that approval, and it stays **unverified** until a fresh verified review is recorded for J2: it has no `review_access` record, so `forgeflow status J2` shows "snapshot access NOT verified", and neither resume nor delivery would accept it. J2 is not re-reviewed, because the state machine offers no review-only round after Gate B (a `forgeflow rework J2` would rebuild). The separate live access check below does not change J2's stored evidence.
- **Access re-verified live after the fix**, on J2's commit `64c5374` through ForgeFlow's exact reviewer path (`dispatch.review_in_snapshot` → the real provider class, real prompt, real argv, PID tracked), without changing J2's record: Codex returned the unprompted token and `inspection_complete: true` and approved; Claude Code did the same and cited `forgeflow/__init__.py`, a file outside the diff.
- **Delivery:** pushed `64c5374` (not forced), opened **draft** [PR #8](https://github.com/PDJ-E/ForgeFlow/pull/8) to `main`; GitHub reported that exact head; the job entered `awaiting_merge_approval`. The Project item went Ready → Building → QA → Review; issue #7 stayed open. All seven CI jobs passed on the PR. Nothing was merged; Gate B is the owner's.
- Verified on GitHub afterwards with `gh`, not from ForgeFlow's output: PR draft, open, head `64c5374…`, base `main`; issue open; Status Review.

**Reverse combination, locally (real CLIs, no GitHub):** ForgeFlow's `CodexProvider` built in a linked worktree of a disposable repository outside `%TEMP%` (it fixed a seeded bug and added a unittest; PID recorded and cleared; results `mocked: false`), the configured unittest check passed, and `ClaudeCodeProvider` reviewed a snapshot read-only and approved. The build worktree was unchanged afterwards. This exercised both provider classes in the other roles, but not the whole pipeline with GitHub.

Not exercised live: rework rounds and escalation with real providers (covered by tests with simulated providers), `rework`, and `sync` after a real merge (Gate B is pending).

## Consequences

- A builder cannot run tests itself when Claude Code builds (no command tool); it learns about failures from the next round's prompt. Codex can run commands inside its sandbox. Both are judged only by ForgeFlow's own check run.
- Codex runs with the user's MCP servers and plugins (see above). Choose Claude as the builder when that exposure is not acceptable.
- Codex cannot build in a repository under `%TEMP%` on Windows with the unelevated sandbox.
- Token counts are reported as measured when the CLI reports them (Claude: input including cache reads and writes; Codex: `turn.completed` usage). Claude's `total_cost_usd` is recorded as an **API-equivalent estimate**, not a charge: on a subscription nothing is billed per call. Codex reports no cost.
- Checks run on the local machine; the repository's CI runs separately on the PR and is not a substitute for Gate B or vice versa.
- The configuration fallback to the main checkout's HEAD lets a repository adopt ForgeFlow on a feature branch before `.forge/forgeflow.toml` reaches its base. The evidence names which commit configured the checks.
- POSIX containment is unchanged from ADR 0003: a provider that calls `setsid()` can outlive its orchestrator; recovery refuses until it is stopped.

## Revisit

Revisit when a provider offers a per-invocation way to disable MCP servers, when Codex's Windows sandbox supports workspaces under `%TEMP%`, or if a review contract richer than severity-tagged findings proves necessary in practice.
