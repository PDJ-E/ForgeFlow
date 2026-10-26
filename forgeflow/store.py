"""Durable local job store with guarded transitions, one execution slot and worker leases.

Layout (inside the main checkout, self-ignored by git)::

    .forge/runtime/
        .gitignore            "*"  - nothing here is ever committed
        state.lock            OS lock serialising every read-modify-write
        jobs/<job-id>/
            job.json          current snapshot (atomic replace, revisioned)
            events.jsonl      append-only audit trail of changes and decisions
            lease.lock        OS lock held by the live worker process for the job
            plans/v<N>.md     byte-exact snapshot of plan version N (hash in job.json); builders
                              get this text. Older jobs may have plans/<sha256>.md instead.
            evidence/         machine-readable results (checks, provider output, reviews)

Concurrency model:

* ``state.lock`` is held only for the few milliseconds of a read-modify-write. ``job.json`` also
  carries a ``revision`` so callers holding a stale copy can be rejected (compare-and-swap).
* ``lease.lock`` is held for as long as a worker owns the job. Because it is an OS lock it is
  released automatically if the worker process dies, which is how an interruption is detected
  without trusting PIDs. The lease record in ``job.json`` names the holder for diagnostics and
  the provider subprocess (PID plus start time). While that subprocess still runs, the lease
  record is never cleared, so no second builder can start next to it.
* A job sitting in a worker state without a live lease is, by definition, interrupted.

Gate A is reached only through ``forgeflow.approval.approve_plan``, which captures the decision
from a human at an interactive console; ``transition()`` refuses PLAN_APPROVED outright, and every
later boundary accepts only approvals captured that way (see ADR 0004).

Delivery states are reached only through methods that verify, not through caller claims:
``request_merge_approval`` (Gate B) needs checks that ``run_checks`` itself ran on the current
build round's commit, and a draft PR that GitHub reports with exactly that head commit;
``record_merge`` needs GitHub to report that PR merged with that head commit.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

from forgeflow import fsutil
from forgeflow.errors import (
    ActiveJobError,
    ConcurrencyError,
    DuplicateWorkerError,
    ForgeFlowError,
    InvalidTransitionError,
    LeaseError,
    StaleLeaseError,
    ValidationError,
)
from forgeflow.gitrepo import git_ok, porcelain_status
from forgeflow.jobs import (
    HUMAN_APPROVAL_METHOD,
    JOB_ID_PATTERN,
    REWORK_SOURCES,
    SLOT_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    UNASSIGNED_PROVIDER,
    WORKER_STATES,
    Approval,
    Evidence,
    Job,
    JobState,
    Lease,
    Plan,
    Policy,
    PullRequestLink,
    PullRequestObservation,
    Repo,
    Worktree,
    branch_for,
    new_job_id,
    utc_now,
    validate_job_id,
)
from forgeflow.proc import (
    Runner,
    hostname,
    pid_alive,
    process_started_at,
    redact_credentials,
    run_command,
)
from forgeflow.worktree import WORKTREES_RELATIVE, verify_job_worktree

S = JobState
RUNTIME_RELATIVE = Path(".forge") / "runtime"
_EVIDENCE_KIND = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_OUTPUT_TAIL = 4000  # characters of each check command's output kept as evidence

# A lease may be taken only where a worker legitimately starts or resumes.
_LEASE_START_STATES = frozenset({S.READY, S.INTERRUPTED, S.AWAITING_MERGE_APPROVAL})
# States whose entry ends the recorded lease.
_LEASE_ENDING_STATES = TERMINAL_STATES | {S.NEEDS_HUMAN, S.AWAITING_MERGE_APPROVAL}
# worker_status() answers that must keep the job owned.
_WORKER_BLOCKING = frozenset({"running", "unverifiable"})
# States in which a human may choose (or change) the job's builder and reviewer.
_ASSIGNABLE_STATES = frozenset({S.PLAN_APPROVED, S.READY, S.NEEDS_HUMAN})
# JSON documents beside job.json that edit_document() may replace.
_SIDE_DOCUMENTS = frozenset({"discovery.json"})


@dataclass(frozen=True)
class RecoveryReport:
    job_id: str
    outcome: str  # running | orphaned-worker | foreign-host | recovered | nothing-to-recover
    detail: str


class PullRequestSource(Protocol):
    """Where pull request facts come from: ``github.GitHubClient`` (or a fake in tests)."""

    def view_pull_request(self, repo: str, number: int) -> PullRequestObservation: ...


def worker_status(lease: Lease | None) -> str:
    """State of the lease's provider subprocess: none | running | gone | unverifiable.

    ``running`` means the same process (PID and start time) is alive. ``unverifiable`` means a
    process with that PID is alive but cannot be matched to the recorded one; callers that
    could start a second writer must treat it like ``running``.
    """
    if lease is None or not lease.worker_pid:
        return "none"
    started = process_started_at(lease.worker_pid)
    if started is not None:
        if lease.worker_started_at is None:
            return "unverifiable"
        return "running" if started == lease.worker_started_at else "gone"
    return "unverifiable" if pid_alive(lease.worker_pid) else "gone"


TransitionListener = Callable[[Job], None]


class JobStore:
    def __init__(self, runtime_dir: Path, lock_timeout: float = 10.0) -> None:
        self.runtime_dir = runtime_dir
        self.jobs_dir = runtime_dir / "jobs"
        self.lock_timeout = lock_timeout
        # Called with the job after every committed transition, outside the state lock (e.g. the
        # GitHub Project Status sync). A listener's failure never undoes or blocks a transition.
        self.listeners: list[TransitionListener] = []

    @classmethod
    def for_repo(cls, repo_root: Path, lock_timeout: float = 10.0) -> JobStore:
        return cls(repo_root / RUNTIME_RELATIVE, lock_timeout=lock_timeout)

    @property
    def repo_root(self) -> Path:
        """The main checkout this store belongs to (the store lives in ``.forge/runtime``)."""
        return self.runtime_dir.parent.parent

    # --- paths -----------------------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / validate_job_id(job_id)

    def _job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def _events_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "events.jsonl"

    def _plan_snapshot(self, job_id: str, version: int) -> Path:
        # Short names keep deep repositories under Windows MAX_PATH; the hash lives in job.json.
        return self.job_dir(job_id) / "plans" / f"v{version}.md"

    def _legacy_plan_snapshot(self, job_id: str, digest: str) -> Path:
        # Snapshots written before plan versions had files of their own were named by hash.
        return self.job_dir(job_id) / "plans" / f"{digest}.md"

    def _lease_lock(self, job_id: str) -> fsutil.FileLock:
        return fsutil.FileLock(self.job_dir(job_id) / "lease.lock", timeout=0)

    def _state_lock(self) -> fsutil.FileLock:
        return fsutil.FileLock(self.runtime_dir / "state.lock", timeout=self.lock_timeout)

    def _ensure_layout(self) -> None:
        fsutil.write_self_ignoring_gitignore(self.runtime_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def require_own_repository(self, job: Job) -> None:
        """Refuse a record that names another repository than the one holding this store."""
        if fsutil.normalized(job.repo.root) != fsutil.normalized(self.repo_root):
            raise ValidationError(
                f"job {job.job_id} names repository {job.repo.root}, but this store belongs to "
                f"{self.repo_root}"
            )

    def job_worktree(self, job: Job, runner: Runner = run_command) -> Path:
        """The job's own registered worktree, verified against git; never another checkout."""
        self.require_own_repository(job)
        return verify_job_worktree(job, runner)

    # --- reads (never create files, never take locks) ----------------------------------------

    def exists(self, job_id: str) -> bool:
        return self._job_file(job_id).is_file()

    def load(self, job_id: str) -> Job:
        path = self._job_file(job_id)
        try:
            data = fsutil.read_json(path)
        except FileNotFoundError as exc:
            raise ValidationError(f"no such job: {job_id}") from exc
        except ValueError as exc:
            raise ValidationError(f"job record {path} is not valid JSON: {exc}") from exc
        job = Job.from_dict(data)
        if job.job_id != job_id:
            raise ValidationError(f"job record {path} claims id {job.job_id}")
        return job

    def list_jobs(self) -> tuple[list[Job], list[str]]:
        """All readable jobs (oldest first) plus a description of every unreadable record."""
        jobs: list[Job] = []
        problems: list[str] = []
        if not self.jobs_dir.is_dir():
            return jobs, problems
        for entry in sorted(self.jobs_dir.iterdir()):
            if not entry.is_dir():
                continue
            try:
                jobs.append(self.load(entry.name))
            except ValidationError as exc:
                problems.append(f"{entry.name}: {exc}")
        jobs.sort(key=lambda j: (j.created_at, j.job_id))
        return jobs, problems

    def events(self, job_id: str) -> list[dict[str, Any]]:
        records, _ = fsutil.read_jsonl(self._events_file(job_id))
        return records

    def _read_plan(self, job: Job) -> bytes:
        if not job.plan.path:
            raise InvalidTransitionError(f"job {job.job_id} has no plan document")
        root = Path(job.repo.root)
        plan_file = fsutil.ensure_within(root, root / job.plan.path)
        try:
            return plan_file.read_bytes()
        except FileNotFoundError as exc:
            raise InvalidTransitionError(f"plan document {job.plan.path} is missing") from exc

    def plan_snapshot_text(self, job_id: str, digest: str, version: int | None) -> str:
        """A submitted plan's byte-exact snapshot, verified against its hash, as UTF-8 text.

        The snapshot is ``plans/v<version>.md``. Only when that file does not exist (or the
        version is unknown, as in records older than plan versions) is the earlier
        ``plans/<sha256>.md`` name read. A ``v<version>.md`` that does not match its hash is
        refused outright, never replaced by another file.
        """
        candidates = [self._plan_snapshot(job_id, version)] if version else []
        candidates.append(self._legacy_plan_snapshot(job_id, digest))
        for path in candidates:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                raise InvalidTransitionError(f"the plan snapshot {path.name} was modified")
            return data.decode("utf-8")
        raise InvalidTransitionError("the plan snapshot is missing; resubmit the plan for approval")

    def plan_digest(self, job: Job) -> str:
        """sha256 of the plan file in the main checkout as it is on disk right now."""
        return hashlib.sha256(self._read_plan(job)).hexdigest()

    def approved_plan_text(self, job: Job) -> str:
        """Exactly the bytes that were approved, from the job's snapshot, decoded as UTF-8."""
        approval = job.plan.approval
        if approval is None or approval.gate != "plan":
            raise InvalidTransitionError("Gate A: the plan has not been approved")
        return self.plan_snapshot_text(
            job.job_id, approval.subject_sha256, approval.subject_version
        )

    # --- creation and generic mutation -----------------------------------------------------

    def create(self, title: str, repo: Repo, policy: Policy) -> Job:
        """Create a DRAFT job. Drafts are discussion-only and never dispatch code."""
        if not title.strip():
            raise ValidationError("title must not be empty")
        if fsutil.normalized(repo.root) != fsutil.normalized(self.repo_root):
            raise ValidationError(
                f"repository {repo.root} is not the one this store belongs to ({self.repo_root})"
            )
        with self._state_lock():
            self._ensure_layout()
            # Older jobs get their references first, so numbering follows creation order.
            self._assign_missing_refs_locked()
            ref = self._next_ref_locked()
            for _ in range(5):
                job_id = new_job_id()
                try:
                    self.job_dir(job_id).mkdir()
                    break
                except FileExistsError:
                    continue
            else:  # pragma: no cover - 16M ids per day
                raise ValidationError("could not allocate a unique job id")
            now = utc_now()
            draft = Job(
                job_id=job_id,
                ref=ref,
                title=title.strip(),
                state=S.DRAFT,
                repo=repo,
                policy=policy,
                created_at=now,
                updated_at=now,
                revision=1,
            )
            job = Job.from_dict(draft.to_dict())  # validate (and redact) before the first write
            fsutil.atomic_write_json(self._job_file(job_id), job.to_dict())
            self._append_event(job, "created", None, details={"title": job.title, "ref": ref})
        return job

    # --- short references (J1, J2, ...) --------------------------------------------------------

    def _refs_file(self) -> Path:
        return self.runtime_dir / "refs.json"

    def _next_ref_locked(self) -> str:
        """Allocate the next reference. The counter is saved before the reference is used, so a
        crash can skip a number but never hand one out twice; deleted jobs keep theirs."""
        path = self._refs_file()
        try:
            counter = int(fsutil.read_json(path)["next"])
        except FileNotFoundError:
            counter = 1
        except (ValueError, KeyError, TypeError) as exc:
            raise ValidationError(f"{path} is corrupt ({exc}); fix or remove it") from exc
        jobs, _ = self.list_jobs()
        used = max((int(j.ref[1:]) for j in jobs if j.ref), default=0)
        number = max(counter, used + 1)  # never below a reference already on disk
        fsutil.atomic_write_json(path, {"next": number + 1})
        return f"J{number}"

    def _assign_missing_refs_locked(self) -> list[Job]:
        jobs, _ = self.list_jobs()
        assigned = []
        for job in sorted((j for j in jobs if j.ref is None), key=lambda j: j.created_at):
            ref = self._next_ref_locked()

            def apply(current: Job, ref: str = ref) -> None:
                if current.ref is None:
                    current.ref = ref

            assigned.append(
                self._mutate_locked(job.job_id, apply, "ref-assigned", {"ref": ref}, None)
            )
        return assigned

    def assign_missing_refs(self) -> list[Job]:
        """Give jobs created before short references existed their J<n>. Idempotent."""
        if not self.jobs_dir.is_dir():
            return []
        with self._state_lock():
            return self._assign_missing_refs_locked()

    def resolve(self, token: str) -> str:
        """The job id for a full id or a short reference (``J3``, case-insensitive). Read-only.

        Unknown or ambiguous references are refused rather than guessed.
        """
        text = token.strip()
        if JOB_ID_PATTERN.match(text):
            return text
        match = re.fullmatch(r"[Jj]([1-9]\d{0,8})", text)
        if match is None:
            raise ValidationError(
                f"{token!r} is neither a job reference like J3 nor a job id like ff-YYYYMMDD-xxxxxx"
            )
        ref = f"J{match.group(1)}"
        jobs, problems = self.list_jobs()
        owners = [j.job_id for j in jobs if j.ref == ref]
        if len(owners) > 1:
            raise ValidationError(f"{ref} is ambiguous ({', '.join(owners)}); use the full id")
        if not owners:
            known = ", ".join(j.ref for j in jobs if j.ref) or "none"
            hint = f"; {len(problems)} unreadable job record(s)" if problems else ""
            raise ValidationError(f"no job {ref} in this repository (known: {known}){hint}")
        return owners[0]

    def update(
        self,
        job_id: str,
        mutate: Callable[[Job], None],
        *,
        event: str,
        details: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> Job:
        """Change non-lifecycle fields (links, evidence).

        State, approvals, lease, attempts, the repository and the worktree can only change
        through ``transition``, the lease API and ``record_worktree``, so a careless ``mutate``
        cannot bypass a gate or point the builder at another checkout.
        """

        def guarded(job: Job) -> None:
            protected = _protected_fields(job)
            mutate(job)
            if _protected_fields(job) != protected:
                raise InvalidTransitionError(
                    "update() may not change state, plan approval, lease, attempts, repository "
                    "or worktree; use transition(), the lease API or record_worktree()"
                )

        return self._mutate(job_id, guarded, event, details, expected_revision)

    def _mutate(
        self,
        job_id: str,
        mutate: Callable[[Job], None],
        event: str,
        details: dict[str, Any] | None,
        expected_revision: int | None,
    ) -> Job:
        with self._state_lock():
            return self._mutate_locked(job_id, mutate, event, details, expected_revision)

    def _mutate_locked(
        self,
        job_id: str,
        mutate: Callable[[Job], None],
        event: str,
        details: dict[str, Any] | None,
        expected_revision: int | None,
    ) -> Job:
        job = self.load(job_id)
        if expected_revision is not None and job.revision != expected_revision:
            raise ConcurrencyError(
                f"job {job_id} is at revision {job.revision}, expected {expected_revision}"
            )
        before = job.state
        mutate(job)
        job.revision += 1
        job.updated_at = utc_now()
        validated = Job.from_dict(job.to_dict())  # never persist a record we could not reload
        # The snapshot is the source of truth; the event log is the audit trail written after it.
        fsutil.atomic_write_json(self._job_file(job_id), validated.to_dict())
        self._append_event(validated, event, before, details)
        return validated

    def _append_event(
        self, job: Job, event: str, before: JobState | None, details: dict[str, Any] | None
    ) -> None:
        record: dict[str, Any] = {
            "at": job.updated_at,
            "event": event,
            "revision": job.revision,
            "state": job.state.value,
            "pid": os.getpid(),
        }
        if before is not None and before is not job.state:
            record["from_state"] = before.value
        if details:
            record["details"] = details
        fsutil.append_jsonl(self._events_file(job.job_id), record)

    def edit_document(
        self,
        job_id: str,
        name: str,
        edit: Callable[[Job, Any], Any],
        *,
        allowed_states: frozenset[JobState],
        event: str,
        details: dict[str, Any] | None = None,
    ) -> Any:
        """Replace one of a job's side documents (e.g. ``discovery.json``) under the state lock.

        ``edit`` receives the job and the current document (``None`` if absent) and returns
        the new one. The job's revision advances and the change is logged, so an approval shown
        at an older revision can no longer be committed.
        """
        if name not in _SIDE_DOCUMENTS:
            raise ValidationError(f"unknown job document {name!r}")
        path = self.job_dir(job_id) / name
        with self._state_lock():
            job = self.load(job_id)
            if job.state not in allowed_states:
                allowed = ", ".join(sorted(s.value for s in allowed_states))
                raise InvalidTransitionError(
                    f"job {job_id} is {job.state.value}; {name} can change only in: {allowed}"
                )
            try:
                current = fsutil.read_json(path)
            except FileNotFoundError:
                current = None
            except ValueError as exc:
                raise ValidationError(f"{path} is not valid JSON: {exc}") from exc
            updated = edit(job, current)
            fsutil.atomic_write_json(path, updated)
            self._mutate_locked(job_id, lambda _: None, event, details, None)
        return updated

    def read_document(self, job_id: str, name: str) -> Any:
        if name not in _SIDE_DOCUMENTS:
            raise ValidationError(f"unknown job document {name!r}")
        path = self.job_dir(job_id) / name
        try:
            return fsutil.read_json(path)
        except FileNotFoundError:
            return None
        except ValueError as exc:
            raise ValidationError(f"{path} is not valid JSON: {exc}") from exc

    def record_worktree(self, job_id: str, worktree: Worktree) -> Job:
        """Record the job's worktree: only its own location and branch, and only once."""
        expected = self.repo_root / WORKTREES_RELATIVE / job_id
        if fsutil.normalized(worktree.path) != fsutil.normalized(expected) or (
            worktree.branch != branch_for(job_id)
        ):
            raise ValidationError(
                f"a job's worktree must be {expected} on {branch_for(job_id)}, not "
                f"{worktree.path} on {worktree.branch}"
            )

        def apply(job: Job) -> None:
            if job.is_terminal:
                raise InvalidTransitionError(f"job {job_id} is {job.state.value}")
            if job.worktree is not None and job.worktree != worktree:
                raise InvalidTransitionError("the job's worktree is already recorded")
            job.worktree = worktree

        details = {"path": worktree.path, "branch": worktree.branch, "base_sha": worktree.base_sha}
        return self._mutate(job_id, apply, "worktree-prepared", details, None)

    def assign_providers(self, job_id: str, builder: str, reviewer: str) -> Job:
        """Record the human's explicit choice of builder and reviewer for an approved job.

        Allowed only while no worker owns the job: after approval (``plan_approved``,
        ``ready``) or while it waits for a human (``needs_human``), so a running builder can
        never be swapped for another one. The names are checked against the real providers by
        the caller (``forgeflow.pipeline``).
        """
        policy = Policy(builder=builder, reviewer=reviewer)
        Policy.from_dict(policy.to_dict())  # name format

        def apply(job: Job) -> None:
            if job.state not in _ASSIGNABLE_STATES:
                allowed = ", ".join(sorted(s.value for s in _ASSIGNABLE_STATES))
                raise InvalidTransitionError(
                    f"job {job_id} is {job.state.value}; providers can be chosen only in: {allowed}"
                )
            if job.lease is not None:
                raise LeaseError(f"job {job_id} is owned by a worker; providers cannot change")
            job.policy = policy

        return self._mutate(
            job_id, apply, "providers-assigned", {"builder": builder, "reviewer": reviewer}, None
        )

    # --- lifecycle -------------------------------------------------------------------------

    def submit_plan(self, job_id: str, plan_path: str, *, reason: str = "") -> Job:
        """DRAFT -> AWAITING_PLAN_APPROVAL, pinning the plan document's exact bytes and hash."""
        rel = Path(plan_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValidationError("plan path must be relative to the repository root")

        def apply(job: Job) -> None:
            self._require_transition(job, S.AWAITING_PLAN_APPROVAL)
            job.plan = Plan(
                path=rel.as_posix(), version=job.plan.version + 1, submitted_at=utc_now()
            )
            data = self._read_plan(job)
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError(f"plan document {rel.as_posix()} is not UTF-8") from exc
            job.plan.sha256 = hashlib.sha256(data).hexdigest()
            # Versions only grow and job.json still records the previous one, so a v<N>.md that
            # already exists was left by a submission that crashed before it was recorded;
            # nothing refers to it. Recorded snapshots (older versions) are never written again.
            fsutil.atomic_write_bytes(self._plan_snapshot(job.job_id, job.plan.version), data)
            job.state = S.AWAITING_PLAN_APPROVAL

        details: dict[str, Any] = {"plan": rel.as_posix(), "reason": reason}

        def apply_and_describe(job: Job) -> None:
            apply(job)
            # The log names exactly which bytes each version was, so history survives revisions.
            details.update(version=job.plan.version, sha256=job.plan.sha256)

        return self._mutate(job_id, apply_and_describe, "plan-submitted", details, None)

    def transition(
        self,
        job_id: str,
        target: JobState,
        *,
        reason: str = "",
        approval: Approval | None = None,
        lease_token: str | None = None,
        expected_revision: int | None = None,
    ) -> Job:
        """Move a job to ``target`` if the table and the target's guard allow it."""
        if target is S.INTERRUPTED:
            raise InvalidTransitionError("INTERRUPTED is entered only through recovery")
        if target is S.PLAN_APPROVED:
            raise InvalidTransitionError(
                "Gate A: plan approval is captured only by `forgeflow approve`, run by a human "
                "at an interactive console"
            )
        if target is S.AWAITING_PLAN_APPROVAL:
            raise InvalidTransitionError("use submit_plan() to request plan approval")
        if target is S.AWAITING_MERGE_APPROVAL:
            raise InvalidTransitionError("use request_merge_approval(), which verifies the PR")
        if target is S.MERGED:
            raise InvalidTransitionError("use record_merge(), which verifies the merge on GitHub")
        return self._transition(
            job_id,
            target,
            reason=reason,
            approval=approval,
            lease_token=lease_token,
            expected_revision=expected_revision,
        )

    def _record_plan_approval(
        self, job_id: str, approval: Approval, *, expected_revision: int
    ) -> Job:
        """AWAITING_PLAN_APPROVAL -> PLAN_APPROVED. Called only by forgeflow.approval.

        ``expected_revision`` is the revision the human was shown, so a resubmission or any
        other change between display and confirmation is refused instead of being approved.
        """
        return self._transition(
            job_id, S.PLAN_APPROVED, approval=approval, expected_revision=expected_revision
        )

    def request_merge_approval(
        self, job_id: str, lease_token: str, github: PullRequestSource
    ) -> Job:
        """REVIEWING -> AWAITING_MERGE_APPROVAL (Gate B) after verifying checks and the PR.

        The linked PR is looked up on GitHub; it must be an open draft from the job's branch
        whose head is exactly the commit the current round's passing checks ran on.
        """
        observed = self._observe_linked_pr(job_id, github)
        return self._transition(
            job_id, S.AWAITING_MERGE_APPROVAL, lease_token=lease_token, observed=observed
        )

    def record_merge(self, job_id: str, github: PullRequestSource) -> Job:
        """AWAITING_MERGE_APPROVAL -> MERGED, only for a merge GitHub reports. Never merges."""
        observed = self._observe_linked_pr(job_id, github)
        return self._transition(job_id, S.MERGED, observed=observed)

    def _observe_linked_pr(self, job_id: str, github: PullRequestSource) -> PullRequestObservation:
        job = self.load(job_id)
        if job.github.pull_request is None or not job.repo.slug:
            raise InvalidTransitionError("Gate B: a linked draft pull request is required")
        return github.view_pull_request(job.repo.slug, job.github.pull_request.number)

    def _transition(
        self,
        job_id: str,
        target: JobState,
        *,
        reason: str = "",
        approval: Approval | None = None,
        lease_token: str | None = None,
        observed: PullRequestObservation | None = None,
        expected_revision: int | None = None,
    ) -> Job:
        details: dict[str, Any] = {"reason": reason} if reason else {}
        if approval is not None:
            details["approval"] = approval.to_dict()
        if observed is not None:
            details["pull_request_observed"] = observed.to_dict()

        with self._state_lock():

            def apply(job: Job) -> None:
                source = job.state
                self._require_transition(job, target)
                self._check_guard(
                    job,
                    target,
                    reason=reason,
                    approval=approval,
                    lease_token=lease_token,
                    observed=observed,
                )
                self._apply_side_effects(job, source, target, approval, observed)
                job.state = target

            job = self._mutate_locked(
                job_id, apply, f"transition:{target.value}", details, expected_revision
            )
        self._notify(job)
        return job

    def _notify(self, job: Job) -> None:
        for listener in list(self.listeners):
            try:
                listener(job)
            except Exception as exc:  # a listener never undoes or blocks a committed transition
                self._append_event(
                    job, "listener-failed", None, {"listener": repr(listener), "error": str(exc)}
                )

    def _require_transition(self, job: Job, target: JobState) -> None:
        if target not in TRANSITIONS[job.state]:
            allowed = ", ".join(sorted(s.value for s in TRANSITIONS[job.state])) or "none"
            raise InvalidTransitionError(
                f"job {job.job_id}: {job.state.value} -> {target.value} is not allowed "
                f"(allowed: {allowed})"
            )

    def _check_guard(
        self,
        job: Job,
        target: JobState,
        *,
        reason: str,
        approval: Approval | None,
        lease_token: str | None,
        observed: PullRequestObservation | None,
    ) -> None:
        source = job.state

        if target is S.PLAN_APPROVED:
            if approval is None or approval.gate != "plan":
                raise InvalidTransitionError("Gate A: a recorded plan approval is required")
            if approval.method != HUMAN_APPROVAL_METHOD:
                raise InvalidTransitionError(
                    f"Gate A: approvals captured by {approval.method!r} are not accepted"
                )
            current = self.plan_digest(job)
            if (
                approval.subject_sha256 != job.plan.sha256
                or current != job.plan.sha256
                or approval.subject_version != job.plan.version
            ):
                raise InvalidTransitionError(
                    "Gate A: the approval does not match the submitted plan version, or the "
                    "plan changed after submission; resubmit it for approval"
                )
            # The snapshot must be intact too.
            self.plan_snapshot_text(job.job_id, current, job.plan.version)

        if target is S.DRAFT and source is S.PLAN_APPROVED:
            # Revoking an approval is safe only while nothing was built or published from it.
            if job.github.issue is not None or job.github.handoff is not None:
                raise InvalidTransitionError(
                    "the approved plan was already handed off to GitHub; it cannot be revised "
                    "in place (cancel the job and close its issue instead)"
                )
            if job.worktree is not None:
                raise InvalidTransitionError("the job already has a worktree")
            if not reason.strip():
                raise InvalidTransitionError("revoking a plan approval requires a reason")

        needs_reason = target in (S.CANCELLED, S.NEEDS_HUMAN) or source is S.NEEDS_HUMAN
        if needs_reason and not reason.strip():
            raise InvalidTransitionError(f"{target.value} requires a reason")

        if target is S.READY:
            self._require_current_plan_approval(job)
            if job.worktree is None:
                raise InvalidTransitionError("READY requires a prepared worktree")
            if job.github.issue is None:
                raise InvalidTransitionError("READY requires a linked GitHub issue")

        if target in SLOT_STATES and source not in SLOT_STATES:
            occupant = self._slot_occupant(exclude=job.job_id)
            if occupant:
                raise ActiveJobError(
                    f"job {occupant} already occupies the execution slot; only one active job "
                    "is allowed"
                )

        if source in WORKER_STATES or target in WORKER_STATES:
            # Only the current owner may move a job into, through or out of a worker phase.
            self._require_lease(job, lease_token, target)
        if job.lease is not None and (source is S.BUILDING or target in _LEASE_ENDING_STATES):
            # The builder's provider must be gone before its output is verified, or before the
            # lease ends and another builder could be started next to it.
            status = worker_status(job.lease)
            if status in _WORKER_BLOCKING:
                raise InvalidTransitionError(
                    f"provider process {job.lease.worker_pid} is still running ({status}); "
                    f"stop it before leaving {source.value}"
                )
        if target in WORKER_STATES:
            if target is S.BUILDING:
                self._require_current_plan_approval(job)  # Gate A is re-verified at dispatch
                if source in REWORK_SOURCES and job.attempts.rework >= job.attempts.max_rework:
                    raise InvalidTransitionError(
                        f"rework limit reached ({job.attempts.max_rework}); escalate to a human"
                    )
            if source is S.INTERRUPTED and job.interrupted_from is not target:
                origin = job.interrupted_from.value if job.interrupted_from else "?"
                raise InvalidTransitionError(
                    f"job was interrupted in {origin}; it must resume there, not in {target.value}"
                )

        if target is S.AWAITING_MERGE_APPROVAL:
            checks = self._require_current_checks(job)
            pr = self._require_linked_pr(job, observed)
            if pr.state != "OPEN" or not pr.is_draft:
                raise InvalidTransitionError(
                    f"Gate B: PR #{pr.number} must be an open draft (it is {pr.state}"
                    f"{', draft' if pr.is_draft else ', not a draft'})"
                )
            if pr.head_sha != checks.commit:
                raise InvalidTransitionError(
                    f"Gate B: PR #{pr.number} head commit {pr.head_sha[:12]} is not the "
                    f"validated commit {(checks.commit or '?')[:12]}; run the checks on it"
                )
            head = self._clean_head(self.job_worktree(job))
            if head != checks.commit:
                raise InvalidTransitionError(
                    f"Gate B: the worktree moved to commit {head[:12]} after the checks ran"
                )

        if target is S.MERGED:
            checks = self._require_current_checks(job)
            pr = self._require_linked_pr(job, observed)
            if not pr.merged or not pr.merged_at:
                raise InvalidTransitionError(
                    f"PR #{pr.number} is not merged on GitHub (state {pr.state}); ForgeFlow "
                    "only records merges it observes and never merges"
                )
            if pr.head_sha != checks.commit:
                raise InvalidTransitionError(
                    f"PR #{pr.number} was merged with head commit {pr.head_sha[:12]}, not the "
                    f"validated commit {(checks.commit or '?')[:12]}; investigate, or cancel "
                    "the job with a reason"
                )

    def _require_current_checks(self, job: Job) -> Evidence:
        """The latest checks evidence, if run_checks produced it this round and it passed."""
        checks = [e for e in job.evidence if e.kind == "checks"]
        if not checks:
            raise InvalidTransitionError("no deterministic checks were run for this job")
        latest = checks[-1]
        if latest.build_round != job.attempts.build:
            raise InvalidTransitionError(
                f"the latest checks ran in build round {latest.build_round}, not the current "
                f"build round {job.attempts.build}; run them again"
            )
        if latest.mocked or latest.commit is None:
            raise InvalidTransitionError(
                "the latest checks evidence was not produced by run_checks"
            )
        if latest.status != "passed":
            raise InvalidTransitionError("the latest deterministic checks did not pass")
        simulated = sorted(
            {e.kind for e in job.evidence if e.build_round == job.attempts.build and e.mocked}
        )
        if simulated:
            raise InvalidTransitionError(
                f"this build round includes mocked evidence ({', '.join(simulated)}); a "
                "simulated build cannot be delivered"
            )
        return latest

    def _require_linked_pr(
        self, job: Job, observed: PullRequestObservation | None
    ) -> PullRequestObservation:
        linked = job.github.pull_request
        if linked is None or observed is None or observed.number != linked.number:
            raise InvalidTransitionError("Gate B: a linked draft pull request is required")
        if job.worktree is None or not job.repo.slug:
            raise InvalidTransitionError("the job has no worktree or GitHub repository")
        expected = (job.repo.slug.lower(), job.worktree.branch, job.repo.base_branch)
        actual = (
            observed.repo.lower(),
            observed.head_ref,
            observed.base_ref,
        )
        head_repo = (observed.head_repo or "").lower()
        if actual != expected or head_repo != job.repo.slug.lower():
            raise InvalidTransitionError(
                f"PR #{observed.number} is {observed.head_repo}:{observed.head_ref} -> "
                f"{observed.base_ref} in {observed.repo}, not {job.repo.slug}:"
                f"{job.worktree.branch} -> {job.repo.base_branch}"
            )
        return observed

    def _apply_side_effects(
        self,
        job: Job,
        source: JobState,
        target: JobState,
        approval: Approval | None,
        observed: PullRequestObservation | None,
    ) -> None:
        if target is S.PLAN_APPROVED:
            job.plan.approval = approval
        if target is S.DRAFT:
            # Revising the plan invalidates the previous submission and any approval of it; the
            # version counter is kept so the next submission is a new, distinct version.
            job.plan = Plan(version=job.plan.version)
        if target is S.BUILDING:
            job.attempts.build += 1
            if source in REWORK_SOURCES:
                job.attempts.rework += 1
        if source is S.INTERRUPTED:
            if target in WORKER_STATES:
                job.attempts.resumes += 1
            job.interrupted_from = None
        if source is S.NEEDS_HUMAN and target is S.READY:
            # A human chose to continue an escalated job (with a reason): that decision grants a
            # fresh, still bounded, rework budget. attempts.build keeps the total.
            job.attempts.human_resumes += 1
            job.attempts.rework = 0
        if observed is not None:
            job.github.pull_request = PullRequestLink(
                number=observed.number,
                url=observed.url,
                is_draft=observed.is_draft,
                state=observed.state,
            )
        if target in _LEASE_ENDING_STATES:
            # Human-facing and terminal states own no worker; the recorded lease ends here.
            job.lease = None

    def verify_plan_approval(self, job_id: str) -> Job:
        """Gate A re-check for an execution boundary (handoff, dispatch). Returns the job.

        Refuses unless the plan was approved by a human, the plan file still has the approved
        hash and the approved snapshot is intact.
        """
        job = self.load(job_id)
        self.require_own_repository(job)
        self._require_current_plan_approval(job)
        return job

    def _require_current_plan_approval(self, job: Job) -> None:
        approval = job.plan.approval
        if approval is None or approval.gate != "plan":
            raise InvalidTransitionError("Gate A: the plan has not been approved")
        if approval.method != HUMAN_APPROVAL_METHOD:
            raise InvalidTransitionError(
                f"Gate A: the recorded approval was captured by {approval.method!r}, not by a "
                "human at an interactive console"
            )
        if self.plan_digest(job) != approval.subject_sha256:
            raise InvalidTransitionError(
                "Gate A: the plan document changed after approval; it must be re-approved"
            )
        self.approved_plan_text(job)  # the snapshot handed to builders must be intact too

    def _require_lease(self, job: Job, token: str | None, target: JobState) -> None:
        lease = job.lease
        if lease is None or token is None or not secrets.compare_digest(lease.token, token):
            raise LeaseError(
                f"{job.state.value} -> {target.value} requires the caller to hold the job lease"
            )
        # Single writer: only the job's assigned builder may enter the writing phase.
        if target is S.BUILDING and (
            lease.role != "builder" or lease.provider != job.policy.builder
        ):
            raise LeaseError(
                f"lease {lease.role}/{lease.provider} is not the job's assigned builder "
                f"{job.policy.builder!r}"
            )

    def _slot_occupant(self, exclude: str) -> str | None:
        jobs, problems = self.list_jobs()
        if problems:
            # Fail closed: an unreadable record might be the active job.
            raise ActiveJobError(
                "cannot verify the execution slot; unreadable job records: " + "; ".join(problems)
            )
        for other in jobs:
            if other.job_id != exclude and other.state in SLOT_STATES:
                return other.job_id
        return None

    # --- GitHub issue ownership --------------------------------------------------------------

    def issue_owner(self, repo_slug: str, number: int, exclude: str) -> str | None:
        """The non-terminal job already linked to this issue, if any."""
        jobs, problems = self.list_jobs()
        if problems:
            raise ActiveJobError(
                "cannot verify issue ownership; unreadable job records: " + "; ".join(problems)
            )
        for other in jobs:
            issue = other.github.issue
            if (
                other.job_id != exclude
                and not other.is_terminal
                and issue is not None
                and issue.repo.lower() == repo_slug.lower()
                and issue.number == number
            ):
                return other.job_id
        return None

    def update_if_issue_free(
        self,
        job_id: str,
        repo_slug: str,
        number: int,
        mutate: Callable[[Job], None],
        *,
        details: dict[str, Any],
    ) -> Job:
        """Link an issue atomically with the duplicate-ownership check."""
        with self._state_lock():
            owner = self.issue_owner(repo_slug, number, exclude=job_id)
            if owner:
                raise ActiveJobError(f"issue {repo_slug}#{number} is already owned by job {owner}")

            def guarded(job: Job) -> None:
                protected = _protected_fields(job)
                mutate(job)
                if _protected_fields(job) != protected:
                    raise InvalidTransitionError("issue linking may not change lifecycle fields")

            return self._mutate_locked(job_id, guarded, "issue-linked", details, None)

    # --- evidence --------------------------------------------------------------------------

    def record_evidence(
        self,
        job_id: str,
        kind: str,
        status: str,
        payload: dict[str, Any],
        *,
        mocked: bool,
        commit: str | None = None,
    ) -> Evidence:
        """Record a provider result, review or other note. Check results come from run_checks.

        ``commit`` names the commit the evidence is about (the reviewed or committed one).
        """
        if not _EVIDENCE_KIND.match(kind):
            raise ValidationError(f"invalid evidence kind {kind!r}")
        if kind == "checks":
            raise ValidationError(
                "checks evidence is recorded only by run_checks(), which runs them"
            )
        if commit is not None and not re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", commit):
            raise ValidationError(f"{commit!r} is not a full commit id")
        return self._record_evidence(job_id, kind, status, payload, mocked=mocked, commit=commit)

    def _record_evidence(
        self,
        job_id: str,
        kind: str,
        status: str,
        payload: dict[str, Any],
        *,
        mocked: bool,
        commit: str | None,
        check: Callable[[Job], None] | None = None,
    ) -> Evidence:
        holder: list[Evidence] = []

        def apply(job: Job) -> None:
            if check is not None:
                check(job)
            seq = len(job.evidence) + 1
            rel = f"evidence/{seq:03d}-{kind}.json"
            fsutil.atomic_write_json(
                self.job_dir(job_id) / rel,
                {"kind": kind, "status": status, "mocked": mocked, "payload": payload},
            )
            item = Evidence(
                kind=kind,
                path=rel,
                status=status,
                mocked=mocked,
                created_at=utc_now(),
                build_round=job.attempts.build,
                commit=commit,
            )
            job.evidence.append(item)
            holder.append(item)

        self._mutate(
            job_id,
            apply,
            "evidence-recorded",
            {"kind": kind, "status": status, "mocked": mocked, "commit": commit},
            None,
        )
        return holder[0]

    def run_checks(
        self,
        job_id: str,
        lease_token: str,
        commands: Sequence[Sequence[str]],
        *,
        runner: Runner = run_command,
        timeout: float = 600.0,
        names: Sequence[str] = (),
        source: dict[str, Any] | None = None,
    ) -> Evidence:
        """Run the deterministic checks in the job's worktree and record what really happened.

        Only the lease holder may run them, only in VERIFYING, and only on a clean worktree.
        The evidence names the commit they ran on; it passes only if every command exited 0 and
        the commands left that commit and a clean worktree behind. ``names`` label the commands
        and ``source`` says where they were configured; both are recorded as given.
        """
        if not commands or any(not argv for argv in commands):
            raise ValidationError("at least one non-empty check command is required")
        job = self.load(job_id)
        lease = job.lease
        if lease is None or not secrets.compare_digest(lease.token, lease_token):
            raise LeaseError("running checks requires the caller to hold the job lease")
        if job.state is not S.VERIFYING:
            raise InvalidTransitionError(f"checks run while verifying, not in {job.state.value}")
        worktree = self.job_worktree(job)
        commit = self._clean_head(worktree)
        build_round = job.attempts.build

        results: list[dict[str, Any]] = []
        for i, argv in enumerate(commands):
            label = {"name": names[i]} if i < len(names) else {}
            started = time.monotonic()
            try:
                result = runner(list(argv), cwd=worktree, timeout=timeout)
            except ForgeFlowError as exc:  # e.g. the tool is not installed
                results.append({**label, "argv": list(argv), "ok": False, "error": str(exc)})
                continue
            results.append(
                {
                    **label,
                    "argv": list(argv),
                    "ok": result.ok,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "seconds": round(time.monotonic() - started, 1),
                    "stdout_tail": redact_credentials(result.stdout[-_OUTPUT_TAIL:]),
                    "stderr_tail": redact_credentials(result.stderr[-_OUTPUT_TAIL:]),
                }
            )
        after = git_ok(worktree, "rev-parse", "HEAD", read_only=True)
        dirty_after = porcelain_status(worktree)
        unchanged = after == commit and not dirty_after
        passed = unchanged and all(r["ok"] for r in results)
        payload = {
            "commit": commit,
            "build_round": build_round,
            "commands": results,
            "worktree_unchanged": unchanged,
            "changed_paths": dirty_after[:50],
            "source": source or {},
        }

        def still_owned(current: Job) -> None:
            if (
                current.lease is None
                or current.lease.token != lease_token
                or current.state is not S.VERIFYING
                or current.attempts.build != build_round
            ):
                raise LeaseError("the job changed hands or phase while its checks ran")

        return self._record_evidence(
            job_id,
            "checks",
            "passed" if passed else "failed",
            payload,
            mocked=False,
            commit=commit,
            check=still_owned,
        )

    @staticmethod
    def _clean_head(worktree: Path) -> str:
        dirty = porcelain_status(worktree)
        if dirty:
            raise InvalidTransitionError(
                f"the worktree has {len(dirty)} uncommitted change(s); checks and delivery are "
                "judged on a commit, so commit or discard them first"
            )
        return git_ok(worktree, "rev-parse", "HEAD", read_only=True)

    # --- worker leases ---------------------------------------------------------------------

    def acquire_lease(self, job_id: str, role: str, provider: str) -> LeaseHandle:
        """Become the job's single worker. Raises if a live or stale owner exists."""
        if role not in ("builder", "reviewer"):
            raise ValidationError(f"unknown role {role!r}")
        os_lock = self._lease_lock(job_id)
        if not self.exists(job_id):
            raise ValidationError(f"no such job: {job_id}")
        if not os_lock.try_acquire():
            current = self.load(job_id).lease
            who = f" (pid {current.holder_pid} on {current.holder_host})" if current else ""
            raise DuplicateWorkerError(f"job {job_id} is already owned by a live worker{who}")
        try:
            token = secrets.token_hex(16)

            def apply(job: Job) -> None:
                if job.lease is not None:
                    raise StaleLeaseError(
                        f"job {job_id} still records a lease from pid {job.lease.holder_pid} on "
                        f"{job.lease.holder_host}, which no longer holds it; run recovery first"
                    )
                if job.state not in _LEASE_START_STATES:
                    raise LeaseError(f"a worker cannot take over a job in {job.state.value}")
                resume_target = job.interrupted_from if job.state is S.INTERRUPTED else None
                # The job is owned by its builder's orchestrator throughout; the reviewer is a
                # read-only subprocess of it. A review interrupted mid-way may also be resumed
                # by a reviewer lease (Checkpoint A), which can never enter BUILDING.
                expected_role = (
                    role if resume_target is S.REVIEWING and role == "reviewer" else "builder"
                )
                expected_provider = (
                    job.policy.reviewer if expected_role == "reviewer" else job.policy.builder
                )
                if job.policy.builder == UNASSIGNED_PROVIDER or expected_provider is None:
                    raise LeaseError(
                        f"job {job_id} has no {expected_role} assigned yet; nothing can run it"
                    )
                if role != expected_role or provider != expected_provider:
                    raise LeaseError(
                        f"job {job_id} needs its {expected_role} {expected_provider!r}, "
                        f"not {role} {provider!r}"
                    )
                job.lease = Lease(
                    token=token,
                    role=role,
                    provider=provider,
                    holder_pid=os.getpid(),
                    holder_host=hostname(),
                    acquired_at=utc_now(),
                )

            self._mutate(
                job_id,
                apply,
                "lease-acquired",
                {"role": role, "provider": provider, "holder_pid": os.getpid()},
                None,
            )
        except BaseException:
            os_lock.release()
            raise
        return LeaseHandle(self, job_id, token, os_lock)

    def _set_worker_pid(self, job_id: str, token: str, pid: int | None) -> None:
        started = process_started_at(pid) if pid else None

        def apply(job: Job) -> None:
            if job.lease is None or job.lease.token != token:
                raise LeaseError("lease is no longer held")
            job.lease.worker_pid = pid
            job.lease.worker_started_at = started

        self._mutate(
            job_id, apply, "worker-pid", {"worker_pid": pid, "worker_started_at": started}, None
        )

    def _release_lease(self, job_id: str, token: str) -> None:
        with self._state_lock():
            job = self.load(job_id)
            if job.lease is None or job.lease.token != token:
                return
            status = worker_status(job.lease)
            if status in _WORKER_BLOCKING:
                # The provider outlived its orchestrator's worker phase. Keep the lease record:
                # acquire_lease() then refuses (stale lease) and recover() reports the orphan.
                self._mutate_locked(
                    job_id,
                    lambda _: None,
                    "lease-retained",
                    {"worker_pid": job.lease.worker_pid, "worker_status": status},
                    None,
                )
                return

            def apply(current: Job) -> None:
                current.lease = None
                if current.state in WORKER_STATES:
                    # Leaving a worker phase without finishing it is an interruption.
                    current.interrupted_from = current.state
                    current.state = S.INTERRUPTED

            self._mutate_locked(job_id, apply, "lease-released", None, None)

    def recover(self, job_id: str) -> RecoveryReport:
        """Detect a worker that died and mark the job INTERRUPTED, preserving its worktree.

        Refuses (without changes) while a worker is alive, while the recorded provider
        subprocess still runs or cannot be told apart from a live process, or when the lease
        was taken on another host.
        """
        job = self.load(job_id)
        if job.lease is None and job.state not in WORKER_STATES:
            return RecoveryReport(
                job_id, "nothing-to-recover", f"job is {job.state.value} with no worker lease"
            )
        os_lock = self._lease_lock(job_id)
        if not os_lock.try_acquire():
            return RecoveryReport(job_id, "running", "a live worker holds the job lease")
        try:
            with self._state_lock():
                current = self.load(job_id)
                lease = current.lease
                if lease is not None and lease.holder_host != hostname():
                    return RecoveryReport(
                        job_id,
                        "foreign-host",
                        f"lease was taken on {lease.holder_host}; recover it there",
                    )
                status = worker_status(lease)
                if lease is not None and status in _WORKER_BLOCKING:
                    return RecoveryReport(
                        job_id,
                        "orphaned-worker",
                        f"provider process {lease.worker_pid} is still running ({status}); stop "
                        "it before recovering (if that PID now belongs to an unrelated process, "
                        "use force_release_lease with a reason)",
                    )

                def apply(job: Job) -> None:
                    job.lease = None
                    if job.state in WORKER_STATES:
                        job.interrupted_from = job.state
                        job.state = S.INTERRUPTED

                lost = lease.to_dict() if lease else None
                updated = self._mutate_locked(
                    job_id, apply, "recovered", {"lost_lease": lost, "worker_status": status}, None
                )
            return RecoveryReport(
                job_id,
                "recovered",
                f"worker lost; job is {updated.state.value}, worktree untouched",
            )
        finally:
            os_lock.release()

    def force_release_lease(self, job_id: str, reason: str) -> Job:
        """Human override for a lease that fails closed (e.g. a reused PID). Audited.

        Still refuses while the recorded provider process is verifiably alive (same PID and
        start time): that is not a stale lease, and releasing it would allow a second writer.
        """
        if not reason.strip():
            raise ValidationError("a reason is required to force-release a lease")
        os_lock = self._lease_lock(job_id)
        if not os_lock.try_acquire():
            raise DuplicateWorkerError("a live worker holds the job lease; stop it first")
        try:

            def apply(job: Job) -> None:
                if job.lease is not None and worker_status(job.lease) == "running":
                    raise LeaseError(
                        f"provider process {job.lease.worker_pid} is verifiably still running; "
                        "stop it first"
                    )
                job.lease = None
                if job.state in WORKER_STATES:
                    job.interrupted_from = job.state
                    job.state = S.INTERRUPTED

            return self._mutate(
                job_id,
                apply,
                "lease-force-released",
                {"reason": reason},
                None,
            )
        finally:
            os_lock.release()


class LeaseHandle:
    """Proof of worker ownership. Release it (or use ``with``) when the worker phase ends.

    Releasing while the recorded provider subprocess still runs keeps the lease record (the job
    stays owned and needs recovery once the process is stopped).
    """

    def __init__(self, store: JobStore, job_id: str, token: str, os_lock: fsutil.FileLock):
        self.store = store
        self.job_id = job_id
        self.token = token
        self._os_lock = os_lock

    @property
    def active(self) -> bool:
        return self._os_lock.held

    def set_worker_pid(self, pid: int | None) -> None:
        self.store._set_worker_pid(self.job_id, self.token, pid)

    def release(self) -> None:
        if not self._os_lock.held:
            return
        try:
            self.store._release_lease(self.job_id, self.token)
        finally:
            self._os_lock.release()

    def __enter__(self) -> LeaseHandle:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def _protected_fields(job: Job) -> tuple[Any, ...]:
    return (
        job.state,
        job.interrupted_from,
        job.plan.to_dict(),
        job.lease.to_dict() if job.lease else None,
        job.attempts.to_dict(),
        job.job_id,
        job.ref,
        job.policy.to_dict(),
        job.repo.to_dict(),
        job.worktree.to_dict() if job.worktree else None,
    )
