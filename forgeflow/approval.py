"""Gate A: capture a human's approval of one exact plan version at an interactive console.

``approve_plan`` is the only way a job reaches PLAN_APPROVED (``JobStore.transition`` refuses
it). It runs only when **both** standard input and standard output are an interactive console:

* Windows: ``GetConsoleMode`` must succeed on both handles. ``os.isatty`` is not enough there,
  because it also reports the ``NUL`` device as a terminal.
* POSIX: both file descriptors must be terminals.

An agent's ordinary tool calls (Claude Code's Bash tool, ``codex exec``, a piped ``echo``) have
no such console, so the command refuses and records nothing. The human sees the full plan from
its byte-exact snapshot, the version, the sha256 and what the approval authorises, then types a
one-time code. The code guards against approving by accident (a stray Enter, a pasted script);
it is not the security property, which is the console requirement. The decision is committed
against the job revision that was displayed, after re-hashing the plan, so an edit or a
resubmission made while the human was reading is refused rather than approved.

Threat model and limits (ADR 0004): this separates a human at their own terminal from an agent
working through its normal non-interactive tools, and from model-generated claims such as "the
user approved this". It does not stop a same-user process that deliberately drives a
pseudo-console, edits ``.forge/runtime`` directly or controls the screen. ForgeFlow is local,
single-user software, not an authentication service.
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from forgeflow.errors import ConcurrencyError, ForgeFlowError, InvalidTransitionError
from forgeflow.jobs import HUMAN_APPROVAL_METHOD, Approval, Job, JobState, utc_now
from forgeflow.store import JobStore

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L


class ApprovalUnavailableError(ForgeFlowError):
    """No interactive console: Gate A cannot be captured here, and nothing was recorded."""


class ApprovalDeclinedError(ForgeFlowError):
    """The human did not confirm. Nothing was recorded."""


class HumanConsole(Protocol):
    kind: str

    def write(self, text: str) -> None: ...

    def read_line(self, prompt: str) -> str: ...


class _StdioConsole:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def read_line(self, prompt: str) -> str:
        return input(prompt)


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    def _is_console(fd: int) -> bool:
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return False
        mode = wintypes.DWORD()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(mode)))

    _CONSOLE_KIND = "windows-console"
else:

    def _is_console(fd: int) -> bool:
        try:
            return os.isatty(fd)
        except OSError:
            return False

    _CONSOLE_KIND = "posix-tty"


def open_human_console() -> HumanConsole:
    """The interactive console the human is sitting at, or ApprovalUnavailableError."""
    streams = (sys.stdin, sys.stdout)
    try:
        interactive = all(s is not None and _is_console(s.fileno()) for s in streams)
    except (AttributeError, OSError, ValueError):
        interactive = False
    if not interactive:
        raise ApprovalUnavailableError(
            "Gate A needs a human at an interactive console, and this process has none "
            "(its input or output is a pipe, a file or NUL, as for an agent's tool call). "
            "Nothing was approved."
        )
    return _StdioConsole(_CONSOLE_KIND)


@dataclass(frozen=True)
class ApprovalContext:
    """What the human is told besides the plan itself; produced by the CLI."""

    summary: Sequence[str] = ()  # e.g. tasks found in the plan
    authorises: Sequence[str] = ()
    does_not_authorise: Sequence[str] = ()


DEFAULT_NOT_AUTHORISED = (
    "writing or changing code, creating worktrees, branches or pull requests",
    "dispatching Claude Code, Codex or any other builder (Checkpoint C, separately gated)",
    "merging, deploying or any production operation",
    "publishing discovery notes or local knowledge (they never leave this machine)",
    "any later version of this plan: any change invalidates this approval",
)


def one_time_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def approve_plan(store: JobStore, job_id: str, context: ApprovalContext | None = None) -> Job:
    """Show the submitted plan to a human and record their explicit approval of exactly it."""
    context = context or ApprovalContext()
    job = store.load(job_id)
    if job.state is not JobState.AWAITING_PLAN_APPROVAL:
        hint = {
            JobState.DRAFT: "submit a plan first: forgeflow design submit " + job_id,
            JobState.PLAN_APPROVED: "this plan version is already approved",
        }.get(job.state, "only a submitted plan can be approved")
        raise InvalidTransitionError(f"job {job_id} is {job.state.value}; {hint}")
    console = open_human_console()  # fail closed before showing or asking anything

    digest = job.plan.sha256
    if digest is None or store.plan_digest(job) != digest:
        raise InvalidTransitionError(
            f"the plan file changed after version {job.plan.version} was submitted; review "
            f"and resubmit it (forgeflow design submit {job_id}). Nothing was approved."
        )
    text = store.plan_snapshot_text(job_id, digest)  # show exactly what will be approved
    shown_revision = job.revision

    rule = "=" * 78
    lines = [
        rule,
        f" GATE A - PLAN APPROVAL   job {job.job_id}",
        rule,
        text.rstrip("\n"),
        rule,
        f" Job          {job.job_id}: {job.title}",
        f" Repository   {job.repo.slug or job.repo.root}",
        f" Plan         version {job.plan.version}, submitted {job.plan.submitted_at or '?'}",
        f" sha256       {digest}",
        *[f" {line}" for line in context.summary],
        "",
        " Approving authorises:",
        f"   - exactly plan version {job.plan.version} (sha256 {digest[:16]}...) shown above",
        *[f"   - {line}" for line in context.authorises],
        "",
        " It does NOT authorise:",
        *[f"   - {line}" for line in (*DEFAULT_NOT_AUTHORISED, *context.does_not_authorise)],
        rule,
    ]
    console.write("\n".join(lines) + "\n")
    code = one_time_code()
    try:
        typed = console.read_line(f"Type {code} to approve this plan (Enter cancels): ")
    except (EOFError, KeyboardInterrupt) as exc:
        raise ApprovalDeclinedError("approval cancelled; nothing was recorded") from exc
    if typed.strip().upper() != code:
        raise ApprovalDeclinedError("the code did not match; nothing was approved")

    approval = Approval(
        gate="plan",
        approver=_approver(),
        approved_at=utc_now(),
        subject_sha256=digest,
        method=HUMAN_APPROVAL_METHOD,
        note=f"confirmed with a one-time code at a {console.kind}",
        subject_version=job.plan.version,
    )
    try:
        # The store re-hashes the plan file and snapshot under its lock before committing.
        return store._record_plan_approval(job_id, approval, expected_revision=shown_revision)
    except ConcurrencyError as exc:
        raise InvalidTransitionError(
            "the job changed while the plan was being reviewed (for example it was revised or "
            "resubmitted); nothing was approved, run approve again"
        ) from exc


def _approver() -> str:
    try:
        return getpass.getuser() or "local-user"
    except Exception:  # getuser raises OSError (or others) when no name can be found
        return "local-user"
