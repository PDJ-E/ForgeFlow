"""Provider contract for Claude Code and Codex CLI, plus a clearly-labelled mock.

Checkpoint A scope: *detection* of real providers is real (executables, versions, auth status
and CLI options parsed from ``--help``); *execution* of real providers is not implemented yet
and raises ``ProviderError``. ``MockProvider`` executes tasks for lifecycle tests and marks
every result ``mocked=True``.

Detection never treats "installed and logged in" as permission to run unrestricted: what a
provider may do is decided per task by ``ProviderTask.allowed_operations`` and the job lease.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from forgeflow import fsutil
from forgeflow.errors import ProviderError, ToolNotFoundError, ValidationError
from forgeflow.jobs import Policy
from forgeflow.proc import CommandResult, Runner, run_command, which

if TYPE_CHECKING:
    from forgeflow.store import JobStore, LeaseHandle

ROLES = ("builder", "reviewer")
WRITE_OPERATIONS = frozenset({"write", "git-commit"})
KNOWN_OPERATIONS = frozenset({"read", "run-validation", *WRITE_OPERATIONS})

# The four execution policies are data, not four implementations.
NAMED_POLICIES: dict[str, Policy] = {
    "claude-only": Policy(builder="claude", reviewer="claude"),
    "codex-only": Policy(builder="codex", reviewer="codex"),
    "claude-build-codex-review": Policy(builder="claude", reviewer="codex"),
    "codex-build-claude-review": Policy(builder="codex", reviewer="claude"),
}


@dataclass(frozen=True)
class ProviderTask:
    """Everything a provider receives for one unit of work. Nothing implicit."""

    job_id: str
    role: str
    repo_slug: str | None
    worktree_path: str
    branch: str
    base_sha: str
    plan_path: str  # where the plan lives in the main checkout; for reference only
    plan_sha256: str
    plan_text: (
        str  # the approved plan itself, from the job's snapshot; never read it from a checkout
    )
    acceptance_criteria: tuple[str, ...]
    allowed_paths: tuple[str, ...]  # glob patterns, repository-relative, '/' separated
    allowed_operations: tuple[str, ...]
    validation_commands: tuple[tuple[str, ...], ...]
    context_files: tuple[str, ...] = ()
    evidence_requirements: tuple[str, ...] = ("summary", "changed_files")
    timeout_seconds: int = 1800
    max_attempts: int = 1

    def validate(self) -> None:
        if self.role not in ROLES:
            raise ValidationError(f"unknown role {self.role!r}")
        unknown = set(self.allowed_operations) - KNOWN_OPERATIONS
        if unknown:
            raise ValidationError(f"unknown operations: {sorted(unknown)}")
        if self.role == "reviewer" and WRITE_OPERATIONS & set(self.allowed_operations):
            raise ValidationError("a reviewer task must be read-only")
        if not self.acceptance_criteria:
            raise ValidationError("a task needs at least one acceptance criterion")
        for pattern in (*self.allowed_paths, *self.context_files):
            p = PurePosixPath(pattern)
            if p.is_absolute() or ".." in p.parts or "\\" in pattern or ":" in pattern:
                raise ValidationError(f"path pattern {pattern!r} must be repository-relative")
        if self.role == "builder" and not self.allowed_paths:
            raise ValidationError("a builder task must declare its allowed paths")
        if self.timeout_seconds <= 0 or self.max_attempts <= 0:
            raise ValidationError("timeout and attempts must be positive")
        if hashlib.sha256(self.plan_text.encode("utf-8")).hexdigest() != self.plan_sha256:
            raise ValidationError("the task's plan text is not the approved plan")

    def permits_path(self, rel_path: str) -> bool:
        """True if ``rel_path`` is a plain repository-relative path inside an allowed pattern.

        Paths are judged as written, never normalised: any empty, ``.`` or ``..`` segment,
        absolute path, drive or stream (``:``) or ``.git`` segment is refused outright, so
        ``src/*`` cannot reach ``src/../.github``. Segments are compared the way Windows reads
        them, without trailing dots and spaces (``".. "`` is ``".."`` there).
        """
        parts = [p.rstrip(". ") for p in rel_path.replace("\\", "/").split("/")]
        if ":" in rel_path or any(not p or p.lower() == ".git" for p in parts):
            return False
        posix = rel_path.replace("\\", "/")  # matched as written: "src./x" is not "src/x"
        return any(fnmatch.fnmatchcase(posix, pattern) for pattern in self.allowed_paths)

    def permitted_target(self, rel_path: str) -> Path:
        """The real file a write to ``rel_path`` would touch, if every check allows it.

        Symlinks and junctions are resolved first and the resolved path must still be inside
        the worktree and inside an allowed pattern.
        """
        if not self.permits_path(rel_path):
            raise ValidationError(f"{rel_path!r} is outside the allowed paths")
        root = Path(self.worktree_path)
        target = fsutil.ensure_within(root, root / rel_path)
        real_rel = Path(os.path.relpath(target, os.path.realpath(root))).as_posix()
        if not self.permits_path(real_rel):
            raise ValidationError(
                f"{rel_path!r} resolves to {real_rel!r}, outside the allowed paths"
            )
        return target

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class UsageReport:
    """Token/cost information with its provenance. ``None`` means unknown, never zero."""

    tokens: str = "unavailable"  # "measured" | "unavailable"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: str = "unavailable"  # "api-equivalent-estimate" | "billed" | "unavailable"
    cost_usd: float | None = None
    source: str = ""


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    role: str
    status: str  # "succeeded" | "failed" | "timed_out"
    summary: str
    changed_files: tuple[str, ...]
    usage: UsageReport
    mocked: bool
    verdict: str | None = None  # reviewer: "approve" | "request-changes"
    findings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderCapabilities:
    provider: str
    executable: str | None
    version: str | None = None
    authenticated: bool | None = None  # None = could not determine
    auth_detail: str = ""
    features: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return self.executable is not None

    @property
    def available(self) -> bool:
        return self.installed and self.authenticated is True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["installed"] = self.installed
        data["available"] = self.available
        return data


class Provider(ABC):
    name: str

    @abstractmethod
    def detect(self, runner: Runner = run_command) -> ProviderCapabilities: ...

    @abstractmethod
    def execute(self, task: ProviderTask, lease: LeaseHandle) -> ProviderResult: ...


def _safe(runner: Runner, argv: list[str], timeout: float = 30) -> CommandResult | None:
    try:
        return runner(argv, timeout=timeout)
    except ToolNotFoundError:
        return None


def _flags_in(help_text: str, flags: tuple[str, ...]) -> dict[str, bool]:
    return {flag: flag in help_text for flag in flags}


class ClaudeCodeProvider(Provider):
    name = "claude"
    executable_name = "claude"
    # Options a future builder/reviewer invocation would rely on; detected, not assumed.
    feature_flags = (
        "--print",
        "--output-format",
        "--permission-mode",
        "--allowedTools",
        "--disallowedTools",
        "--json-schema",
        "--max-budget-usd",
        "--add-dir",
        "--model",
    )

    def detect(self, runner: Runner = run_command) -> ProviderCapabilities:
        caps = ProviderCapabilities(self.name, which(self.executable_name))
        if not caps.installed:
            caps.notes.append("`claude` is not on PATH")
            return caps
        version = _safe(runner, [self.executable_name, "--version"])
        if version and version.ok:
            caps.version = (
                version.stdout.strip().splitlines()[0] if version.stdout.strip() else None
            )
        help_result = _safe(runner, [self.executable_name, "--help"])
        if help_result and help_result.ok:
            caps.features = _flags_in(help_result.stdout, self.feature_flags)
        auth = _safe(runner, [self.executable_name, "auth", "status"])
        if auth is None or auth.timed_out:
            caps.auth_detail = "`claude auth status` did not complete"
        else:
            try:
                data = json.loads(auth.stdout)
                caps.authenticated = bool(data.get("loggedIn"))
                caps.auth_detail = f"method: {data.get('authMethod', 'unknown')}"
            except (ValueError, AttributeError):
                caps.auth_detail = "`claude auth status` output was not recognised"
        caps.notes.append("execution is not implemented in Checkpoint A")
        return caps

    def execute(self, task: ProviderTask, lease: LeaseHandle) -> ProviderResult:
        raise ProviderError("real Claude Code execution is not implemented until Checkpoint C")


class CodexProvider(Provider):
    name = "codex"
    executable_name = "codex"
    feature_flags = (
        "--sandbox",
        "read-only",
        "workspace-write",
        "--json",
        "--output-schema",
        "--cd",
        "--output-last-message",
        "--ephemeral",
        "--model",
    )

    def detect(self, runner: Runner = run_command) -> ProviderCapabilities:
        caps = ProviderCapabilities(self.name, which(self.executable_name))
        if not caps.installed:
            caps.notes.append("`codex` is not on PATH")
            return caps
        version = _safe(runner, [self.executable_name, "--version"])
        if version and version.ok:
            caps.version = (
                version.stdout.strip().splitlines()[0] if version.stdout.strip() else None
            )
        help_result = _safe(runner, [self.executable_name, "exec", "--help"])
        if help_result and help_result.ok:
            caps.features = _flags_in(help_result.stdout, self.feature_flags)
        auth = _safe(runner, [self.executable_name, "login", "status"])
        if auth is None or auth.timed_out:
            caps.auth_detail = "`codex login status` did not complete"
        else:
            text = (auth.stdout + auth.stderr).strip()
            caps.authenticated = (
                auth.returncode == 0
                and "logged in" in text.lower()
                and "not logged in" not in text.lower()
            )
            caps.auth_detail = text.splitlines()[0] if text else f"exit {auth.returncode}"
        caps.notes.append("execution is not implemented in Checkpoint A")
        return caps

    def execute(self, task: ProviderTask, lease: LeaseHandle) -> ProviderResult:
        raise ProviderError("real Codex CLI execution is not implemented until Checkpoint C")


REAL_PROVIDERS: dict[str, type[Provider]] = {
    ClaudeCodeProvider.name: ClaudeCodeProvider,
    CodexProvider.name: CodexProvider,
}


def validate_policy(policy: Policy, known: set[str] | None = None) -> Policy:
    names = known if known is not None else set(REAL_PROVIDERS)
    if policy.builder not in names:
        raise ValidationError(f"unknown builder provider {policy.builder!r}")
    if policy.reviewer is not None and policy.reviewer not in names:
        raise ValidationError(f"unknown reviewer provider {policy.reviewer!r}")
    return policy


class MockProvider(Provider):
    """Deterministic stand-in for tests. Every result it returns is marked ``mocked=True``."""

    name = "mock"

    def __init__(
        self,
        name: str = "mock",
        *,
        writes: dict[str, str] | None = None,
        status: str = "succeeded",
        verdict: str | None = None,
        findings: tuple[str, ...] = (),
    ) -> None:
        self.name = name  # a mock can stand in for any policy slot, e.g. "claude"
        self.writes = writes or {}
        self.status = status
        self.verdict = verdict
        self.findings = findings
        self.executed: list[ProviderTask] = []

    def detect(self, runner: Runner = run_command) -> ProviderCapabilities:
        return ProviderCapabilities(
            self.name,
            executable="<mock>",
            version="mock",
            authenticated=True,
            auth_detail="mock provider",
            notes=["mock provider: no real model is invoked"],
        )

    def execute(self, task: ProviderTask, lease: LeaseHandle) -> ProviderResult:
        task.validate()
        if lease.job_id != task.job_id or not lease.active:
            raise ProviderError("the task's job lease is not held")
        self.executed.append(task)
        if self.writes and task.role != "builder":
            raise ProviderError("mock reviewer attempted to write")
        if self.writes and "write" not in task.allowed_operations:
            raise ProviderError("task does not allow writes")
        for rel, content in self.writes.items():
            try:
                target = task.permitted_target(rel)
            except ValidationError as exc:
                raise ProviderError(f"mock write refused: {exc}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return ProviderResult(
            provider=self.name,
            role=task.role,
            status=self.status,
            summary=f"mock {task.role} run for {task.job_id}",
            changed_files=tuple(sorted(self.writes)),
            usage=UsageReport(source="mock provider: no usage exists"),
            mocked=True,
            verdict=self.verdict,
            findings=self.findings,
        )


def build_task(
    store: JobStore,
    job_id: str,
    *,
    role: str,
    acceptance_criteria: tuple[str, ...],
    allowed_paths: tuple[str, ...] = (),
    validation_commands: tuple[tuple[str, ...], ...] = (),
    context_files: tuple[str, ...] = (),
    timeout_seconds: int = 1800,
) -> ProviderTask:
    """Derive a task from the job record so the approved plan and worktree cannot drift."""
    job = store.load(job_id)
    if job.worktree is None:
        raise ValidationError("the job has no worktree")
    if job.plan.path is None or job.plan.approval is None:
        raise ValidationError("the job's plan is not approved")
    operations = (
        ("read", "write", "git-commit", "run-validation")
        if role == "builder"
        else ("read", "run-validation")
    )
    task = ProviderTask(
        job_id=job.job_id,
        role=role,
        repo_slug=job.repo.slug,
        worktree_path=job.worktree.path,
        branch=job.worktree.branch,
        base_sha=job.worktree.base_sha,
        plan_path=job.plan.path,
        plan_sha256=job.plan.approval.subject_sha256,
        plan_text=store.approved_plan_text(job),
        acceptance_criteria=acceptance_criteria,
        allowed_paths=allowed_paths,
        allowed_operations=operations,
        validation_commands=validation_commands,
        context_files=context_files,
        timeout_seconds=timeout_seconds,
    )
    task.validate()
    return task
