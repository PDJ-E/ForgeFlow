from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from forgeflow import fsutil
from forgeflow.errors import LockTimeoutError, ValidationError
from tests.support import PROJECT_ROOT, rmtree

_HOLD_LOCK = """
import sys, time
from pathlib import Path
from unittest import mock
from forgeflow.fsutil import FileLock
lock = FileLock(Path(sys.argv[1]))
lock.acquire()
print("LOCKED", flush=True)
time.sleep(60)
"""


class FsutilTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="forgeflow-fs-"))
        self.addCleanup(rmtree, self.dir)

    def test_atomic_write_replaces_content_and_leaves_no_temp_files(self) -> None:
        target = self.dir / "state.json"
        fsutil.atomic_write_json(target, {"a": 1})
        fsutil.atomic_write_json(target, {"a": 2})
        self.assertEqual(fsutil.read_json(target), {"a": 2})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["state.json"])

    def test_atomic_write_survives_target_briefly_held_open(self) -> None:
        # On Windows os.replace fails while another handle has the file open; the write must
        # retry rather than fail or fall back to a non-atomic write.
        target = self.dir / "state.json"
        fsutil.atomic_write_json(target, {"v": "old"})
        reader = open(target, encoding="utf-8")  # noqa: SIM115
        timer = threading.Timer(0.2, reader.close)
        timer.start()
        try:
            fsutil.atomic_write_json(target, {"v": "new"})
        finally:
            timer.join()
            reader.close()
        self.assertEqual(fsutil.read_json(target), {"v": "new"})

    def test_failed_write_keeps_previous_content(self) -> None:
        target = self.dir / "state.json"
        fsutil.atomic_write_json(target, {"v": 1})
        with self.assertRaises(TypeError):
            fsutil.atomic_write_json(target, {"v": object()})
        self.assertEqual(fsutil.read_json(target), {"v": 1})
        self.assertEqual(len(list(self.dir.iterdir())), 1)

    def test_jsonl_isolates_a_torn_trailing_line(self) -> None:
        log = self.dir / "events.jsonl"
        fsutil.append_jsonl(log, {"n": 1})
        with open(log, "ab") as fh:
            fh.write(b'{"n": 2, "trunc')  # simulated crash mid-write
        fsutil.append_jsonl(log, {"n": 3})
        records, corrupt = fsutil.read_jsonl(log)
        self.assertEqual([r["n"] for r in records], [1, 3])
        self.assertEqual(corrupt, 1)

    def test_missing_log_is_empty(self) -> None:
        self.assertEqual(fsutil.read_jsonl(self.dir / "absent.jsonl"), ([], 0))

    def test_ensure_within_rejects_traversal(self) -> None:
        base = self.dir / "base"
        base.mkdir()
        fsutil.ensure_within(base, base / "a" / "b.md")
        with self.assertRaises(ValidationError):
            fsutil.ensure_within(base, base / ".." / "outside.md")

    def test_ensure_within_rejects_symlink_escape(self) -> None:
        base = self.dir / "base"
        outside = self.dir / "outside"
        base.mkdir()
        outside.mkdir()
        try:
            os.symlink(outside, base / "link", target_is_directory=True)
        except OSError:
            self.skipTest("creating symlinks is not permitted for this user")
        with self.assertRaises(ValidationError):
            fsutil.ensure_within(base, base / "link" / "note.md")

    def test_self_ignoring_gitignore_is_idempotent(self) -> None:
        d = self.dir / "runtime"
        fsutil.write_self_ignoring_gitignore(d)
        fsutil.write_self_ignoring_gitignore(d)
        self.assertIn("*", (d / ".gitignore").read_text(encoding="utf-8").splitlines())

    def test_reads_retry_a_transient_sharing_violation_on_windows(self) -> None:
        """Regression (CI, 2026-10-27): a reader polling job.json while another process replaced
        it got PermissionError on Windows."""
        target = self.dir / "job.json"
        fsutil.atomic_write_json(target, {"revision": 1})
        real_open = open
        failures = [PermissionError(13, "sharing violation")] * 2

        def flaky_open(*args: object, **kwargs: object) -> object:
            if failures:
                raise failures.pop()
            return real_open(*args, **kwargs)  # type: ignore[call-overload]

        with mock.patch("builtins.open", flaky_open):
            if os.name == "nt":
                self.assertEqual(fsutil.read_json(target), {"revision": 1})
            else:  # no such transient condition on POSIX: never retried
                with self.assertRaises(PermissionError):
                    fsutil.read_json(target)

    def test_reads_during_concurrent_replaces_always_see_a_whole_record(self) -> None:
        target = self.dir / "job.json"
        fsutil.atomic_write_json(target, {"revision": 0})
        stop = threading.Event()

        def writer() -> None:
            n = 0
            while not stop.is_set():
                n += 1
                fsutil.atomic_write_json(target, {"revision": n, "pad": "x" * 2000})

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                self.assertIn("revision", fsutil.read_json(target))
        finally:
            stop.set()
            thread.join(timeout=10)


class FileLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="forgeflow-lock-"))
        self.addCleanup(rmtree, self.dir)
        self.path = self.dir / "x.lock"

    def test_two_instances_exclude_each_other(self) -> None:
        a = fsutil.FileLock(self.path)
        b = fsutil.FileLock(self.path, timeout=0.2)
        a.acquire()
        try:
            self.assertFalse(b.try_acquire())
            with self.assertRaises(LockTimeoutError):
                b.acquire()
        finally:
            a.release()
        self.assertTrue(b.try_acquire())
        b.release()

    def test_not_reentrant(self) -> None:
        lock = fsutil.FileLock(self.path)
        with lock, self.assertRaises(RuntimeError):
            lock.try_acquire()

    def test_lock_is_released_when_holder_process_is_killed(self) -> None:
        env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
        child = subprocess.Popen(
            [sys.executable, "-c", _HOLD_LOCK, str(self.path)],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            assert child.stdout is not None
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            self.assertFalse(fsutil.FileLock(self.path).try_acquire())
        finally:
            child.kill()
            child.wait(timeout=10)
            if child.stdout:
                child.stdout.close()
        # The OS drops the lock with the process; no stale-lock cleanup is involved.
        deadline = time.monotonic() + 5
        lock = fsutil.FileLock(self.path)
        while not lock.try_acquire():
            self.assertLess(time.monotonic(), deadline, "lock not released after holder died")
            time.sleep(0.05)
        lock.release()


if __name__ == "__main__":
    unittest.main()
