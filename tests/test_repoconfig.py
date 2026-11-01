"""Repository-configured checks: parsing, and that a builder cannot change what judges it.

REAL: git repositories and ``git show``. Nothing is mocked.
"""

from __future__ import annotations

import sys
import unittest

from forgeflow.errors import ValidationError
from forgeflow.repoconfig import (
    DEFAULT_MAX_BUDGET_USD,
    DEFAULT_MAX_TURNS,
    DEFAULT_PROTECTED_PATHS,
    DEFAULT_TASK_TIMEOUT,
    MAX_BUDGET_USD_LIMIT,
    MAX_TASK_TIMEOUT,
    MAX_TURNS_LIMIT,
    load,
    parse,
)
from tests.support import GitRepoTestCase, run_git

GOOD = """
[checks]
timeout_seconds = 30
[[checks.commands]]
name = "tests"
argv = ["{python}", "-m", "unittest"]
[[checks.commands]]
argv = ["git", "diff", "--check"]
[build]
allowed_paths = ["src/*", "tests/*"]
"""


class ParseTests(unittest.TestCase):
    def test_a_valid_configuration(self) -> None:
        config = parse(GOOD, python="PY")
        self.assertEqual(config.argv_list, (("PY", "-m", "unittest"), ("git", "diff", "--check")))
        self.assertEqual(config.names, ("tests", "git diff --check"))
        self.assertEqual(config.check_timeout, 30)
        self.assertEqual(config.allowed_paths, ("src/*", "tests/*"))
        self.assertEqual(config.protected_paths, DEFAULT_PROTECTED_PATHS)

    def test_budgets_default_and_parse(self) -> None:
        config = parse(GOOD)
        self.assertEqual(
            (config.max_turns, config.max_budget_usd, config.task_timeout),
            (DEFAULT_MAX_TURNS, DEFAULT_MAX_BUDGET_USD, DEFAULT_TASK_TIMEOUT),
        )
        limits = (
            f"max_turns = {MAX_TURNS_LIMIT}\nmax_budget_usd = {MAX_BUDGET_USD_LIMIT}\n"
            f"task_timeout_seconds = {MAX_TASK_TIMEOUT}\n"
        )
        config = parse(GOOD + limits)
        self.assertEqual(
            (config.max_turns, config.max_budget_usd, config.task_timeout),
            (MAX_TURNS_LIMIT, MAX_BUDGET_USD_LIMIT, MAX_TASK_TIMEOUT),
        )
        self.assertEqual(parse(GOOD + "max_budget_usd = 2\n").max_budget_usd, 2.0)

    def test_budget_rejections_at_each_boundary(self) -> None:
        bad = {
            "max_turns": ["0", str(MAX_TURNS_LIMIT + 1), "true", "1.5", "'10'"],
            "max_budget_usd": ["0", "-1", str(MAX_BUDGET_USD_LIMIT + 0.01), "true", "'5'"],
            "task_timeout_seconds": ["0", str(MAX_TASK_TIMEOUT + 1), "true", "60.5"],
        }
        for key, values in bad.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                    parse(GOOD + f"{key} = {value}\n")

    def test_python_placeholder_defaults_to_this_interpreter(self) -> None:
        self.assertEqual(parse(GOOD).checks[0].argv[0], sys.executable)

    def test_rejections(self) -> None:
        cases = {
            "not toml": "[checks",
            "no checks": "[build]\n",
            "no commands": "[checks]\n",
            "empty argv": "[checks]\n[[checks.commands]]\nargv = []\n",
            "argv not strings": "[checks]\n[[checks.commands]]\nargv = [1]\n",
            "bad timeout": "[checks]\ntimeout_seconds = 0\n[[checks.commands]]\nargv = ['x']\n",
            "escaping path": "[checks]\n[[checks.commands]]\nargv = ['x']\n[build]\n"
            "allowed_paths = ['../*']\n",
            "no allowed paths": "[checks]\n[[checks.commands]]\nargv = ['x']\n[build]\n"
            "allowed_paths = []\n",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                parse(text)


class LoadTests(GitRepoTestCase):
    def commit_config(self, text: str) -> str:
        (self.repo / ".forge").mkdir(exist_ok=True)
        (self.repo / ".forge" / "forgeflow.toml").write_text(text, encoding="utf-8")
        run_git(self.repo, "add", "-A")
        run_git(self.repo, "commit", "-q", "-m", "config")
        return run_git(self.repo, "rev-parse", "HEAD")

    def test_read_from_the_base_commit_not_the_working_tree(self) -> None:
        base = self.commit_config(GOOD)
        (self.repo / ".forge" / "forgeflow.toml").write_text(
            "[checks]\n[[checks.commands]]\nargv = ['true']\n", encoding="utf-8"
        )
        config = load(self.repo, base)
        self.assertEqual(config.names[0], "tests")
        self.assertEqual(
            config.source, {"path": ".forge/forgeflow.toml", "commit": base, "origin": "base"}
        )

    def test_falls_back_to_the_main_checkout_head(self) -> None:
        base = run_git(self.repo, "rev-parse", "HEAD")
        head = self.commit_config(GOOD)
        config = load(self.repo, base)
        self.assertEqual(config.source["origin"], "main-checkout-head")
        self.assertEqual(config.source["commit"], head)

    def test_no_configuration_is_a_clear_refusal(self) -> None:
        base = run_git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(ValidationError, "does not build without deterministic"):
            load(self.repo, base)


if __name__ == "__main__":
    unittest.main()
