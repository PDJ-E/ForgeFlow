from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest import mock

from forgeflow.errors import ProviderError, ValidationError
from forgeflow.jobs import Policy
from forgeflow.proc import DEFAULT_TIMEOUT, CommandResult, pid_alive
from forgeflow.providers import (
    NAMED_POLICIES,
    ClaudeCodeProvider,
    CodexProvider,
    ProviderCapabilities,
    ProviderResult,
    ProviderTask,
    ProviderUnavailableError,
    UsageReport,
    interpret_review,
    validate_policy,
)
from tests.support import FAKE_CLI, rmtree


def _wait_dead(pid: int, seconds: float = 10) -> bool:
    deadline = time.monotonic() + seconds
    while pid_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


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
        plan_sha256=hashlib.sha256(b"# Plan\n").hexdigest(),
        plan_text="# Plan\n",
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
            "plan text not approved": dict(plan_text="# Another plan\n"),
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


class StubLease:
    """Stands in for a held LeaseHandle; records the worker PIDs the provider reports."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.active = True
        self.pids: list[int | None] = []

    def set_worker_pid(self, pid: int | None) -> None:
        self.pids.append(pid)


class ExecutionTests(unittest.TestCase):
    """The real provider classes driving tests/helpers/fake_cli.py (a real child process).

    MOCKED: the CLI's output (captured formats of Claude Code 2.1.311 and Codex 0.164.0).
    REAL: argv construction, stdin, subprocess and PID tracking, timeouts, parsing.
    """

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="forgeflow-prov-"))
        self.addCleanup(rmtree, self.dir)
        self.lease = StubLease("ff-20261024-abcdef")

    def cli(self, name: str, step: dict[str, Any]) -> list[str]:
        script = self.dir / f"{name}-{len(list(self.dir.iterdir()))}.json"
        script.write_text(json.dumps(step), encoding="utf-8")
        return [sys.executable, str(FAKE_CLI), name, str(script)]

    def log(self, command: list[str]) -> list[dict[str, Any]]:
        text = Path(command[3]).with_suffix(".log").read_text(encoding="utf-8")
        return [json.loads(line) for line in text.splitlines()]

    def task(self, **overrides: Any) -> ProviderTask:
        work = self.dir / "work"
        work.mkdir(exist_ok=True)
        return _task(worktree_path=str(work), **overrides)

    def execute(self, provider: Any, task: ProviderTask) -> ProviderResult:
        result: ProviderResult = provider.execute(task, cast(Any, self.lease))
        return result

    def test_claude_builder_invocation_and_result(self) -> None:
        command = self.cli("claude", {"writes": {"src/a.py": "x\n"}, "result": "made a.py"})
        result = self.execute(ClaudeCodeProvider(command=command), self.task())
        self.assertEqual(
            (result.status, result.summary, result.mocked), ("succeeded", "made a.py", True)
        )
        self.assertEqual(result.usage.tokens, "measured")
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (153, 20))
        self.assertEqual(result.usage.cost, "api-equivalent-estimate")
        [start, _end] = self.log(command)
        argv = start["argv"]
        for flag in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Write,Glob,Grep")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertNotIn("Bash", " ".join(argv))
        self.assertIn("# Plan", start["prompt"], "the approved plan travels on stdin")
        self.assertEqual(self.lease.pids, [start["pid"], None])

    def test_claude_reviewer_is_read_only_and_findings_decide_the_verdict(self) -> None:
        review = {
            "verdict": "approve",
            "summary": "ok",
            "findings": [
                {"severity": "blocking", "file": "a.py", "issue": "crash", "suggestion": ""}
            ],
        }
        command = self.cli("claude", {"review": review})
        task = self.task(role="reviewer", allowed_operations=("read",), review_commit="a" * 40)
        result = self.execute(ClaudeCodeProvider(command=command), task)
        self.assertEqual(result.verdict, "request-changes")
        self.assertEqual(result.findings, ("[blocking] a.py: crash",))
        argv = self.log(command)[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep")
        self.assertIn("--json-schema", argv)

    def test_claude_errors_are_failures(self) -> None:
        for step in ({"is_error": True, "result": "Credit balance too low"}, {"exit": 1}):
            with self.subTest(step):
                result = self.execute(
                    ClaudeCodeProvider(command=self.cli("claude", step)), self.task()
                )
                self.assertEqual(result.status, "failed")

    def test_codex_builder_and_reviewer_invocations(self) -> None:
        command = self.cli("codex", {"result": "built"})
        result = self.execute(CodexProvider(command=command), self.task())
        self.assertEqual((result.status, result.summary), ("succeeded", "built"))
        argv = self.log(command)[0]["argv"]
        self.assertEqual(argv[argv.index("-s") + 1], "workspace-write")
        self.assertEqual(argv[argv.index("-C") + 1], str(self.dir / "work"))
        self.assertIn('approval_policy="never"', argv)
        self.assertEqual(argv[-1], "-")

        review = {
            "verdict": "request-changes",
            "summary": "nit",
            "findings": [
                {"severity": "minor", "file": "a.py", "issue": "naming", "suggestion": "rename"}
            ],
        }
        command = self.cli("codex", {"review": review})
        task = self.task(role="reviewer", allowed_operations=("read",), review_commit="a" * 40)
        result = self.execute(CodexProvider(command=command), task)
        self.assertEqual(result.verdict, "approve", "minor findings do not request changes")
        argv = self.log(command)[0]["argv"]
        self.assertEqual(argv[argv.index("-s") + 1], "read-only")
        self.assertIn("--output-schema", argv)
        self.assertIn("--skip-git-repo-check", argv)

    def test_codex_sandbox_refusal_is_reported_as_blocked(self) -> None:
        command = self.cli("codex", {"sandbox_refusal": True})
        result = self.execute(CodexProvider(command=command), self.task())
        self.assertEqual(result.status, "blocked")
        self.assertTrue(any("refusing to run unsandboxed" in n for n in result.notes))

    def test_a_sandbox_refusal_blocks_a_reviewer_too(self) -> None:
        command = self.cli("codex", {"sandbox_refusal": True, "review": {"verdict": "approve",
                                     "summary": "fine", "findings": []}})  # fmt: skip
        task = self.task(role="reviewer", allowed_operations=("read",), review_commit="a" * 40)
        result = self.execute(CodexProvider(command=command), task)
        self.assertEqual((result.status, result.verdict), ("blocked", None))

    def test_review_access_must_be_proven(self) -> None:
        from forgeflow.dispatch import verify_review_access

        def review(token: str, complete: bool) -> ProviderResult:
            answer = {"verdict": "approve", "summary": "ok", "findings": [],
                      "snapshot_token": token, "inspection_complete": complete}  # fmt: skip
            return ProviderResult("codex", "reviewer", "succeeded", "ok", (), UsageReport(),
                                  False, verdict="approve", review=answer)  # fmt: skip

        self.assertEqual(verify_review_access(review("t0k", True), "t0k").verdict, "approve")
        for bad in (review("", True), review("other", True), review("t0k", False)):
            with self.subTest(bad.review):
                checked = verify_review_access(bad, "t0k")
                self.assertEqual((checked.status, checked.verdict), ("blocked", None))
                self.assertIn("review incomplete", checked.summary)

    def test_a_provider_that_overruns_is_killed(self) -> None:
        command = self.cli("claude", {"sleep": 30})
        started = time.monotonic()
        result = self.execute(ClaudeCodeProvider(command=command), self.task(timeout_seconds=1))
        self.assertEqual(result.status, "timed_out")
        self.assertLess(time.monotonic() - started, 20)
        pid = self.log(command)[0]["pid"]
        self.assertTrue(_wait_dead(pid), "the provider process tree must be gone")
        self.assertEqual(self.lease.pids[-1], None)

    def test_malformed_reviews_are_errors_not_approvals(self) -> None:
        answers: list[Any] = [
            None,
            [],
            {"verdict": "lgtm", "findings": []},
            {"verdict": "approve"},
            {"verdict": "approve", "summary": "", "findings": []},
        ]  # no token
        for answer in answers:
            with self.subTest(answer), self.assertRaises(ProviderError):
                interpret_review("x", answer)


class PreflightTests(unittest.TestCase):
    def detected(self, provider: Any, **caps: Any) -> Any:
        found = ProviderCapabilities(provider.name, "/bin/x", version="1.0", **caps)
        return mock.patch.object(type(provider), "detect", return_value=found)

    def test_missing_options_and_login_are_blockers(self) -> None:
        claude = ClaudeCodeProvider()
        with self.detected(claude, authenticated=False, features={"--print": True}):
            with self.assertRaises(ProviderUnavailableError) as ctx:
                claude.preflight("reviewer")
        message = str(ctx.exception)
        self.assertIn("not logged in", message)
        self.assertIn("--json-schema", message)

    def test_a_ready_provider_passes(self) -> None:
        codex = CodexProvider()
        features = dict.fromkeys(CodexProvider.feature_flags, True)
        with self.detected(codex, authenticated=True, features=features):
            codex.preflight("reviewer")
            codex.preflight("builder", Path.home() / "repo" / ".forge" / "worktrees" / "x")

    @unittest.skipUnless(sys.platform == "win32", "Codex's TEMP refusal is a Windows sandbox trait")
    def test_codex_refuses_a_worktree_under_temp_on_windows(self) -> None:
        codex = CodexProvider()
        features = dict.fromkeys(CodexProvider.feature_flags, True)
        with self.detected(codex, authenticated=True, features=features):
            with self.assertRaisesRegex(ProviderUnavailableError, "TEMP"):
                codex.preflight("builder", Path(tempfile.gettempdir()) / "repo")

    def test_missing_executable(self) -> None:
        with mock.patch("forgeflow.providers.which", return_value=None):
            with self.assertRaisesRegex(ProviderUnavailableError, "not installed"):
                CodexProvider().preflight("builder")


if __name__ == "__main__":
    unittest.main()
