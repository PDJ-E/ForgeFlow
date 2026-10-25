"""Subprocess execution and process liveness, with Windows-specific care.

* Commands run without a shell, with prompts disabled, and with a timeout. On timeout the whole
  process tree is killed: npm-installed CLIs on Windows are ``.cmd`` shims whose real work
  happens in a grandchild that ``Popen.kill`` alone would orphan.
* ``pid_alive`` never uses ``os.kill(pid, 0)`` on Windows, where signal 0 is CTRL_C_EVENT.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
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
        return f"`{' '.join(self.argv)}` {status}" + (f": {detail[-1]}" if detail else "")


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


def run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    input_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run ``argv`` and capture text output. Raises ToolNotFoundError if it is not on PATH."""
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
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
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
    try:
        stdout, stderr = proc.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc.pid)
        stdout, stderr = proc.communicate()
        return CommandResult(tuple(argv), None, stdout or "", stderr or "", timed_out=True)
    return CommandResult(tuple(argv), proc.returncode, stdout or "", stderr or "")


def kill_process_tree(pid: int) -> None:
    """Forcefully terminate ``pid`` and its descendants. Best effort; never raises."""
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def hostname() -> str:
    return socket.gethostname()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _ERROR_ACCESS_DENIED = 5

    def pid_alive(pid: int) -> bool:
        """True if a process with ``pid`` exists. Unknown answers are reported as alive."""
        if pid <= 0:
            return False
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
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
