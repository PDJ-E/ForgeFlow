from __future__ import annotations

import json
import os
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from forgeflow.errors import ValidationError
from forgeflow.jobs import Policy
from forgeflow.proc import DEFAULT_TIMEOUT, CommandResult
from forgeflow.providers import (
    NAMED_POLICIES,
    ClaudeCodeProvider,
    CodexProvider,
    ProviderTask,
    validate_policy,
)


class ScriptedRunner:
    """Returns canned output keyed by the argv tail (everything after the executable)."""

    def __init__(self, responses: Mapping[tuple[str, ...], tuple[int, str]]) -> None:
        self.responses = responses

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        code, out = self.responses.get(tuple(argv[1:]), (1, ""))
        return CommandResult(tuple(argv), code, out, "")


def _task(**overrides: object) -> ProviderTask:
    base = ProviderTask(
        job_id="ff-20261024-abcdef",
        role="builder",
        repo_slug="o/n",
        worktree_path="/w",
        branch="forge/ff-20261024-abcdef",
        base_sha="0" * 40,
        plan_path="docs/plan.md",
        plan_sha256="1" * 64,
        acceptance_criteria=("works",),
        allowed_paths=("src/*",),
        allowed_operations=("read", "write"),
        validation_commands=(("python", "-V"),),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


class DetectionTests(unittest.TestCase):
    def test_claude_detection_reads_version_auth_and_flags(self) -> None:
        runner = ScriptedRunner(
            {
                ("--version",): (0, "2.1.310 (Claude Code)\n"),
                ("--help",): (0, "  -p, --print\n  --output-format <f>\n  --permission-mode\n"),
                ("auth", "status"): (0, json.dumps({"loggedIn": True, "authMethod": "claude.ai"})),
            }
        )
        with mock.patch("forgeflow.providers.which", return_value="C:/bin/claude.exe"):
            caps = ClaudeCodeProvider().detect(runner)
        self.assertTrue(caps.available)
        self.assertEqual(caps.version, "2.1.310 (Claude Code)")
        self.assertTrue(caps.features["--print"])
        self.assertFalse(caps.features["--json-schema"])

    def test_codex_not_logged_in(self) -> None:
        runner = ScriptedRunner(
            {
                ("--version",): (0, "codex-cli 0.164.0\n"),
                ("exec", "--help"): (0, "-s, --sandbox <MODE> [read-only, workspace-write]"),
                ("login", "status"): (1, "Not logged in\n"),
            }
        )
        with mock.patch("forgeflow.providers.which", return_value="C:/npm/codex.cmd"):
            caps = CodexProvider().detect(runner)
        self.assertTrue(caps.installed)
        self.assertFalse(caps.available)
        self.assertIs(caps.authenticated, False)

    def test_unparseable_auth_is_unknown_not_true(self) -> None:
        runner = ScriptedRunner({("auth", "status"): (0, "something new")})
        with mock.patch("forgeflow.providers.which", return_value="claude"):
            caps = ClaudeCodeProvider().detect(runner)
        self.assertIsNone(caps.authenticated)
        self.assertFalse(caps.available)

    def test_missing_executable(self) -> None:
        with mock.patch("forgeflow.providers.which", return_value=None):
            caps = CodexProvider().detect(ScriptedRunner({}))
        self.assertFalse(caps.installed)
        self.assertFalse(caps.available)


class TaskContractTests(unittest.TestCase):
    def test_valid_task(self) -> None:
        _task().validate()

    def test_rejections(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "reviewer writes": dict(role="reviewer"),
            "unknown op": dict(allowed_operations=("read", "deploy")),
            "no criteria": dict(acceptance_criteria=()),
            "absolute path": dict(allowed_paths=("/etc/*",)),
            "traversal": dict(allowed_paths=("../other/*",)),
            "windows drive": dict(allowed_paths=("C:/x/*",)),
            "backslash": dict(allowed_paths=("src\\*",)),
            "builder without paths": dict(allowed_paths=()),
            "zero timeout": dict(timeout_seconds=0),
        }
        for name, overrides in cases.items():
            with self.subTest(name), self.assertRaises(ValidationError):
                _task(**overrides).validate()

    def test_path_permission_globs(self) -> None:
        task = _task(allowed_paths=("src/*", "README.md"))
        self.assertTrue(task.permits_path("src/a/b.py"))
        self.assertTrue(task.permits_path("src\\a.py"))
        self.assertTrue(task.permits_path("README.md"))
        self.assertFalse(task.permits_path(".github/workflows/ci.yml"))


class PolicyTests(unittest.TestCase):
    def test_four_named_policies(self) -> None:
        self.assertEqual(
            {(p.builder, p.reviewer) for p in NAMED_POLICIES.values()},
            {("claude", "claude"), ("codex", "codex"), ("claude", "codex"), ("codex", "claude")},
        )
        for policy in NAMED_POLICIES.values():
            validate_policy(policy)

    def test_unknown_provider(self) -> None:
        with self.assertRaises(ValidationError):
            validate_policy(Policy("gemini", None))


@unittest.skipUnless(
    os.environ.get("FORGEFLOW_LIVE_PROVIDERS") == "1",
    "set FORGEFLOW_LIVE_PROVIDERS=1 to detect the real installed CLIs",
)
class LiveDetectionTests(unittest.TestCase):
    """REAL detection of installed CLIs. Runs --version/--help/auth status only; no model call."""

    def test_detect_installed_providers(self) -> None:
        for provider in (ClaudeCodeProvider(), CodexProvider()):
            with self.subTest(provider.name):
                caps = provider.detect()
                if not caps.installed:
                    self.skipTest(f"{provider.name} not installed")
                self.assertIsNotNone(caps.version)
                self.assertTrue(any(caps.features.values()), caps.features)


if __name__ == "__main__":
    unittest.main()
