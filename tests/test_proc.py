from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from forgeflow.errors import ToolNotFoundError
from forgeflow.proc import pid_alive, run_command
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
