"""Provider contract for Claude Code and Codex CLI, plus a clearly-labelled mock.

Detection (executables, versions, auth status and CLI options parsed from ``--help``) and
execution are real. The invocations below were verified against the installed CLIs (Claude
Code 2.1.311, Codex CLI 0.164.0, Windows 11) before they were written; see ADR 0005.

* **Claude Code builder:** ``claude -p --restricted --tools Read,Edit,Write,Glob,Grep
  --permission-mode acceptEdits --permission-prompts none --strict-mcp-config``. It has no
  command-running tool at all, its file tools are confined to the worktree (a write outside is
  denied and reported in ``permission_denials``), and user/project settings, hooks and MCP
  servers are not loaded.
* **Claude Code reviewer:** the same with ``--tools Read,Glob,Grep --permission-mode dontAsk``
  and ``--json-schema`` (the answer arrives in ``structured_output``).
* **Codex builder:** ``codex exec -s workspace-write -C <worktree>``; **reviewer:** ``-s
  read-only`` with ``--output-schema``. Both use a minimal independent ``CODEX_HOME``;
  configuration sources and effective MCP/plugin inventories are checked before dispatch. On
  Windows its unelevated sandbox refuses every write in a workspace under ``%TEMP%``.

Providers never commit, push or open pull requests: ForgeFlow does that after checking the
diff. The prompt goes on stdin. The subprocess PID (and start time) is recorded in the job
lease while it runs, so no second writer can start next to it. ``MockProvider`` and a provider
built with a substitute ``command`` (tests) mark every result ``mocked=True``.

Detection never treats "installed and logged in" as permission to run unrestricted: what a
provider may do is decided per task by ``ProviderTask.allowed_operations`` and the job lease.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from forgeflow import fsutil, prompts
from forgeflow.codex_runtime import (
    ISOLATION_ARGS,
    CodexAuthenticationError,
    CodexRuntime,
    CodexRuntimeError,
)
from forgeflow.errors import LeaseError, ProviderError, ToolNotFoundError, ValidationError
from forgeflow.jobs import Policy
from forgeflow.proc import (
    CommandResult,
    Runner,
    TrackedRunner,
    redact_credentials,
    run_command,
    run_tracked,
    which,
)

if TYPE_CHECKING:
    from forgeflow.store import JobStore, LeaseHandle

ROLES = ("builder", "reviewer")
WRITE_OPERATIONS = frozenset({"write", "git-commit"})
KNOWN_OPERATIONS = frozenset({"read", "run-validation", *WRITE_OPERATIONS})
_TAIL = 4000  # characters of provider output kept as evidence

# The four execution policies are data, not four implementations.
NAMED_POLICIES: dict[str, Policy] = {
    "claude-only": Policy(builder="claude", reviewer="claude"),
    "codex-only": Policy(builder="codex", reviewer="codex"),
    "claude-build-codex-review": Policy(builder="claude", reviewer="codex"),
    "codex-build-claude-review": Policy(builder="codex", reviewer="claude"),
}


class ProviderUnavailableError(ProviderError):
    """A provider cannot run this role safely here; nothing was dispatched."""


@dataclass(frozen=True)
class ProviderTask:
    """Everything a provider receives for one unit of work. Nothing implicit."""

    job_id: str
    role: str
    repo_slug: str | None
    worktree_path: str  # builder: the job's worktree; reviewer: a disposable snapshot
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
    protected_paths: tuple[str, ...] = ()  # changes here are refused and escalated
    feedback: tuple[str, ...] = ()  # rework: what the previous round left to fix
    review_commit: str = ""  # reviewer: the commit under review
    review_diff: str = ""  # reviewer: the diff base..commit (possibly truncated)
    review_checks: str = ""  # reviewer: summary of the checks that ran on the commit
    # reviewer: snapshot file whose (unprompted) content proves the snapshot was readable
    review_token_file: str = ""
    model: str | None = None  # passed through to the CLI only when set

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
        for pattern in (*self.allowed_paths, *self.context_files, *self.protected_paths):
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

    def is_protected(self, rel_path: str) -> bool:
        """True if ``rel_path`` matches a protected pattern (compared case-insensitively)."""
        posix = rel_path.replace("\\", "/").lower()
        return any(fnmatch.fnmatchcase(posix, p.lower()) for p in self.protected_paths)

    def permitted_target(self, rel_path: str) -> Path:
        """The real file a write to ``rel_path`` would touch, if every check allows it.

        Symlinks and junctions are resolved first and the resolved path must still be inside
        the worktree and inside an allowed pattern.
        """
        if not self.permits_path(rel_path) or self.is_protected(rel_path):
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
        data = asdict(self)
        data.pop("review_diff")  # large and reconstructible from the two commits
        return data


@dataclass(frozen=True)
class UsageReport:
    """Token/cost information with its provenance. ``None`` means unknown, never zero."""

    tokens: str = "unavailable"  # "measured" | "unavailable"
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: str = "unavailable"  # "api-equivalent-estimate" | "billed" | "unavailable"
    cost_usd: float | None = None
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict)  # the provider's own usage record


@dataclass(frozen=True)
class ProviderResult:
    provider: str
    role: str
    status: str  # "succeeded" | "failed" | "timed_out" | "blocked"
    summary: str
    changed_files: tuple[str, ...]
    usage: UsageReport
    mocked: bool
    verdict: str | None = None  # reviewer: "approve" | "request-changes" (ForgeFlow's reading)
    findings: tuple[str, ...] = ()
    review: dict[str, Any] | None = None  # reviewer: the structured answer as returned
    notes: tuple[str, ...] = ()  # e.g. denied tool calls, failed writes, sandbox refusals
    exit_code: int | None = None
    stderr_tail: str = ""

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
    isolation_error: str = ""

    @property
    def installed(self) -> bool:
        return self.executable is not None

    @property
    def available(self) -> bool:
        return self.installed and self.authenticated is True and not self.isolation_error

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["installed"] = self.installed
        data["available"] = self.available
        return data


class Provider(ABC):
    name: str
    # True only for a provider that runs the real installed CLI.
    real: bool = False

    @abstractmethod
    def detect(self, runner: Runner = run_command) -> ProviderCapabilities: ...

    @abstractmethod
    def execute(self, task: ProviderTask, lease: LeaseHandle) -> ProviderResult: ...

    def preflight(self, role: str, workdir: Path | None = None) -> None:  # noqa: B027
        """Raise ProviderUnavailableError if ``role`` cannot run safely here. Default: fine."""


def _safe(
    runner: Runner,
    argv: list[str],
    timeout: float = 30,
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult | None:
    try:
        return runner(argv, timeout=timeout, cwd=cwd, env=env)
    except ToolNotFoundError:
        return None


def _flags_in(help_text: str, flags: tuple[str, ...]) -> dict[str, bool]:
    return {flag: flag in help_text for flag in flags}


def _first_line(text: str) -> str | None:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else None


def interpret_review(provider: str, answer: Any) -> tuple[str, tuple[str, ...], dict[str, Any]]:
    """(verdict, findings, review) from a reviewer's structured answer.

    ForgeFlow decides the verdict from the findings, not from the reviewer's own word: changes
    are requested only for a blocking or major finding, so a "request-changes" with only minor
    notes does not trigger a rework round, and an "approve" that lists a blocking finding does.
    """
    if not isinstance(answer, dict):
        raise ProviderError(f"{provider} reviewer did not return a JSON object")
    findings_raw = answer.get("findings")
    if (
        answer.get("verdict") not in ("approve", "request-changes")
        or not isinstance(findings_raw, list)
        or not isinstance(answer.get("snapshot_token"), str)
        or not isinstance(answer.get("inspection_complete"), bool)
    ):
        raise ProviderError(f"{provider} reviewer answer does not match the review schema")
    findings: list[str] = []
    actionable = 0
    for item in findings_raw:
        if not isinstance(item, dict):
            raise ProviderError(f"{provider} reviewer returned a malformed finding")
        severity = str(item.get("severity", "")).lower()
        if severity in prompts.ACTIONABLE_SEVERITIES:
            actionable += 1
        findings.append(
            f"[{severity or '?'}] {item.get('file') or '-'}: {item.get('issue', '').strip()}"
            + (f" (suggestion: {item['suggestion'].strip()})" if item.get("suggestion") else "")
        )
    verdict = "request-changes" if actionable else "approve"
    return verdict, tuple(findings), answer


class _CliProvider(Provider):
    """Shared plumbing: the executable, subprocess tracking in the lease, result assembly."""

    executable_name: str
    feature_flags: tuple[str, ...] = ()
    role_flags: dict[str, tuple[str, ...]] = {}

    def __init__(
        self,
        *,
        command: Sequence[str] | None = None,
        runner: TrackedRunner = run_tracked,
    ) -> None:
        # ``command`` substitutes the executable (tests); its results are always mocked.
        self.command = tuple(command) if command else None
        self.runner = runner
        self.real = command is None

    def _argv0(self) -> list[str]:
        return list(self.command) if self.command else [self.executable_name]

    def preflight(self, role: str, workdir: Path | None = None) -> None:
        if self.command is not None:
            return
        caps = self._detect_for_workdir(workdir)
        problems: list[str] = []
        if not caps.installed:
            problems.append(f"`{self.executable_name}` is not installed or not on PATH")
        else:
            if caps.isolation_error:
                problems.append(caps.isolation_error)
            if caps.authenticated is not True:
                state = "not logged in" if caps.authenticated is False else "unverified"
                problems.append(f"authentication is {state} ({caps.auth_detail or 'no detail'})")
            missing = [f for f in self.role_flags.get(role, ()) if not caps.features.get(f)]
            if missing:
                problems.append(
                    f"{caps.version or 'this version'} lacks options ForgeFlow relies on for a "
                    f"{role}: {', '.join(missing)}"
                )
        problems += self._workspace_problems(role, workdir)
        if problems:
            raise ProviderUnavailableError(
                f"{self.name} cannot run as {role}: " + "; ".join(problems)
            )

    def _workspace_problems(self, role: str, workdir: Path | None) -> list[str]:
        return []

    def _detect_for_workdir(self, workdir: Path | None) -> ProviderCapabilities:
        return self.detect()

    def _run(
        self,
        task: ProviderTask,
        lease: LeaseHandle,
        argv: list[str],
        prompt: str,
        *,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        task.validate()
        if lease.job_id != task.job_id or not lease.active:
            raise ProviderError("the task's job lease is not held")

        def record(pid: int) -> None:
            lease.set_worker_pid(pid)  # before the provider does any work

        try:
            return self.runner(
                argv,
                cwd=Path(task.worktree_path),
                timeout=task.timeout_seconds,
                input_text=prompt,
                on_start=record,
                env=env,
            )
        except ToolNotFoundError as exc:
            raise ProviderUnavailableError(f"{self.name}: {exc}") from exc
        finally:
            try:
                lease.set_worker_pid(None)  # the whole process tree is gone by now
            except LeaseError:
                pass

    def _result(self, task: ProviderTask, result: CommandResult, **fields: Any) -> ProviderResult:
        status = fields.pop("status", None)
        if status is None:
            status = "timed_out" if result.timed_out else "succeeded" if result.ok else "failed"
        return ProviderResult(
            provider=self.name,
            role=task.role,
            status=status,
            changed_files=(),  # ForgeFlow asks git, not the provider, what changed
            mocked=not self.real,
            exit_code=result.returncode,
            stderr_tail=redact_credentials(result.stderr[-_TAIL:]),
            **fields,
        )


class ClaudeCodeProvider(_CliProvider):
    name = "claude"
    executable_name = "claude"
    # Options the builder/reviewer invocations rely on; detected, not assumed.
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
        "--tools",
        "--restricted",
        "--strict-mcp-config",
        "--permission-prompts",
        "--no-session-persistence",
    )
    _common_flags = (
        "--print",
        "--output-format",
        "--permission-mode",
        "--tools",
        "--restricted",
        "--strict-mcp-config",
        "--permission-prompts",
        "--no-session-persistence",
    )
    role_flags = {"builder": _common_flags, "reviewer": (*_common_flags, "--json-schema")}
    builder_tools = "Read,Edit,Write,Glob,Grep"
    reviewer_tools = "Read,Glob,Grep"

    def detect(self, runner: Runner = run_command) -> ProviderCapabilities:
        caps = ProviderCapabilities(self.name, which(self.executable_name))
        if not caps.installed:
            caps.notes.append("`claude` is not on PATH")
            return caps
        version = _safe(runner, [self.executable_name, "--version"])
        if version and version.ok:
            caps.version = _first_line(version.stdout)
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
        return caps

    def argv(self, task: ProviderTask) -> list[str]:
        argv = [
            *self._argv0(),
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--restricted",
            "--strict-mcp-config",
            "--permission-prompts",
            "none",
        ]
        if task.role == "builder":
            argv += ["--tools", self.builder_tools, "--permission-mode", "acceptEdits"]
        else:
            argv += [
                "--tools",
                self.reviewer_tools,
                "--permission-mode",
                "dontAsk",
                "--json-schema",
                json.dumps(prompts.REVIEW_SCHEMA, separators=(",", ":")),
            ]
        if task.model:
            argv += ["--model", task.model]
        return argv

    def execute(self, task: ProviderTask, lease: LeaseHandle) -> ProviderResult:
        prompt = (
            prompts.builder_prompt(task)
            if task.role == "builder"
            else prompts.reviewer_prompt(task)
        )
        result = self._run(task, lease, self.argv(task), prompt)
        if result.timed_out:
            return self._result(task, result, summary="timed out", usage=UsageReport())
        try:
            data = json.loads(result.stdout)
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except ValueError:
            detail = _first_line(result.stderr) or _first_line(result.stdout) or "no output"
            return self._result(
                task,
                result,
                status="failed",
                summary=f"claude did not return its JSON result: {detail}",
                usage=UsageReport(),
            )
        usage = _claude_usage(data)
        notes = tuple(
            f"denied {d.get('tool_name', '?')}: {json.dumps(d.get('tool_input', {}))[:300]}"
            for d in data.get("permission_denials") or []
            if isinstance(d, dict)
        )
        summary = str(data.get("result") or data.get("subtype") or "").strip()
        failed = bool(data.get("is_error")) or data.get("subtype") != "success" or not result.ok
        if failed:
            return self._result(
                task, result, status="failed", summary=summary, usage=usage, notes=notes
            )
        if task.role == "builder":
            return self._result(task, result, summary=summary, usage=usage, notes=notes)
        verdict, findings, review = interpret_review(self.name, data.get("structured_output"))
        return self._result(
            task,
            result,
            summary=str(review.get("summary", "")),
            usage=usage,
            notes=notes,
            verdict=verdict,
            findings=findings,
            review=review,
        )


def _claude_usage(data: dict[str, Any]) -> UsageReport:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return UsageReport(source="claude reported no usage")
    cost = data.get("total_cost_usd")
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return UsageReport(
        tokens="measured",
        input_tokens=sum(int(usage.get(k) or 0) for k in keys),
        output_tokens=int(usage.get("output_tokens") or 0),
        # Claude Code prices usage at list rates; on a subscription nothing is billed per call.
        cost="api-equivalent-estimate" if isinstance(cost, int | float) else "unavailable",
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        source="claude -p --output-format json (input includes cache reads and writes)",
        raw={k: usage.get(k) for k in (*keys, "output_tokens")},
    )


class CodexProvider(_CliProvider):
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
        "--skip-git-repo-check",
    )
    _common_flags = ("--sandbox", "--json", "--cd", "--output-last-message", "--ephemeral")
    role_flags = {
        "builder": (*_common_flags, "workspace-write"),
        "reviewer": (*_common_flags, "read-only", "--output-schema", "--skip-git-repo-check"),
    }
    # Messages with which Codex's sandbox refuses to run rather than run unsandboxed.
    _SANDBOX_REFUSALS = ("refusing to run unsandboxed", "UnsupportedOperation")

    def _argv0(self) -> list[str]:
        return [*super()._argv0(), *ISOLATION_ARGS]

    def detect(
        self,
        runner: Runner = run_command,
        *,
        workdir: Path | None = None,
    ) -> ProviderCapabilities:
        caps = ProviderCapabilities(self.name, which(self.executable_name))
        if not caps.installed:
            caps.notes.append("`codex` is not on PATH")
            return caps
        try:
            runtime = CodexRuntime.prepare(workdir)
            caps.notes.append(f"isolated CODEX_HOME: {runtime.home}")
            version = _safe(runner, [*self._argv0(), "--version"], cwd=runtime.cwd, env=runtime.env)
            if version and version.ok:
                caps.version = _first_line(version.stdout)
            help_result = _safe(
                runner, [*self._argv0(), "exec", "--help"], cwd=runtime.cwd, env=runtime.env
            )
            if help_result and help_result.ok:
                caps.features = _flags_in(help_result.stdout, self.feature_flags)
            runtime.verify(runner, self._argv0())
            caps.authenticated = True
            caps.auth_detail = "authenticated with the isolated Codex profile"
            caps.notes.append(
                "plugin/account features verified disabled; effective MCP/plugin inventories empty"
            )
        except CodexAuthenticationError as exc:
            caps.authenticated = False
            caps.auth_detail = str(exc)
        except CodexRuntimeError as exc:
            caps.isolation_error = str(exc)
            caps.auth_detail = "isolated runtime unavailable"
            caps.notes.append(str(exc))
        return caps

    def _detect_for_workdir(self, workdir: Path | None) -> ProviderCapabilities:
        return self.detect(workdir=workdir)

    def _workspace_problems(self, role: str, workdir: Path | None) -> list[str]:
        if sys.platform != "win32" or workdir is None or role != "builder":
            return []
        temp = Path(tempfile.gettempdir())
        try:
            fsutil.ensure_within(temp, workdir)
        except ValidationError:
            return []
        return [
            f"the worktree {workdir} is under %TEMP% ({temp}), where Codex's Windows sandbox "
            "refuses every write (verified with Codex 0.164.0); move the repository elsewhere"
        ]

    def argv(self, task: ProviderTask, last_message: Path, schema: Path | None) -> list[str]:
        sandbox = "workspace-write" if task.role == "builder" else "read-only"
        argv = [
            *self._argv0(),
            "exec",
            "--json",
            "--ephemeral",
            "--color",
            "never",
            "-C",
            task.worktree_path,
            "-s",
            sandbox,
            "-c",
            'approval_policy="never"',
            "-o",
            str(last_message),
        ]
        if schema is not None:
            argv += ["--output-schema", str(schema), "--skip-git-repo-check"]
        if task.model:
            argv += ["-m", task.model]
        return [*argv, "-"]  # the prompt comes from stdin

    def execute(self, task: ProviderTask, lease: LeaseHandle) -> ProviderResult:
        # Validate before any CLI probe, including when execute is called directly.
        task.validate()
        if lease.job_id != task.job_id or not lease.active:
            raise ProviderError("the task's job lease is not held")
        try:
            runtime = CodexRuntime.prepare(Path(task.worktree_path))
            runtime.verify(self.runner, self._argv0())
        except CodexRuntimeError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        builder = task.role == "builder"
        prompt = prompts.builder_prompt(task) if builder else prompts.reviewer_prompt(task)
        # Outside the worktree, so they never appear as changes of the build.
        with tempfile.TemporaryDirectory(prefix="forgeflow-codex-") as scratch:
            last = Path(scratch) / "last-message.txt"
            schema = None if builder else Path(scratch) / "review-schema.json"
            if schema is not None:
                schema.write_text(json.dumps(prompts.REVIEW_SCHEMA), encoding="utf-8")
            result = self._run(task, lease, self.argv(task, last, schema), prompt, env=runtime.env)
            message = last.read_text(encoding="utf-8").strip() if last.is_file() else ""
        events = _jsonl(result.stdout)
        usage = _codex_usage(events)
        notes = (
            "Codex configuration isolation verified before execution: independent CODEX_HOME; "
            "plugin/account features disabled; empty MCP/plugin inventories. "
            "This is not operating-system isolation.",
            *_codex_notes(events),
        )
        refused = [
            line.strip()
            for line in result.stderr.splitlines()
            if any(r in line for r in self._SANDBOX_REFUSALS)
        ]
        if refused:
            notes += (f"sandbox refusal: {refused[0][:300]}",)
        if result.timed_out:
            return self._result(task, result, summary="timed out", usage=usage, notes=notes)
        failure = next((e for e in events if e.get("type") in ("turn.failed", "error")), None)
        if refused:  # builder or reviewer: its work is incomplete, whatever it answered
            return self._result(
                task,
                result,
                status="blocked",
                summary="Codex's sandbox refused to run (it does not run unsandboxed); see "
                f"notes. Choose another {task.role} or move the repository.",
                usage=usage,
                notes=notes,
            )
        if failure is not None or not result.ok:
            detail = json.dumps(failure)[:500] if failure else _first_line(result.stderr) or ""
            return self._result(
                task,
                result,
                status="failed",
                summary=message or f"codex exec failed: {detail}",
                usage=usage,
                notes=notes,
            )
        if builder:
            return self._result(task, result, summary=message, usage=usage, notes=notes)
        try:
            answer = json.loads(message)
        except ValueError as exc:
            raise ProviderError("codex reviewer did not return JSON") from exc
        verdict, findings, review = interpret_review(self.name, answer)
        return self._result(
            task,
            result,
            summary=str(review.get("summary", "")),
            usage=usage,
            notes=notes,
            verdict=verdict,
            findings=findings,
            review=review,
        )


def _jsonl(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _codex_usage(events: list[dict[str, Any]]) -> UsageReport:
    totals: dict[str, int] = {}
    for event in events:
        if event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            for key, value in event["usage"].items():
                if isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value
    if not totals:
        return UsageReport(source="codex reported no usage")
    return UsageReport(
        tokens="measured",
        input_tokens=totals.get("input_tokens"),
        output_tokens=totals.get("output_tokens"),
        source="codex exec --json turn.completed usage (input includes cached input)",
        raw=totals,
    )


def _codex_notes(events: list[dict[str, Any]]) -> tuple[str, ...]:
    notes = []
    for event in events:
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict):
            continue
        if item.get("type") == "file_change" and item.get("status") == "failed":
            paths = [c.get("path", "?") for c in item.get("changes") or [] if isinstance(c, dict)]
            notes.append(f"failed file change: {', '.join(paths)}")
    return tuple(notes)


REAL_PROVIDERS: dict[str, type[_CliProvider]] = {
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
    protected_paths: tuple[str, ...] = (),
    feedback: tuple[str, ...] = (),
    model: str | None = None,
) -> ProviderTask:
    """Derive a task from the job record so the approved plan and worktree cannot drift."""
    job = store.load(job_id)
    if job.worktree is None:
        raise ValidationError("the job has no worktree")
    if job.plan.path is None or job.plan.approval is None:
        raise ValidationError("the job's plan is not approved")
    # Builders write files; ForgeFlow alone commits them (dispatch.commit_build).
    operations = ("read", "write", "run-validation") if role == "builder" else ("read",)
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
        protected_paths=protected_paths,
        feedback=feedback,
        model=model,
    )
    task.validate()
    return task
