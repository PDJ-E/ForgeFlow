from __future__ import annotations

import gc
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

from forgeflow.errors import ToolNotFoundError
from forgeflow.proc import pid_alive, redact_credentials, run_command, run_tracked
from tests.support import rmtree

_SPAWN_GRANDCHILD = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(60)
"""


class RunCommandTests(unittest.TestCase):
    def test_captures_output_and_exit_code(self) -> None:
        result = run_command([sys.executable, "-c", "import sys; print('hi'); sys.exit(3)"])
        self.assertEqual(result.stdout.strip(), "hi")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(result.ok)

    def test_disables_interactive_prompts(self) -> None:
        result = run_command(
            [sys.executable, "-c", "import os; print(os.environ['GIT_TERMINAL_PROMPT'])"]
        )
        self.assertEqual(result.stdout.strip(), "0")

    def test_missing_executable_raises(self) -> None:
        with self.assertRaises(ToolNotFoundError):
            run_command(["forgeflow-definitely-not-installed-xyz"])

    def test_timeout_kills_the_whole_process_tree(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="forgeflow-proc-"))
        self.addCleanup(rmtree, tmp)
        pid_file = tmp / "grandchild.pid"
        started = time.monotonic()
        result = run_command([sys.executable, "-c", _SPAWN_GRANDCHILD, str(pid_file)], timeout=3)
        self.assertTrue(result.timed_out)
        self.assertLess(time.monotonic() - started, 30)
        grandchild = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while pid_alive(grandchild):
            self.assertLess(time.monotonic(), deadline, "grandchild survived the timeout")
            time.sleep(0.1)


class StdoutLineTests(unittest.TestCase):
    def test_lines_reach_the_callback_as_they_arrive_and_survive_a_kill(self) -> None:
        lines: list[str] = []
        script = "import time; print('one', flush=True); print('two', flush=True); time.sleep(60)"
        result = run_tracked([sys.executable, "-c", script], timeout=3, on_stdout_line=lines.append)
        self.assertTrue(result.timed_out)
        self.assertEqual(lines, ["one\n", "two\n"])
        self.assertEqual(result.stdout, "one\ntwo\n")

    def test_large_input_and_output_do_not_deadlock(self) -> None:
        lines: list[str] = []
        script = (
            "import sys; data = sys.stdin.read(); sys.stderr.write('e' * 100000)\n"
            "for i in range(20000): print(i)\n"
            "print(len(data)); sys.exit(4)"
        )
        result = run_tracked(
            [sys.executable, "-c", script],
            timeout=60,
            input_text="x" * 1_000_000,
            on_stdout_line=lines.append,
        )
        self.assertEqual((result.returncode, result.timed_out), (4, False))
        self.assertEqual(len(lines), 20001)
        self.assertEqual(lines[-1], "1000000\n")
        self.assertEqual(result.stdout, "".join(lines))
        self.assertEqual(len(result.stderr), 100000)

    def test_pipes_are_closed_after_normal_exit_and_after_a_kill(self) -> None:
        for script, timeout in (("print('x')", 30.0), ("import time; time.sleep(60)", 2.0)):
            with self.subTest(script), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                run_tracked([sys.executable, "-c", script], timeout=timeout, on_stdout_line=print)
                gc.collect()
            self.assertEqual([str(w.message) for w in caught if w.category is ResourceWarning], [])

    def test_a_failing_callback_does_not_stop_the_drain(self) -> None:
        def explode(_line: str) -> None:
            raise ValueError("sink closed")

        script = "for i in range(50000): print(i)"
        result = run_tracked([sys.executable, "-c", script], timeout=30, on_stdout_line=explode)
        self.assertEqual((result.returncode, len(result.stdout.splitlines())), (0, 50000))


class RedactionTests(unittest.TestCase):
    def test_tokens_and_url_credentials_are_removed(self) -> None:
        text = (
            "push https://x-access-token:ghs_abc123@github.com/o/r "
            "key sk-ant-api03-AbC_d-9 pat github_pat_11ABC_xyz token ghp_A1b2C3"
        )
        redacted = redact_credentials(text)
        for secret in ("ghs_abc123", "sk-ant-api03-AbC_d-9", "github_pat_11ABC_xyz", "ghp_A1b2C3"):
            self.assertNotIn(secret, redacted)
        self.assertIn("https://github.com/o/r", redacted)


class PidAliveTests(unittest.TestCase):
    def test_current_process_is_alive(self) -> None:
        import os

        self.assertTrue(pid_alive(os.getpid()))

    def test_exited_process_is_not_alive(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait(timeout=10)
        self.assertFalse(pid_alive(proc.pid))

    def test_invalid_pid(self) -> None:
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-5))


if __name__ == "__main__":
    unittest.main()
