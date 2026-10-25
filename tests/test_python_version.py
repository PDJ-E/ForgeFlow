"""The supported Python floor is declared once and enforced consistently."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
import unittest
from unittest import mock

import forgeflow
from forgeflow.environment import ERROR, OK, check_python
from tests.support import PROJECT_ROOT


class PythonFloorTests(unittest.TestCase):
    def test_configuration_agrees_with_the_runtime_floor(self) -> None:
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text("utf-8"))
        major, minor = forgeflow.MIN_PYTHON
        self.assertEqual((major, minor), (3, 13))
        self.assertEqual(config["project"]["requires-python"], f">={major}.{minor}")
        self.assertEqual(config["tool"]["ruff"]["target-version"], f"py{major}{minor}")
        self.assertEqual(config["tool"]["mypy"]["python_version"], f"{major}.{minor}")
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
        tested = re.search(r"python: \[([^\]]*)\]", workflow)
        assert tested is not None
        self.assertIn(f'"{major}.{minor}"', tested.group(1))

    def test_doctor_reports_an_old_interpreter_as_a_failure(self) -> None:
        with mock.patch.object(sys, "version_info", (3, 12, 9, "final", 0)):
            check = check_python()
        self.assertEqual((check.status, check.required), (ERROR, True))
        self.assertIn("py -3.13 -m forgeflow", check.summary)
        self.assertEqual(check_python().status, OK)

    def test_an_old_interpreter_gets_a_clear_message(self) -> None:
        # Real check on machines that also have an older Python (the pilot machine has 3.10).
        launcher = shutil.which("py")
        if launcher is None:
            self.skipTest("the Windows py launcher is not installed")
        probe = subprocess.run(
            [launcher, "-3.10", "-c", "pass"], capture_output=True, text=True, check=False
        )
        if probe.returncode != 0:
            self.skipTest("no Python 3.10 installed")
        result = subprocess.run(
            [launcher, "-3.10", "-m", "forgeflow", "--version"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ForgeFlow requires Python 3.13 or newer", result.stderr)
        self.assertNotIn("SyntaxError", result.stderr)


if __name__ == "__main__":
    unittest.main()
