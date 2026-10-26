"""Subprocess execution and process liveness, with Windows-specific care.

* Commands run without a shell, with prompts disabled, and with a timeout.
* Every command runs in a container that ends with it. On Windows that is a Job Object created
  with ``KILL_ON_JOB_CLOSE`` (the child starts suspended and is assigned before it can spawn
  anything), so descendants die even after their parent has exited: npm-installed CLIs are
  ``.cmd`` shims whose real work happens in a grandchild. On POSIX it is the child's own
  process group (``start_new_session``).
* After a kill, output is drained for at most ``KILL_GRACE`` seconds, so a descendant that
  escaped the container and still holds the pipes cannot block the caller indefinitely.
* ``pid_alive`` never uses ``os.kill(pid, 0)`` on Windows, where signal 0 is CTRL_C_EVENT.
* ``process_started_at`` pairs a PID with its start time, so a reused PID is not mistaken for
  the process that was recorded.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Protocol

from forgeflow.errors import ToolNotFoundError

# Fail fast instead of hanging on an interactive credential or pager prompt.
NONINTERACTIVE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GH_PROMPT_DISABLED": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "NO_COLOR": "1",
}

DEFAULT_TIMEOUT = 60.0
KILL_GRACE = 5.0  # seconds to collect output after the process tree was killed

# "scheme://user:secret@host" -> "scheme://host". Tokens are often sent as the user name alone.
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s?#]*@")


def redact_credentials(text: str) -> str:
    """Remove user names, passwords and tokens embedded in URLs anywhere in ``text``."""
    return _URL_USERINFO.sub(r"\1", text)


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def describe(self) -> str:
        status = "timed out" if self.timed_out else f"exit {self.returncode}"
        detail = (self.stderr or self.stdout).strip().splitlines()
        text = f"`{' '.join(self.argv)}` {status}" + (f": {detail[-1]}" if detail else "")
        return redact_credentials(text)


class Runner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


def which(name: str) -> str | None:
    return shutil.which(name)


class TrackedRunner(Protocol):
    """A runner that reports the child's PID once it is contained, before it does any work."""

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        on_start: Callable[[int], None] | None = None,
    ) -> CommandResult: ...


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run ``argv`` and capture text output. Raises ToolNotFoundError if it is not on PATH.

    Returns within roughly ``timeout + 2 * KILL_GRACE`` seconds, and no descendant of the command
    is left running afterwards (see the module notes for the POSIX limitation).
    """
    return run_tracked(argv, cwd=cwd, timeout=timeout, input_text=input_text, env=env)


def run_tracked(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
    on_start: Callable[[int], None] | None = None,
) -> CommandResult:
    """``run_command`` that calls ``on_start(pid)`` once the child is inside its container.

    Used for provider subprocesses, whose PID the job lease records so that no second builder
    can start while one still runs. If ``on_start`` raises, the child is killed and the error
    propagates. On Windows the child dies with this process (kill-on-close Job Object), so an
    orchestrator that is killed takes its provider with it.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    executable = shutil.which(argv[0])
    if executable is None:
        raise ToolNotFoundError(f"executable not found on PATH: {argv[0]}")
    full_env = dict(os.environ)
    full_env.update(NONINTERACTIVE_ENV)
    if env:
        full_env.update(env)
    popen_kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        # Suspended until it is inside the Job Object, so it cannot spawn an escapee first.
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_SUSPENDED
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
        [executable, *argv[1:]],
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=full_env,
        **popen_kwargs,  # type: ignore[call-overload]
    )
    with _ProcessTree(proc) as tree:
        if on_start is not None:
            on_start(proc.pid)  # an exception here leaves the with block, which kills the tree
        try:
            stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            tree.kill()
            stdout, stderr = _drain(proc)
            return CommandResult(tuple(argv), None, stdout, stderr, timed_out=True)
    return CommandResult(tuple(argv), proc.returncode, stdout or "", stderr or "")


def _drain(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect output after a kill, giving up after ``KILL_GRACE`` seconds."""
    try:
        stdout, stderr = proc.communicate(timeout=KILL_GRACE)
        return stdout or "", stderr or ""
    except subprocess.TimeoutExpired:
        pass
    # Something outside the container still holds the pipes. Abandon them. On Windows the
    # reader threads are daemons blocked in ReadFile, and closing their files would deadlock.
    if sys.platform != "win32":
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=KILL_GRACE)
    return "", ""


class _ProcessTree:
    """The command and all its descendants: killable as a unit, and killed on exit."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self._job: int | None = None
        if sys.platform == "win32":
            try:
                self._job = _contain_and_resume(proc.pid)
            except BaseException:
                proc.kill()  # never leave a suspended child behind
                raise

    def kill(self) -> None:
        if sys.platform == "win32":
            if self._job is not None:
                _kernel32.TerminateJobObject(self._job, 1)
                return
        kill_process_tree(self.proc.pid)

    def __enter__(self) -> _ProcessTree:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if sys.platform == "win32":
            if self._job is not None:
                _kernel32.TerminateJobObject(self._job, 1)
                _kernel32.CloseHandle(self._job)
                self._job = None
        else:
            _killpg(self.proc.pid)  # members of the command's group that outlived it
        if self.proc.poll() is None:
            self.proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self.proc.wait(timeout=KILL_GRACE)


def kill_process_tree(pid: int) -> None:
    """Forcefully terminate ``pid`` and its descendants. Best effort; never raises.

    On Windows this walks parent links with ``taskkill /T``, which misses descendants whose
    parent already exited; ``run_command`` uses a Job Object instead and calls this only as a
    fallback.
    """
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    _killpg(pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _killpg(pgid: int) -> None:
    if sys.platform == "win32":
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def hostname() -> str:
    return socket.gethostname()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, *[ctypes.POINTER(ctypes.c_uint64)] * 4)
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
    _ntdll.NtResumeProcess.restype = ctypes.c_long

    _CREATE_SUSPENDED = 0x00000004
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_SUSPEND_RESUME = 0x0800
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _BasicLimits(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    def _contain_and_resume(pid: int) -> int:
        """Put the suspended process ``pid`` in a kill-on-close Job Object, then resume it.

        Returns the job handle. The PID cannot be reused here: Popen holds a handle to the
        (suspended) process.
        """
        access = _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_SUSPEND_RESUME
        process = _kernel32.OpenProcess(access, False, pid)
        if not process:
            raise ctypes.WinError(ctypes.get_last_error())
        job = None
        try:
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ) or not _kernel32.AssignProcessToJobObject(job, process):
                raise ctypes.WinError(ctypes.get_last_error())
            status = _ntdll.NtResumeProcess(process)
            if status != 0:
                raise OSError(f"NtResumeProcess failed with NTSTATUS {status & 0xFFFFFFFF:#x}")
            return int(job)
        except BaseException:
            if job:
                _kernel32.CloseHandle(job)  # kill-on-close: takes the child with it
            raise
        finally:
            _kernel32.CloseHandle(process)

    def _open_running(pid: int) -> tuple[int | None, bool]:
        """(handle, access_denied) for a running process; (None, False) if it is gone."""
        if pid <= 0:
            return None, False
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None, ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        code = wintypes.DWORD()
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != _STILL_ACTIVE:
            _kernel32.CloseHandle(handle)
            return None, False
        return handle, False

    def pid_alive(pid: int) -> bool:
        """True if a process with ``pid`` exists. Unknown answers are reported as alive."""
        handle, denied = _open_running(pid)
        if handle is None:
            return denied
        _kernel32.CloseHandle(handle)
        return True

    def process_started_at(pid: int) -> int | None:
        """Creation time of the running process ``pid``, or None if gone or not queryable."""
        handle, _ = _open_running(pid)
        if handle is None:
            return None
        try:
            created, exited, kernel, user = (ctypes.c_uint64() for _ in range(4))
            if not _kernel32.GetProcessTimes(
                handle,
                ctypes.byref(created),
                ctypes.byref(exited),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            return int(created.value)
        finally:
            _kernel32.CloseHandle(handle)

else:

    def pid_alive(pid: int) -> bool:
        """True if a process with ``pid`` exists. Unknown answers are reported as alive."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def process_started_at(pid: int) -> int | None:
        """Start time (clock ticks after boot) of the running process ``pid``, from /proc.

        None if the process is gone, a zombie, or /proc is unavailable (e.g. macOS).
        """
        if pid <= 0:
            return None
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            return None
        fields = stat.rsplit(")", 1)[-1].split()  # the command name may contain spaces
        if len(fields) < 20 or fields[0] == "Z":
            return None
        try:
            return int(fields[19])  # field 22 of proc(5); fields[0] is field 3
        except ValueError:
            return None
