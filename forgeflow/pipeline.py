"""Run an approved job: build, deterministic checks, independent review, bounded rework, PR.

``run_job`` is the one driver for a job's execution, whether it starts fresh, resumes after an
interruption or continues after a human looked at an escalation. Every step is decided from
the job record, never from memory, so a rerun after a crash picks up exactly where the record
says the job is::

    plan_approved --(worktree)--> ready --> building --> verifying --> reviewing --> delivery
                                              ^    failed checks |     | changes requested
                                              +------------------+-----+   (bounded rework)
    any unresolved failure, or the rework limit --> needs_human (Blocked on the board)
    delivery --> awaiting_merge_approval (Gate B, the human merges) --> merged (sync)

Guarantees, each enforced below or by the store rather than by prompts:

* **Gate A** is re-verified when the worktree is prepared and at every builder dispatch.
* **One job, one writer:** the store's execution slot, an OS-locked lease, and the provider's
  PID in that lease. A second ``run`` of the same job fails with DuplicateWorkerError; a lost
  orchestrator whose provider still runs blocks recovery until it is stopped.
* **Explicit providers:** the human names the builder and the reviewer. Both are checked
  (installed, logged in, required CLI options, a sandbox that works for this workspace)
  *before* anything is dispatched; an unavailable reviewer is a blocker, never a skipped
  review or an assumed approval.
* **Fresh evidence:** checks run on the round's committed result; a review is of the commit
  whose checks passed; delivery verifies both and the PR head again.
* **Bounded rework:** at most ``attempts.max_rework`` (default 2) automatic rounds, then a
  human. A review with no blocking or major finding is not reworked, and a commit that already
  has an approving review is not reviewed again after an interruption.
* **No lost work:** nothing is reset, stashed, cleaned or force-pushed. Interrupted work stays in
  the worktree and is part of the next round.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from forgeflow import repoconfig
from forgeflow.board import BoardListener
from forgeflow.dispatch import (
    latest,
    passing_checks_for_round,
    run_builder,
    run_reviewer,
    verified_approval,
)
from forgeflow.errors import (
    DuplicateWorkerError,
    EvidenceError,
    ForgeFlowError,
    InvalidTransitionError,
    LeaseError,
    ProviderError,
    ValidationError,
)
from forgeflow.github import GitHubClient
from forgeflow.gitrepo import default_base_ref, resolve_commit
from forgeflow.jobs import UNASSIGNED_PROVIDER, Job, JobState
from forgeflow.providers import REAL_PROVIDERS, Provider, build_task
from forgeflow.store import JobStore, LeaseHandle
from forgeflow.worktree import WORKTREES_RELATIVE, WorktreeManager, prepare_job_worktree

S = JobState
ProviderFactory = Callable[[str], Provider]
Reporter = Callable[[str], None]
_CRITERION = re.compile(r"^\s*[-*] \[[ xX]\] (.+?)\s*$", re.MULTILINE)
_FEEDBACK_TAIL = 1500  # characters of a failed check's output passed back to the builder


def real_provider(name: str) -> Provider:
    try:
        return REAL_PROVIDERS[name]()
    except KeyError as exc:
        known = ", ".join(sorted(REAL_PROVIDERS))
        raise ValidationError(f"unknown provider {name!r} (known: {known})") from exc


@dataclass(frozen=True)
class RunOptions:
    builder: str | None = None
    reviewer: str | None = None
    reason: str = ""  # required to continue a job that waits for a human
    builder_model: str | None = None
    reviewer_model: str | None = None
    timeout_seconds: int = 1800


@dataclass
class Context:
    store: JobStore
    job_id: str
    options: RunOptions
    builder: Provider
    reviewer: Provider
    config: repoconfig.RepoConfig
    criteria: tuple[str, ...]
    github: GitHubClient | None
    report: Reporter
    lease: LeaseHandle | None = None

    def held(self) -> LeaseHandle:
        if self.lease is None:
            raise LeaseError("no lease is held")
        return self.lease

    @property
    def token(self) -> str:
        return self.held().token

    def load(self) -> Job:
        return self.store.load(self.job_id)


def acceptance_criteria(plan_text: str, fallback: str) -> tuple[str, ...]:
    """The plan's ``- [ ]`` criteria, in order and without duplicates."""
    found = list(dict.fromkeys(m.group(1) for m in _CRITERION.finditer(plan_text)))
    return tuple(found) or (fallback,)


def run_job(
    store: JobStore,
    job_id: str,
    options: RunOptions,
    *,
    providers: ProviderFactory = real_provider,
    github: GitHubClient | None = None,
    board: BoardListener | None = None,
    report: Reporter = print,
) -> Job:
    """Start, resume or continue the job until it waits for a human (Gate B or an escalation)."""
    job = store.load(job_id)
    store.require_own_repository(job)
    _refuse_unrunnable(job)
    if job.lease is not None or job.state in (S.BUILDING, S.VERIFYING, S.REVIEWING):
        recovery = store.recover(job_id)
        if recovery.outcome in ("running", "orphaned-worker", "foreign-host"):
            raise DuplicateWorkerError(
                f"{job.label} is still owned ({recovery.outcome}): {recovery.detail}"
            )
        report(f"recovered {job.label}: {recovery.detail}")
        job = store.load(job_id)
    builder_name, reviewer_name = _choose(job, options)
    builder, reviewer = providers(builder_name), providers(reviewer_name)
    worktree_path = store.repo_root / WORKTREES_RELATIVE / job.job_id
    # Everything that could make the run unsafe is checked before anything changes.
    builder.preflight("builder", worktree_path)
    reviewer.preflight("reviewer", None)
    store.verify_plan_approval(job_id)
    if job.github.issue is None:
        raise InvalidTransitionError(
            f"{job.label} has no GitHub issue yet; publish the approved plan first "
            f"(forgeflow handoff {job.ref or job.job_id})"
        )
    base_sha = job.worktree.base_sha if job.worktree else _base_sha(store, job)
    config = repoconfig.load(store.repo_root, base_sha)
    if job.state is S.NEEDS_HUMAN and not options.reason.strip():
        raise ValidationError(
            f'{job.label} waits for a human; say why it may continue with --reason "..."'
        )
    if (job.policy.builder, job.policy.reviewer) != (builder_name, reviewer_name):
        if job.state is S.INTERRUPTED:
            raise ValidationError(
                f"{job.label} was interrupted with builder {job.policy.builder} and reviewer "
                f"{job.policy.reviewer}; resume it with those (or let a human escalate it)"
            )
        job = store.assign_providers(job_id, builder_name, reviewer_name)
        report(f"builder {builder_name}, reviewer {reviewer_name}")

    plan_text = store.approved_plan_text(job)
    ctx = Context(
        store,
        job_id,
        options,
        builder,
        reviewer,
        config,
        acceptance_criteria(plan_text, job.title),
        github,
        report,
    )
    if board is not None and board not in store.listeners:
        store.listeners.append(board)
    try:
        board_sync(board, job)
        if job.state is S.PLAN_APPROVED:
            prepare_job_worktree(store, WorktreeManager(store.repo_root), job_id)
            job = store.transition(job_id, S.READY)
            report(f"prepared worktree {worktree_path} on branch forge/{job.job_id}")
        elif job.state is S.NEEDS_HUMAN:
            # The worktree is re-verified (never recreated or cleaned) before continuing.
            prepare_job_worktree(store, WorktreeManager(store.repo_root), job_id)
            job = store.transition(job_id, S.READY, reason=options.reason)
            report(f"continuing {job.label} after human review: {options.reason}")
        return _drive(ctx, _first_phase(job))
    finally:
        if board is not None and board in store.listeners:
            store.listeners.remove(board)


def request_rework(
    store: JobStore,
    job_id: str,
    note: str,
    *,
    providers: ProviderFactory = real_provider,
    github: GitHubClient | None = None,
    board: BoardListener | None = None,
    report: Reporter = print,
) -> Job:
    """A human asks for another round on a delivered draft PR (from Gate B), with a note."""
    if not note.strip():
        raise ValidationError('say what should change: --reason "..."')
    job = store.load(job_id)
    if job.state is not S.AWAITING_MERGE_APPROVAL:
        raise InvalidTransitionError(
            f"{job.label} is {job.state.value}; rework is requested on a delivered draft PR "
            "(awaiting_merge_approval)"
        )
    if job.attempts.rework >= job.attempts.max_rework:
        raise InvalidTransitionError(
            f"the rework limit ({job.attempts.max_rework}) is reached; continue by hand or cancel"
        )
    store.record_evidence(job_id, "human-feedback", "requested", {"note": note}, mocked=False)
    return run_job(
        store,
        job_id,
        RunOptions(builder=job.policy.builder, reviewer=job.policy.reviewer),
        providers=providers,
        github=github,
        board=board,
        report=report,
    )


def board_sync(board: BoardListener | None, job: Job) -> None:
    if board is not None:
        board(job)


def _refuse_unrunnable(job: Job) -> None:
    if job.state in (S.DRAFT, S.AWAITING_PLAN_APPROVAL):
        raise InvalidTransitionError(
            f"{job.label} is {job.state.value}: Gate A first. A human approves the plan with "
            f"`forgeflow approve {job.ref or job.job_id}` in their own terminal."
        )
    if job.is_terminal:
        raise InvalidTransitionError(f"{job.label} is {job.state.value}; nothing to run")
    if job.state is S.AWAITING_MERGE_APPROVAL and not _has_human_feedback(job):
        raise InvalidTransitionError(
            f"{job.label} waits at Gate B for the owner's merge decision on PR "
            f"#{job.github.pull_request.number if job.github.pull_request else '?'}. After a "
            "merge run `forgeflow sync`; to change the PR run `forgeflow rework --reason ...`."
        )


def _has_human_feedback(job: Job) -> bool:
    note = latest(job, "human-feedback")
    return note is not None and note.build_round == job.attempts.build


def _choose(job: Job, options: RunOptions) -> tuple[str, str]:
    builder = options.builder or (
        job.policy.builder if job.policy.builder != UNASSIGNED_PROVIDER else None
    )
    reviewer = options.reviewer or job.policy.reviewer
    if builder is None or reviewer is None:
        raise ValidationError(
            "choose the builder and the reviewer explicitly: --builder claude|codex "
            "--reviewer claude|codex (the reviewer may be the other provider)"
        )
    return builder, reviewer


def _base_sha(store: JobStore, job: Job) -> str:
    return resolve_commit(store.repo_root, default_base_ref(store.repo_root, job.repo.base_branch))


def _first_phase(job: Job) -> str:
    if job.state is S.INTERRUPTED:
        origin = job.interrupted_from
        return {S.BUILDING: "build", S.VERIFYING: "verify", S.REVIEWING: "review"}[
            origin or S.BUILDING
        ]
    return "build"  # ready, or awaiting_merge_approval with a human rework request


def _drive(ctx: Context, phase: str | None) -> Job:
    store = ctx.store
    lease = store.acquire_lease(ctx.job_id, "builder", ctx.builder.name)
    ctx.lease = lease
    try:
        with lease:
            while phase is not None:
                phase = {"build": _build, "verify": _verify, "review": _review}[phase](ctx)
            return ctx.load()
    finally:
        ctx.lease = None


def _escalate(ctx: Context, reason: str) -> None:
    ctx.store.transition(ctx.job_id, S.NEEDS_HUMAN, lease_token=ctx.token, reason=reason)
    ctx.report(f"needs a human: {reason}")


def _can_rework(job: Job) -> bool:
    return job.attempts.rework < job.attempts.max_rework


# --- build ---------------------------------------------------------------------------------


def _build(ctx: Context) -> str | None:
    job = ctx.load()
    task = build_task(
        ctx.store,
        ctx.job_id,
        role="builder",
        acceptance_criteria=ctx.criteria,
        allowed_paths=ctx.config.allowed_paths,
        protected_paths=ctx.config.protected_paths,
        validation_commands=ctx.config.argv_list,
        timeout_seconds=ctx.options.timeout_seconds,
        feedback=feedback_for(ctx.store, job),
        model=ctx.options.builder_model,
    )
    round_no = job.attempts.build + 1
    ctx.report(f"round {round_no}: building with {ctx.builder.name}")
    message = (
        f"forgeflow: {job.ref or job.job_id} build round {round_no}\n\n{job.title}\n\n"
        f"Builder: {ctx.builder.name}\nJob: {job.job_id}\n"
        f"Plan-sha256: {task.plan_sha256}\n"
    )
    try:
        result = run_builder(ctx.store, ctx.held(), ctx.builder, task, commit_message=message)
    except ProviderError as exc:
        if ctx.load().state is S.NEEDS_HUMAN:  # run_builder escalated already
            ctx.report(f"needs a human: builder failed: {exc}")
            return None
        raise
    ctx.report(f"builder {result.status}: {result.summary[:200]}")
    return "verify" if ctx.load().state is S.VERIFYING else None


def feedback_for(store: JobStore, job: Job) -> tuple[str, ...]:
    """What the previous round left to fix, rebuilt from the record (so resumes see it too)."""
    interrupted = job.state is S.INTERRUPTED
    previous = job.attempts.build - 1 if interrupted else job.attempts.build
    items: list[str] = []
    if interrupted:
        items.append(
            "Your previous run was interrupted. Its partial, uncommitted work is still in the "
            "worktree: review it and finish the job."
        )
    for evidence in job.evidence:
        if evidence.build_round != previous:
            continue
        try:
            payload = store.evidence_payload(job, evidence)
        except EvidenceError:
            items.append(
                f"{evidence.kind} evidence for round {previous} at {evidence.path} "
                "could not be read; details unavailable."
            )
            continue
        if evidence.kind == "checks" and evidence.status == "failed":
            for command in payload.get("commands", []):
                if command.get("ok"):
                    continue
                output = (command.get("stderr_tail") or "") + (command.get("stdout_tail") or "")
                items.append(
                    f"Check {command.get('name') or ' '.join(command.get('argv', []))!r} failed "
                    f"(exit {command.get('returncode')}, timed out {command.get('timed_out')}). "
                    f"End of its output:\n{output[-_FEEDBACK_TAIL:]}"
                    if "error" not in command
                    else f"Check {command.get('name')!r} could not run: {command['error']}"
                )
            if not payload.get("worktree_unchanged", True):
                items.append(
                    "The checks changed files or commits; checks must not modify the tree."
                )
        elif evidence.kind == "review" and evidence.status == "request-changes":
            items += [f"Reviewer finding: {f}" for f in payload.get("findings", [])]
        elif evidence.kind == "human-feedback":
            items.append(f"The owner asked for: {payload.get('note', '')}")
    if job.state is S.READY and job.attempts.human_resumes:
        escalation = _last_reason(store, job, "transition:needs_human")
        note = _last_reason(store, job, "transition:ready")
        items.append(
            f"The job was escalated to a human ({escalation or 'no reason recorded'}). The "
            f"human let it continue and said: {note or '(nothing)'}"
        )
    return tuple(items)


def _last_reason(store: JobStore, job: Job, event: str) -> str:
    for record in reversed(store.events(job.job_id)):
        if record.get("event") == event:
            details = record.get("details") or {}
            return str(details.get("reason", "")) if isinstance(details, dict) else ""
    return ""


# --- verify --------------------------------------------------------------------------------


def _verify(ctx: Context) -> str | None:
    store = ctx.store
    job = ctx.load()
    if job.state is S.INTERRUPTED:
        job = store.transition(ctx.job_id, S.VERIFYING, lease_token=ctx.token)
    ctx.report(f"round {job.attempts.build}: running {len(ctx.config.checks)} check(s)")
    try:
        evidence = store.run_checks(
            ctx.job_id,
            ctx.token,
            ctx.config.argv_list,
            timeout=ctx.config.check_timeout,
            names=ctx.config.names,
            source=dict(ctx.config.source),
        )
    except InvalidTransitionError as exc:  # e.g. uncommitted changes appeared
        _escalate(ctx, f"the checks could not run: {exc}")
        return None
    ctx.report(f"checks {evidence.status} on {evidence.commit[:12] if evidence.commit else '?'}")
    if evidence.status == "passed":
        store.transition(ctx.job_id, S.REVIEWING, lease_token=ctx.token)
        return "review"
    if _can_rework(ctx.load()):
        return "build"
    _escalate(ctx, f"checks still fail after {job.attempts.rework} rework round(s)")
    return None


# --- review --------------------------------------------------------------------------------


def checks_summary(store: JobStore, job: Job) -> str:
    checks = latest(job, "checks")
    if checks is None:
        return ""
    lines = [f"Commit {checks.commit}: {checks.status} (real run by ForgeFlow, not mocked)"]
    try:
        payload = store.evidence_payload(job, checks)
    except EvidenceError:
        lines.append(f"Evidence {checks.path} could not be read; command details unavailable.")
        return "\n".join(lines)
    for command in payload.get("commands", []):
        lines.append(
            f"- {command.get('name') or ' '.join(command.get('argv', []))}: "
            f"{'passed' if command.get('ok') else 'failed'} (exit {command.get('returncode')})"
        )
    return "\n".join(lines)


def _review(ctx: Context) -> str | None:
    store = ctx.store
    job = ctx.load()
    if job.state is S.INTERRUPTED:
        job = store.transition(ctx.job_id, S.REVIEWING, lease_token=ctx.token)
    checks = passing_checks_for_round(job)
    if verified_approval(store, job, checks.commit) is not None:
        ctx.report("this commit already has a verified approving review; not reviewing it again")
        _deliver(ctx)
        return None
    earlier = latest(job, "review")
    if earlier is not None and earlier.status == "approve" and earlier.commit == checks.commit:
        # E.g. recorded before reviews had to prove snapshot access: never reused, never altered.
        ctx.report("the earlier approval of this commit is unverified; reviewing it again")
    commit = checks.commit[:12] if checks.commit else "?"
    ctx.report(f"round {job.attempts.build}: reviewing {commit} with {ctx.reviewer.name}")
    task = build_task(
        store,
        ctx.job_id,
        role="reviewer",
        acceptance_criteria=ctx.criteria,
        validation_commands=ctx.config.argv_list,
        timeout_seconds=ctx.options.timeout_seconds,
        model=ctx.options.reviewer_model,
    )
    try:
        result, _ = run_reviewer(
            store, ctx.held(), ctx.reviewer, task, checks_summary=checks_summary(store, job)
        )
    except (ProviderError, ValidationError) as exc:
        store.record_evidence(
            ctx.job_id,
            "review-error",
            "failed",
            {"reviewer": ctx.reviewer.name, "error": str(exc)},
            mocked=not ctx.reviewer.real,
        )
        _escalate(
            ctx,
            f"reviewer {ctx.reviewer.name} could not review: {exc}. Nothing was approved; "
            "choose an available reviewer with --reviewer",
        )
        return None
    if result.status != "succeeded" or result.verdict is None:
        _escalate(
            ctx,
            f"reviewer {ctx.reviewer.name} {result.status}: {result.summary[:300]}. Nothing was "
            "approved; choose an available reviewer with --reviewer",
        )
        return None
    ctx.report(f"review: {result.verdict} ({len(result.findings)} finding(s))")
    if result.verdict == "approve":
        _deliver(ctx)
        return None
    if _can_rework(ctx.load()):
        return "build"
    _escalate(
        ctx,
        f"the reviewer still requests changes after {job.attempts.rework} rework round(s): "
        + "; ".join(result.findings)[:600],
    )
    return None


# --- deliver -------------------------------------------------------------------------------


def _deliver(ctx: Context) -> None:
    from forgeflow.delivery import deliver, round_mocked_kinds

    job = ctx.load()
    mocked = round_mocked_kinds(job)
    if mocked:
        _escalate(
            ctx,
            f"simulated build: this round's {', '.join(mocked)} evidence is mocked, so "
            "ForgeFlow does not push or open a pull request",
        )
        return None
    if ctx.github is None:
        raise ForgeFlowError("delivery needs GitHub access")
    # A GitHub or push failure leaves the job reviewing; releasing the lease marks it
    # interrupted, and `forgeflow resume` retries the delivery without reviewing again.
    result = deliver(ctx.store, ctx.held(), ctx.github)
    for action in result.actions:
        ctx.report(action)
    return None
