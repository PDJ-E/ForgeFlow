"""Job record (schema v1), lifecycle states and the explicit transition table.

The record is the local technical execution state of one approved unit of work (ADR 0001).
GitHub remains authoritative for the work item itself; the job stores references to it.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from forgeflow.errors import ValidationError

SCHEMA_VERSION = 1
DEFAULT_MAX_REWORK = 2

# Job IDs become directory and branch names, so the format is strict: no separators, no "..".
JOB_ID_PATTERN = re.compile(r"^ff-\d{8}-[0-9a-f]{6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class JobState(str, Enum):
    DRAFT = "draft"  # discovery/design only; can never dispatch code (AC-02)
    AWAITING_PLAN_APPROVAL = "awaiting_plan_approval"
    PLAN_APPROVED = "plan_approved"  # Gate A passed
    READY = "ready"  # issue linked, worktree prepared
    BUILDING = "building"
    VERIFYING = "verifying"  # deterministic checks
    REVIEWING = "reviewing"
    AWAITING_MERGE_APPROVAL = "awaiting_merge_approval"  # Gate B: draft PR open
    INTERRUPTED = "interrupted"
    NEEDS_HUMAN = "needs_human"
    MERGED = "merged"  # observed on GitHub; ForgeFlow never merges
    CANCELLED = "cancelled"


S = JobState

TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    S.DRAFT: frozenset({S.AWAITING_PLAN_APPROVAL, S.CANCELLED}),
    S.AWAITING_PLAN_APPROVAL: frozenset({S.PLAN_APPROVED, S.DRAFT, S.CANCELLED}),
    S.PLAN_APPROVED: frozenset({S.READY, S.CANCELLED}),
    S.READY: frozenset({S.BUILDING, S.CANCELLED}),
    S.BUILDING: frozenset({S.VERIFYING, S.INTERRUPTED, S.NEEDS_HUMAN}),
    S.VERIFYING: frozenset({S.REVIEWING, S.BUILDING, S.INTERRUPTED, S.NEEDS_HUMAN}),
    S.REVIEWING: frozenset({S.AWAITING_MERGE_APPROVAL, S.BUILDING, S.INTERRUPTED, S.NEEDS_HUMAN}),
    S.AWAITING_MERGE_APPROVAL: frozenset({S.MERGED, S.BUILDING, S.CANCELLED}),
    S.INTERRUPTED: frozenset({S.BUILDING, S.VERIFYING, S.REVIEWING, S.NEEDS_HUMAN, S.CANCELLED}),
    S.NEEDS_HUMAN: frozenset({S.READY, S.CANCELLED}),
    S.MERGED: frozenset(),
    S.CANCELLED: frozenset(),
}

TERMINAL_STATES = frozenset({S.MERGED, S.CANCELLED})

# States that occupy the single execution slot: at most one job may be in any of them.
SLOT_STATES = frozenset(
    {S.READY, S.BUILDING, S.VERIFYING, S.REVIEWING, S.INTERRUPTED, S.NEEDS_HUMAN}
)

# States in which a worker process is expected to hold the job lease.
WORKER_STATES = frozenset({S.BUILDING, S.VERIFYING, S.REVIEWING})

# Re-entering BUILDING from these states is a rework round and is bounded.
REWORK_SOURCES = frozenset({S.VERIFYING, S.REVIEWING, S.AWAITING_MERGE_APPROVAL})


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_job_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
    return f"ff-{stamp}-{secrets.token_hex(3)}"


def validate_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.match(job_id):
        raise ValidationError(f"invalid job id {job_id!r} (expected ff-YYYYMMDD-xxxxxx)")
    return job_id


def branch_for(job_id: str) -> str:
    return f"forge/{validate_job_id(job_id)}"


# --- small validation helpers ------------------------------------------------------------------


def _get(
    d: dict[str, Any],
    key: str,
    kind: type | tuple[type, ...],
    where: str,
    *,
    optional: bool = False,
) -> Any:
    if key not in d or d[key] is None:
        if optional:
            return None
        raise ValidationError(f"{where}.{key} is required")
    value = d[key]
    # bool is a subclass of int; reject it where an int is expected.
    if isinstance(value, bool) and bool not in (kind if isinstance(kind, tuple) else (kind,)):
        raise ValidationError(f"{where}.{key} has wrong type bool")
    if not isinstance(value, kind):
        raise ValidationError(f"{where}.{key} has wrong type {type(value).__name__}")
    return value


def _obj(d: dict[str, Any], key: str, where: str, *, optional: bool = False) -> Any:
    return _get(d, key, dict, where, optional=optional)


def _non_negative(value: int, where: str) -> int:
    if value < 0:
        raise ValidationError(f"{where} must be >= 0")
    return value


# --- nested records ----------------------------------------------------------------------------


@dataclass
class Repo:
    root: str
    slug: str | None
    remote_url: str | None
    base_branch: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "slug": self.slug,
            "remote_url": self.remote_url,
            "base_branch": self.base_branch,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "repo") -> Repo:
        slug = _get(d, "slug", str, where, optional=True)
        if slug is not None and not re.match(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", slug):
            raise ValidationError(f"{where}.slug {slug!r} is not owner/name")
        return cls(
            root=_get(d, "root", str, where),
            slug=slug,
            remote_url=_get(d, "remote_url", str, where, optional=True),
            base_branch=_get(d, "base_branch", str, where),
        )


@dataclass
class Approval:
    """An explicit, recorded human decision at a gate."""

    gate: str  # "plan" (Gate A) or "merge" (Gate B)
    approver: str
    approved_at: str
    subject_sha256: str  # hash of exactly what was approved (the plan file for Gate A)
    method: str  # how the decision was captured, e.g. "interactive-cli" or "test-fixture"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "approver": self.approver,
            "approved_at": self.approved_at,
            "subject_sha256": self.subject_sha256,
            "method": self.method,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "approval") -> Approval:
        gate = _get(d, "gate", str, where)
        if gate not in ("plan", "merge"):
            raise ValidationError(f"{where}.gate must be 'plan' or 'merge'")
        digest = _get(d, "subject_sha256", str, where)
        if not _SHA256.match(digest):
            raise ValidationError(f"{where}.subject_sha256 is not a sha256 hex digest")
        approver = _get(d, "approver", str, where)
        if not approver.strip():
            raise ValidationError(f"{where}.approver must not be empty")
        return cls(
            gate=gate,
            approver=approver,
            approved_at=_get(d, "approved_at", str, where),
            subject_sha256=digest,
            method=_get(d, "method", str, where),
            note=_get(d, "note", str, where, optional=True) or "",
        )


@dataclass
class Plan:
    path: str | None = None  # repository-relative path of the approved plan document
    sha256: str | None = None  # hash recorded when the plan was submitted for approval
    approval: Approval | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "approval": self.approval.to_dict() if self.approval else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "plan") -> Plan:
        digest = _get(d, "sha256", str, where, optional=True)
        if digest is not None and not _SHA256.match(digest):
            raise ValidationError(f"{where}.sha256 is not a sha256 hex digest")
        approval = _obj(d, "approval", where, optional=True)
        return cls(
            path=_get(d, "path", str, where, optional=True),
            sha256=digest,
            approval=Approval.from_dict(approval, f"{where}.approval") if approval else None,
        )


@dataclass
class Policy:
    builder: str
    reviewer: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"builder": self.builder, "reviewer": self.reviewer}

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "policy") -> Policy:
        builder = _get(d, "builder", str, where)
        reviewer = _get(d, "reviewer", str, where, optional=True)
        for name, value in (("builder", builder), ("reviewer", reviewer)):
            if value is not None and not _PROVIDER_NAME.match(value):
                raise ValidationError(f"{where}.{name} {value!r} is not a valid provider name")
        return cls(builder=builder, reviewer=reviewer)


@dataclass
class Worktree:
    path: str
    branch: str
    base_ref: str
    base_sha: str
    created_by_forgeflow: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "base_ref": self.base_ref,
            "base_sha": self.base_sha,
            "created_by_forgeflow": self.created_by_forgeflow,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "worktree") -> Worktree:
        return cls(
            path=_get(d, "path", str, where),
            branch=_get(d, "branch", str, where),
            base_ref=_get(d, "base_ref", str, where),
            base_sha=_get(d, "base_sha", str, where),
            created_by_forgeflow=_get(d, "created_by_forgeflow", bool, where),
        )


@dataclass
class IssueLink:
    repo: str
    number: int
    url: str
    node_id: str
    title: str
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "number": self.number,
            "url": self.url,
            "node_id": self.node_id,
            "title": self.title,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str) -> IssueLink:
        return cls(
            repo=_get(d, "repo", str, where),
            number=_non_negative(_get(d, "number", int, where), f"{where}.number"),
            url=_get(d, "url", str, where),
            node_id=_get(d, "node_id", str, where),
            title=_get(d, "title", str, where),
            state=_get(d, "state", str, where),
        )


@dataclass
class ProjectItemLink:
    project_number: int
    project_title: str
    project_url: str
    item_id: str
    status: str | None = None  # value of the Project's "Status" field, when readable

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_number": self.project_number,
            "project_title": self.project_title,
            "project_url": self.project_url,
            "item_id": self.item_id,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str) -> ProjectItemLink:
        return cls(
            project_number=_get(d, "project_number", int, where),
            project_title=_get(d, "project_title", str, where),
            project_url=_get(d, "project_url", str, where),
            item_id=_get(d, "item_id", str, where),
            status=_get(d, "status", str, where, optional=True),
        )


@dataclass
class PullRequestLink:
    number: int
    url: str
    is_draft: bool
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "url": self.url,
            "is_draft": self.is_draft,
            "state": self.state,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str) -> PullRequestLink:
        return cls(
            number=_get(d, "number", int, where),
            url=_get(d, "url", str, where),
            is_draft=_get(d, "is_draft", bool, where),
            state=_get(d, "state", str, where),
        )


@dataclass
class GitHubLinks:
    issue: IssueLink | None = None
    project_items: list[ProjectItemLink] = field(default_factory=list)
    # "ok", "not-checked", or "blocked: <reason>" - a missing Project link is never silent.
    project_lookup: str = "not-checked"
    pull_request: PullRequestLink | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue.to_dict() if self.issue else None,
            "project_items": [p.to_dict() for p in self.project_items],
            "project_lookup": self.project_lookup,
            "pull_request": self.pull_request.to_dict() if self.pull_request else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "github") -> GitHubLinks:
        issue = _obj(d, "issue", where, optional=True)
        pr = _obj(d, "pull_request", where, optional=True)
        items = _get(d, "project_items", list, where, optional=True) or []
        parsed_items = []
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValidationError(f"{where}.project_items[{i}] must be an object")
            parsed_items.append(ProjectItemLink.from_dict(item, f"{where}.project_items[{i}]"))
        return cls(
            issue=IssueLink.from_dict(issue, f"{where}.issue") if issue else None,
            project_items=parsed_items,
            project_lookup=_get(d, "project_lookup", str, where, optional=True) or "not-checked",
            pull_request=PullRequestLink.from_dict(pr, f"{where}.pull_request") if pr else None,
        )


@dataclass
class Attempts:
    build: int = 0  # builder dispatches, including rework rounds and resumes
    rework: int = 0  # rounds triggered by failed checks or review findings
    resumes: int = 0  # recoveries after interruption
    max_rework: int = DEFAULT_MAX_REWORK

    def to_dict(self) -> dict[str, Any]:
        return {
            "build": self.build,
            "rework": self.rework,
            "resumes": self.resumes,
            "max_rework": self.max_rework,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "attempts") -> Attempts:
        return cls(
            **{
                k: _non_negative(_get(d, k, int, where), f"{where}.{k}")
                for k in ("build", "rework", "resumes", "max_rework")
            }
        )


@dataclass
class Lease:
    """Which process owns the job's worker role right now. One writer per job."""

    token: str
    role: str
    provider: str
    holder_pid: int
    holder_host: str
    acquired_at: str
    worker_pid: int | None = None  # the provider subprocess, once spawned

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "role": self.role,
            "provider": self.provider,
            "holder_pid": self.holder_pid,
            "holder_host": self.holder_host,
            "acquired_at": self.acquired_at,
            "worker_pid": self.worker_pid,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str = "lease") -> Lease:
        return cls(
            token=_get(d, "token", str, where),
            role=_get(d, "role", str, where),
            provider=_get(d, "provider", str, where),
            holder_pid=_get(d, "holder_pid", int, where),
            holder_host=_get(d, "holder_host", str, where),
            acquired_at=_get(d, "acquired_at", str, where),
            worker_pid=_get(d, "worker_pid", int, where, optional=True),
        )


@dataclass
class Evidence:
    kind: str  # e.g. "provider-result", "checks", "review"
    path: str  # relative to the job directory
    status: str  # e.g. "passed", "failed", "succeeded", "recorded"
    mocked: bool  # True when produced by a mock provider or fixture, never by a real run
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "status": self.status,
            "mocked": self.mocked,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any], where: str) -> Evidence:
        return cls(
            kind=_get(d, "kind", str, where),
            path=_get(d, "path", str, where),
            status=_get(d, "status", str, where),
            mocked=_get(d, "mocked", bool, where),
            created_at=_get(d, "created_at", str, where),
        )


@dataclass
class Job:
    job_id: str
    title: str
    state: JobState
    repo: Repo
    policy: Policy
    created_at: str
    updated_at: str
    plan: Plan = field(default_factory=Plan)
    worktree: Worktree | None = None
    github: GitHubLinks = field(default_factory=GitHubLinks)
    attempts: Attempts = field(default_factory=Attempts)
    lease: Lease | None = None
    interrupted_from: JobState | None = None
    evidence: list[Evidence] = field(default_factory=list)
    revision: int = 0
    schema_version: int = SCHEMA_VERSION

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "title": self.title,
            "state": self.state.value,
            "repo": self.repo.to_dict(),
            "policy": self.policy.to_dict(),
            "plan": self.plan.to_dict(),
            "worktree": self.worktree.to_dict() if self.worktree else None,
            "github": self.github.to_dict(),
            "attempts": self.attempts.to_dict(),
            "lease": self.lease.to_dict() if self.lease else None,
            "interrupted_from": self.interrupted_from.value if self.interrupted_from else None,
            "evidence": [e.to_dict() for e in self.evidence],
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Any) -> Job:
        if not isinstance(d, dict):
            raise ValidationError("job record must be a JSON object")
        version = _get(d, "schema_version", int, "job")
        if version != SCHEMA_VERSION:
            raise ValidationError(f"unsupported job schema_version {version}")
        state = _parse_state(_get(d, "state", str, "job"), "job.state")
        interrupted_raw = _get(d, "interrupted_from", str, "job", optional=True)
        interrupted_from = (
            _parse_state(interrupted_raw, "job.interrupted_from") if interrupted_raw else None
        )
        if state is S.INTERRUPTED and interrupted_from not in WORKER_STATES:
            raise ValidationError("job.interrupted_from must name a worker state when interrupted")
        worktree = _obj(d, "worktree", "job", optional=True)
        lease = _obj(d, "lease", "job", optional=True)
        evidence = _get(d, "evidence", list, "job", optional=True) or []
        parsed_evidence = []
        for i, item in enumerate(evidence):
            if not isinstance(item, dict):
                raise ValidationError(f"job.evidence[{i}] must be an object")
            parsed_evidence.append(Evidence.from_dict(item, f"job.evidence[{i}]"))
        title = _get(d, "title", str, "job")
        if not title.strip():
            raise ValidationError("job.title must not be empty")
        return cls(
            job_id=validate_job_id(_get(d, "job_id", str, "job")),
            title=title,
            state=state,
            repo=Repo.from_dict(_obj(d, "repo", "job")),
            policy=Policy.from_dict(_obj(d, "policy", "job")),
            plan=Plan.from_dict(_obj(d, "plan", "job", optional=True) or {}),
            worktree=Worktree.from_dict(worktree) if worktree else None,
            github=GitHubLinks.from_dict(_obj(d, "github", "job", optional=True) or {}),
            attempts=Attempts.from_dict(_obj(d, "attempts", "job")),
            lease=Lease.from_dict(lease) if lease else None,
            interrupted_from=interrupted_from,
            evidence=parsed_evidence,
            revision=_non_negative(_get(d, "revision", int, "job"), "job.revision"),
            created_at=_get(d, "created_at", str, "job"),
            updated_at=_get(d, "updated_at", str, "job"),
        )


def _parse_state(value: str, where: str) -> JobState:
    try:
        return JobState(value)
    except ValueError as exc:
        raise ValidationError(f"{where} has unknown state {value!r}") from exc


def allowed_targets(state: JobState) -> frozenset[JobState]:
    return TRANSITIONS[state]
