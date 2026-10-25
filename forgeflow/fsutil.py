"""Filesystem primitives: atomic writes, append-only logs, OS file locks and path containment.

Design notes (Windows first):

* Atomic replace writes to a temporary file in the destination directory, fsyncs it and calls
  ``os.replace``. On Windows ``os.replace`` fails with a sharing violation while another process
  (a reader, the search indexer, an antivirus scanner) holds the target open, so it is retried
  with backoff instead of falling back to a non-atomic write.
* Locks use OS byte-range locks (``msvcrt.locking`` / ``fcntl.flock``). The OS drops them when the
  holder exits or crashes, so there is no stale lock file to clean up and no PID guessing for
  short critical sections. Lock files are never deleted, which avoids unlink/re-create races.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

from forgeflow.errors import LockTimeoutError, ValidationError

_REPLACE_ATTEMPTS = 12


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so readers see either the old or the new content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        _replace_with_retry(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    _fsync_directory(path.parent)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _replace_with_retry(src: str, dst: Path) -> None:
    delay = 0.01
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return  # Windows cannot open a directory handle for fsync; NTFS journals the rename.
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON record as a line, durably.

    If a previous writer crashed mid-line, a newline is written first so the torn line stays
    isolated and every later record remains parseable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
    with open(path, "a+b") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() > 0:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) != b"\n":
                fh.seek(0, os.SEEK_END)
                fh.write(b"\n")
        fh.seek(0, os.SEEK_END)
        fh.write(line.encode("utf-8"))
        fh.flush()
        os.fsync(fh.fileno())


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Return ``(records, corrupt_line_count)``. A missing file is an empty log."""
    records: list[dict[str, Any]] = []
    corrupt = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    corrupt += 1
                    continue
                if isinstance(value, dict):
                    records.append(value)
                else:
                    corrupt += 1
    except FileNotFoundError:
        pass
    return records, corrupt


def write_self_ignoring_gitignore(directory: Path) -> None:
    """Make ``directory`` invisible to git without editing the host repository's .gitignore."""
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / ".gitignore"
    content = "# Created by ForgeFlow: local runtime data, never commit.\n*\n"
    if not marker.exists() or marker.read_text(encoding="utf-8") != content:
        atomic_write_text(marker, content)


def normalized(path: Path | str) -> str:
    """A comparison key for paths: absolute, symlinks/junctions resolved, case-folded on Windows."""
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def ensure_within(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate``; raise if it escapes ``base`` via ``..``, a symlink or a junction."""
    base_key = normalized(base)
    cand_key = normalized(candidate)
    try:
        common = os.path.commonpath([base_key, cand_key])
    except ValueError as exc:  # different drives on Windows
        raise ValidationError(f"path {candidate} is outside {base}") from exc
    if common != base_key:
        raise ValidationError(f"path {candidate} is outside {base}")
    return Path(os.path.realpath(candidate))


class FileLock:
    """Exclusive inter-process lock on a lock file.

    Not re-entrant: acquiring a lock this instance already holds raises instead of deadlocking.
    The same lock file opened by two instances (even in one process) excludes each other.
    """

    def __init__(self, path: Path, timeout: float = 10.0, poll_interval: float = 0.05) -> None:
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fd: int | None = None
        self._guard = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self) -> bool:
        """Acquire without waiting. Returns False if another holder has it."""
        with self._guard:
            if self._fd is not None:
                raise RuntimeError(f"lock {self.path} is already held by this instance")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _os_lock(fd)
            except OSError:
                os.close(fd)
                return False
            self._fd = fd
            return True

    def acquire(self) -> None:
        deadline = time.monotonic() + self.timeout
        while not self.try_acquire():
            if time.monotonic() >= deadline:
                raise LockTimeoutError(f"timed out after {self.timeout}s waiting for {self.path}")
            time.sleep(self.poll_interval)

    def release(self) -> None:
        with self._guard:
            if self._fd is None:
                return
            fd, self._fd = self._fd, None
            try:
                _os_unlock(fd)
            finally:
                os.close(fd)

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


if sys.platform == "win32":
    import msvcrt

    def _os_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _os_unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _os_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _os_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(path: Path, timeout: float = 10.0) -> Iterator[FileLock]:
    lock = FileLock(path, timeout=timeout)
    with lock:
        yield lock
