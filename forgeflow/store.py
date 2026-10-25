"""Durable local job store with guarded transitions, one execution slot and worker leases.

Layout (inside the main checkout, self-ignored by git)::

    .forge/runtime/
        .gitignore            "*"  - nothing here is ever committed
        state.lock            OS lock serialising every read-modify-write
        jobs/<job-id>/
            job.json          current snapshot (atomic replace, revisioned)
            events.jsonl      append-only audit trail of changes and decisions
            lease.lock        OS lock held by the live worker process for the job
            evidence/         machine-readable results (checks, provider output, reviews)

Concurrency model:

* ``state.lock`` is held only for the few milliseconds of a read-modify-write. ``job.json`` also
  carries a ``revision`` so callers holding a stale copy can be rejected (compare-and-swap).
* ``lease.lock`` is held for as long as a worker owns the job. Because it is an OS lock it is
  released automatically if the worker process dies, which is how an interruption is detected
  without trusting PIDs. The lease record in ``job.json`` names the holder for diagnostics and
  for detecting an orphaned provider subprocess that outlived its orchestrator.
* A job sitting in a worker state without a live lease is, by definition, interrupted.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from forgeflow import fsutil
from forgeflow.errors import (
    ActiveJobError,
    ConcurrencyError,
    DuplicateWorkerError,
    InvalidTransitionError,
    LeaseError,
    StaleLeaseError,
    ValidationError,
)
from forgeflow.jobs import (
    REWORK_SOURCES,
    SLOT_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    WORKER_STATES,
    Approval,
    Evidence,
    Job,
    JobState,
    Lease,
    Plan,
    Policy,
    Repo,
    new_job_id,
    utc_now,
    validate_job_id,
)
from forgeflow.proc import hostname, pid_alive

S = JobState
RUNTIME_RELATIVE = Path(".forge") / "runtime"
_EVIDENCE_KIND = re.compile(r"^[a-z][a-z0-9-]{0,40}$")

# A lease may be taken only where a worker legitimately starts or resumes.
_LEASE_START_STATES = frozenset({S.READY, S.INTERRUPTED, S.AWAITING_MERGE_APPROVAL})


@dataclass(frozen=True)
class RecoveryReport:
    job_id: str
    outcome: str  # running | orphaned-worker | foreign-host | recovered | nothing-to-recover
    detail: str


class JobStore:
    def __init__(self, runtime_dir: Path, lock_timeout: float = 10.0) -> None:
        self.runtime_dir = runtime_dir
        self.jobs_dir = runtime_dir / "jobs"
        self.lock_timeout = lock_timeout

    @classmethod
    def for_repo(cls, repo_root: Path, lock_timeout: float = 10.0) -> JobStore:
        return cls(repo_root / RUNTIME_RELATIVE, lock_timeout=lock_timeout)

    # --- paths -----------------------------------------------------------------------------

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / validate_job_id(job_id)

    def _job_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "job.json"

    def _events_file(self, job_id: str) -> Path:
        return self.job_dir(job_id) / "events.jsonl"

    def _lease_lock(self, job_id: str) -> fsutil.FileLock:
        return fsutil.FileLock(self.job_dir(job_id) / "lease.lock", timeout=0)

    def _state_lock(self) -> fsutil.FileLock:
        return fsutil.FileLock(self.runtime_dir / "state.lock", timeout=self.lock_timeout)

    def _ensure_layout(self) -> None:
        fsutil.write_self_ignoring_gitignore(self.runtime_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

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

    def plan_digest(self, job: Job) -> str:
        """sha256 of the plan file as it is on disk right now."""
        if not job.plan.path:
            raise InvalidTransitionError(f"job {job.job_id} has no plan document")
        root = Path(job.repo.root)
        plan_file = fsutil.ensure_within(root, root / job.plan.path)
        try:
            return hashlib.sha256(plan_file.read_bytes()).hexdigest()
        except FileNotFoundError as exc:
            raise InvalidTransitionError(f"plan document {job.plan.path} is missing") from exc

    # --- creation and generic mutation -----------------------------------------------------

    def create(self, title: str, repo: Repo, policy: Policy) -> Job:
        """Create a DRAFT job. Drafts are discussion-only and never dispatch code."""
        if not title.strip():
            raise ValidationError("title must not be empty")
        with self._state_lock():
            self._ensure_layout()
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
            job = Job(
                job_id=job_id,
                title=title.strip(),
                state=S.DRAFT,
                repo=repo,
                policy=policy,
                created_at=now,
                updated_at=now,
                revision=1,
            )
            Job.from_dict(job.to_dict())  # validate before the first write
            fsutil.atomic_write_json(self._job_file(job_id), job.to_dict())
            self._append_event(job, "created", None, details={"title": job.title})
        return job

    def update(
        self,
        job_id: str,
        mutate: Callable[[Job], None],
        *,
        event: str,
        details: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> Job:
        """Change non-lifecycle fields (links, worktree, evidence).

        State, approvals, lease and attempt counters can only change through ``transition`` and
        the lease API, so a careless ``mutate`` cannot bypass a gate.
        """

        def guarded(job: Job) -> None:
            protected = _protected_fields(job)
            mutate(job)
            if _protected_fields(job) != protected:
                raise InvalidTransitionError(
                    "update() may not change state, plan approval, lease or attempts; "
                    "use transition() or the lease API"
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

    # --- lifecycle -------------------------------------------------------------------------

    def submit_plan(self, job_id: str, plan_path: str, *, reason: str = "") -> Job:
        """DRAFT -> AWAITING_PLAN_APPROVAL, pinning the plan document's current hash."""
        rel = Path(plan_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValidationError("plan path must be relative to the repository root")

        def apply(job: Job) -> None:
            self._require_transition(job, S.AWAITING_PLAN_APPROVAL)
            job.plan = Plan(path=rel.as_posix())
            job.plan.sha256 = self.plan_digest(job)
            job.state = S.AWAITING_PLAN_APPROVAL

        return self._mutate(
            job_id, apply, "plan-submitted", {"plan": rel.as_posix(), "reason": reason}, None
        )

    def transition(
        self,
        job_id: str,
        target: JobState,
        *,
        reason: str = "",
        approval: Approval | None = None,
        lease_token: str | None = None,
        merge_observation: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> Job:
        """Move a job to ``target`` if the table and the target's guard allow it."""
        if target is S.INTERRUPTED:
            raise InvalidTransitionError("INTERRUPTED is entered only through recovery")
        if target is S.AWAITING_PLAN_APPROVAL:
            raise InvalidTransitionError("use submit_plan() to request plan approval")
        details: dict[str, Any] = {"reason": reason} if reason else {}
        if approval is not None:
            details["approval"] = approval.to_dict()
        if merge_observation is not None:
            details["merge_observation"] = merge_observation

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
                    merge_observation=merge_observation,
                )
                self._apply_side_effects(job, source, target, approval)
                job.state = target

            return self._mutate_locked(
                job_id, apply, f"transition:{target.value}", details, expected_revision
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
        merge_observation: dict[str, Any] | None,
    ) -> None:
        source = job.state

        if target is S.PLAN_APPROVED:
            if approval is None or approval.gate != "plan":
                raise InvalidTransitionError("Gate A: a recorded plan approval is required")
            current = self.plan_digest(job)
            if approval.subject_sha256 != job.plan.sha256 or current != job.plan.sha256:
                raise InvalidTransitionError(
                    "Gate A: the approval does not match the submitted plan, or the plan "
                    "changed after submission; resubmit it for approval"
                )

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
            pr = job.github.pull_request
            if pr is None or not pr.is_draft:
                raise InvalidTransitionError("Gate B: a linked draft pull request is required")
            checks = [e for e in job.evidence if e.kind == "checks"]
            if not checks or checks[-1].status != "passed":
                raise InvalidTransitionError("the latest deterministic checks did not pass")

        if target is S.MERGED:
            pr = job.github.pull_request
            observed = merge_observation or {}
            if (
                pr is None
                or observed.get("source") != "github"
                or observed.get("pr_number") != pr.number
                or not observed.get("merged_at")
            ):
                raise InvalidTransitionError(
                    "MERGED records a merge observed on GitHub for the linked PR; ForgeFlow "
                    "never merges"
                )

    def _apply_side_effects(
        self, job: Job, source: JobState, target: JobState, approval: Approval | None
    ) -> None:
        if target is S.PLAN_APPROVED:
            job.plan.approval = approval
        if target is S.DRAFT:
            job.plan = Plan()  # revising the plan invalidates the previous submission
        if target is S.BUILDING:
            job.attempts.build += 1
            if source in REWORK_SOURCES:
                job.attempts.rework += 1
        if source is S.INTERRUPTED:
            if target in WORKER_STATES:
                job.attempts.resumes += 1
            job.interrupted_from = None
        if target in TERMINAL_STATES or target in (S.NEEDS_HUMAN, S.AWAITING_MERGE_APPROVAL):
            # Human-facing and terminal states own no worker; the recorded lease ends here.
            job.lease = None

    def _require_current_plan_approval(self, job: Job) -> None:
        approval = job.plan.approval
        if approval is None or approval.gate != "plan":
            raise InvalidTransitionError("Gate A: the plan has not been approved")
        if self.plan_digest(job) != approval.subject_sha256:
            raise InvalidTransitionError(
                "Gate A: the plan document changed after approval; it must be re-approved"
            )

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
        self, job_id: str, kind: str, status: str, payload: dict[str, Any], *, mocked: bool
    ) -> Evidence:
        if not _EVIDENCE_KIND.match(kind):
            raise ValidationError(f"invalid evidence kind {kind!r}")
        holder: list[Evidence] = []

        def apply(job: Job) -> None:
            seq = len(job.evidence) + 1
            rel = f"evidence/{seq:03d}-{kind}.json"
            fsutil.atomic_write_json(
                self.job_dir(job_id) / rel,
                {"kind": kind, "status": status, "mocked": mocked, "payload": payload},
            )
            item = Evidence(kind=kind, path=rel, status=status, mocked=mocked, created_at=utc_now())
            job.evidence.append(item)
            holder.append(item)

        self._mutate(
            job_id,
            apply,
            "evidence-recorded",
            {"kind": kind, "status": status, "mocked": mocked},
            None,
        )
        return holder[0]

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
                expected_role = "reviewer" if resume_target is S.REVIEWING else "builder"
                expected_provider = (
                    job.policy.reviewer if expected_role == "reviewer" else job.policy.builder
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
        def apply(job: Job) -> None:
            if job.lease is None or job.lease.token != token:
                raise LeaseError("lease is no longer held")
            job.lease.worker_pid = pid

        self._mutate(job_id, apply, "worker-pid", {"worker_pid": pid}, None)

    def _release_lease(self, job_id: str, token: str) -> None:
        def apply(job: Job) -> None:
            if job.lease is None or job.lease.token != token:
                return
            job.lease = None
            if job.state in WORKER_STATES:
                # Leaving a worker phase without finishing it is an interruption.
                job.interrupted_from = job.state
                job.state = S.INTERRUPTED

        self._mutate(job_id, apply, "lease-released", None, None)

    def recover(self, job_id: str) -> RecoveryReport:
        """Detect a worker that died and mark the job INTERRUPTED, preserving its worktree.

        Refuses (without changes) while a worker is alive, while a recorded provider subprocess
        still runs, or when the lease was taken on another host.
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
                if lease is not None and lease.worker_pid and pid_alive(lease.worker_pid):
                    return RecoveryReport(
                        job_id,
                        "orphaned-worker",
                        f"provider process {lease.worker_pid} is still running; stop it before "
                        "recovering (if that PID now belongs to an unrelated process, use "
                        "force_release_lease with a reason)",
                    )

                def apply(job: Job) -> None:
                    job.lease = None
                    if job.state in WORKER_STATES:
                        job.interrupted_from = job.state
                        job.state = S.INTERRUPTED

                lost = lease.to_dict() if lease else None
                updated = self._mutate_locked(
                    job_id, apply, "recovered", {"lost_lease": lost}, None
                )
            return RecoveryReport(
                job_id,
                "recovered",
                f"worker lost; job is {updated.state.value}, worktree untouched",
            )
        finally:
            os_lock.release()

    def force_release_lease(self, job_id: str, reason: str) -> Job:
        """Human override for a lease that fails closed (e.g. a reused PID). Audited."""
        if not reason.strip():
            raise ValidationError("a reason is required to force-release a lease")
        os_lock = self._lease_lock(job_id)
        if not os_lock.try_acquire():
            raise DuplicateWorkerError("a live worker holds the job lease; stop it first")
        try:

            def apply(job: Job) -> None:
                job.lease = None
                if job.state in WORKER_STATES:
                    job.interrupted_from = job.state
                    job.state = S.INTERRUPTED

            return self._mutate(job_id, apply, "lease-force-released", {"reason": reason}, None)
        finally:
            os_lock.release()


class LeaseHandle:
    """Proof of worker ownership. Release it (or use ``with``) when the worker phase ends."""

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
        job.policy.to_dict(),
    )
