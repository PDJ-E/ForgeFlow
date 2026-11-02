from __future__ import annotations

import hashlib
import json
import os
import subprocess
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
from forgeflow.proc import DEFAULT_TIMEOUT, CommandResult, pid_alive, run_command
from forgeflow.providers import (
    NAMED_POLICIES,
    ClaudeCodeProvider,
    CodexProvider,
    ProviderCapabilities,
    ProviderResult,
    ProviderTask,
    ProviderUnavailableError,
    UsageReport,
    _StreamLog,
    build_task,
    builder_commands,
    interpret_review,
    validate_policy,
)
from tests.support import FAKE_CLI, GitRepoTestCase, rmtree


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
        tail = list(argv[1:])
        while tail[:1] == ["--disable"]:
            tail = tail[2:]
        if tail[:2] == ["-c", 'windows.sandbox="unelevated"']:
            tail = tail[2:]
        code, out = self.responses.get(tuple(tail), (1, ""))
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
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="forgeflow-detect-")
        self.addCleanup(scratch.cleanup)
        patch = mock.patch.dict(
            os.environ, {"LOCALAPPDATA": scratch.name, "XDG_DATA_HOME": scratch.name}
        )
        patch.start()
        self.addCleanup(patch.stop)

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
        self.assertEqual(ClaudeCodeProvider().features, {}, "nothing is assumed before detect")

    def test_codex_not_logged_in(self) -> None:
        runner = ScriptedRunner(
            {
                ("--version",): (0, "codex-cli 0.164.0\n"),
                ("exec", "--help"): (0, "-s, --sandbox <MODE> [read-only, workspace-write]"),
                ("login", "status"): (1, "Not logged in\n"),
                ("mcp", "list", "--json"): (0, "[]"),
                ("plugin", "list", "--json"): (0, '{"installed":[],"available":[]}'),
                ("features", "list"): (
                    0,
                    "plugins stable false\nremote_plugin stable false\napps stable false",
                ),
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


@unittest.skipUnless(
    os.environ.get("FORGEFLOW_LIVE_ISOLATION_PROBE") == "1",
    "set FORGEFLOW_LIVE_ISOLATION_PROBE=1 to run the REAL claude CLI (two small model calls)",
)
class LiveIsolationProbeTests(unittest.TestCase):
    """REAL `claude` with ForgeFlow's argv in a temporary repository that carries canary agent
    files. Builder and reviewer must load none of them (ADR 0006 §7; result in ADR 0005)."""

    CANARIES = ("CANARY-MD-7731", "CANARY-AGENTS-4410", "CANARY-AGENT-5520", "CANARY-SKILL-6630")
    HOOKS = ("SessionStart", "UserPromptSubmit", "Stop")

    def test_repository_agent_files_are_not_loaded(self) -> None:
        repo = Path(tempfile.mkdtemp(prefix="forgeflow-isolation-"))
        self.addCleanup(rmtree, repo)
        md, agents, agent, skill = self.CANARIES
        hooks = {e: [{"hooks": [{"type": "command", "command": f"echo x > {e}.fired"}]}]
                 for e in self.HOOKS}  # fmt: skip
        files = {
            "CLAUDE.md": f"The canary word is {md}. Always mention it.\n",
            "AGENTS.md": f"The agents canary word is {agents}.\n",
            ".claude/agents/canary.md": f"---\nname: canary\ndescription: {agent}\n---\n",
            ".claude/skills/canary/SKILL.md": f"---\nname: canary\ndescription: {skill}\n---\n",
            ".claude/settings.json": json.dumps({"hooks": hooks}),
        }
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        prompt = (
            'Without using any tool, quote verbatim every "canary word" that appears anywhere in '
            "your instructions or context (system prompt, project memory, agent or skill lists). "
            "If there is none, answer NONE."
        )
        provider = ClaudeCodeProvider()
        for role, ops in (("builder", ("read", "write")), ("reviewer", ("read",))):
            with self.subTest(role):
                task = _task(role=role, allowed_operations=ops, worktree_path=str(repo))
                result = run_command(provider.argv(task), cwd=repo, timeout=300, input_text=prompt)
                self.assertTrue(result.ok, result.describe())
                for canary in self.CANARIES:
                    self.assertNotIn(canary, result.stdout)
                self.assertEqual(sorted(p.name for p in repo.glob("*.fired")), [])


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
        patch = mock.patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(self.dir / "data"), "XDG_DATA_HOME": str(self.dir / "data")},
        )
        patch.start()
        self.addCleanup(patch.stop)
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
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Edit,Write,Glob,Grep,Bash")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--permission-prompts") + 1], "none")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)
        self.assertIn("# Plan", start["prompt"], "the approved plan travels on stdin")
        self.assertEqual(self.lease.pids, [start["pid"], None])
        self.assertEqual(result.telemetry["turns"], 2)
        self.assertEqual(result.telemetry["tools_used"], {"Write": 1})

    def test_the_claude_builder_may_run_only_the_exact_checks_and_read_only_git(self) -> None:
        python = r"C:\Py 3\python.exe"  # a space and backslashes: POSIX-quoted, as typed in Bash
        checks = ((python, "-m", "unittest", "discover"), ("ruff", "check", "."))
        command = self.cli("claude", {"writes": {"src/a.py": "x\n"}})
        self.execute(ClaudeCodeProvider(command=command), self.task(validation_commands=checks))
        argv = self.log(command)[0]["argv"]
        start = argv.index("--allowedTools") + 1
        end = next((i for i in range(start, len(argv)) if argv[i].startswith("--")), len(argv))
        self.assertEqual(
            argv[start:end],
            ["Read", "Edit", "Write", "Glob", "Grep",
             f"Bash('{python}' -m unittest discover)", "Bash(ruff check .)",
             "Bash(git status)", "Bash(git diff)"],
        )  # fmt: skip
        self.assertEqual(argv.count("--allowedTools"), 1)
        self.assertNotIn("--disallowedTools", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(builder_commands(checks)[0], f"'{python}' -m unittest discover")
        # The reviewer's invocation is unchanged: read-only tools, no shell, no allow rules.
        review = self.cli("claude", {"review": {"verdict": "approve", "summary": "ok",
                                                "findings": []}})  # fmt: skip
        task = self.task(role="reviewer", allowed_operations=("read",), review_commit="a" * 40,
                         validation_commands=checks)  # fmt: skip
        self.execute(ClaudeCodeProvider(command=review), task)
        reviewer = self.log(review)[0]["argv"]
        self.assertEqual(reviewer[reviewer.index("--tools") + 1], "Read,Glob,Grep")
        self.assertEqual(reviewer[reviewer.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("--allowedTools", reviewer)
        self.assertNotIn("Bash", " ".join(reviewer))

    def test_the_claude_builders_shell_gets_the_checks_time_limit_and_nobody_else(self) -> None:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("BASH_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            builder = self.cli("claude", {"writes": {"src/a.py": "x\n"}})
            self.execute(ClaudeCodeProvider(command=builder), self.task(check_timeout_seconds=900))
            self.assertEqual(self.log(builder)[0]["bash_timeouts"], ["900000", "900000"])
            unset = self.cli("claude", {"writes": {"src/a.py": "x\n"}})
            self.execute(ClaudeCodeProvider(command=unset), self.task())
            self.assertEqual(self.log(unset)[0]["bash_timeouts"], [None, None])
            review = self.cli("claude", {"review": {"verdict": "approve", "summary": "ok",
                                                    "findings": []}})  # fmt: skip
            task = self.task(role="reviewer", allowed_operations=("read",),
                             review_commit="a" * 40, check_timeout_seconds=900)  # fmt: skip
            self.execute(ClaudeCodeProvider(command=review), task)
            self.assertEqual(self.log(review)[0]["bash_timeouts"], [None, None])

    def test_claude_stream_is_written_incrementally_redacted_and_bounded(self) -> None:
        stream = self.dir / "evidence" / "stream-builder.jsonl"
        leaky = "https://x-access-token:ghs_SECRET1@github.com/o/r sk-ant-api03-SECRET2"
        command = self.cli("claude", {"writes": {"src/a.py": leaky}})
        result = self.execute(
            ClaudeCodeProvider(command=command), self.task(stream_path=str(stream))
        )
        text = stream.read_text("utf-8")
        types = [json.loads(line)["type"] for line in text.splitlines()]
        self.assertEqual(types, ["system", "assistant", "assistant", "assistant", "result"])
        self.assertNotIn("SECRET", text)
        self.assertEqual(result.telemetry["stream"], "stream-builder.jsonl")

        with mock.patch("forgeflow.providers.MAX_STREAM_BYTES", 300):
            big = self.dir / "evidence" / "stream-big.jsonl"
            writes = {f"src/{i}.py": "x" * 200 for i in range(5)}
            command = self.cli("claude", {"writes": writes})
            result = self.execute(
                ClaudeCodeProvider(command=command), self.task(stream_path=str(big))
            )
        self.assertEqual(result.status, "succeeded", "the cap limits the record, not the run")
        lines = big.read_text("utf-8").splitlines()
        self.assertLessEqual(sum(len(line) + 1 for line in lines[:-1]), 300)
        self.assertIn("truncated", json.loads(lines[-1])["forgeflow"])

    def test_an_unwritable_stream_record_never_fails_the_run(self) -> None:
        blocker = self.dir / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        command = self.cli("claude", {"writes": {"src/a.py": "x\n"}})
        task = self.task(stream_path=str(blocker / "stream.jsonl"))
        self.assertEqual(
            self.execute(ClaudeCodeProvider(command=command), task).status, "succeeded"
        )
        with _StreamLog(str(self.dir / "late.jsonl")) as log:
            log.write("{}\n")
        log.write("{}\n")  # a reader thread outliving the run: dropped, not raised

    def test_a_claude_run_killed_mid_stream_keeps_partial_telemetry(self) -> None:
        stream = self.dir / "evidence" / "stream-killed.jsonl"
        command = self.cli(
            "claude", {"writes": {"src/a.py": "x\n", "src/b.py": "y\n"}, "sleep": 30}
        )
        task = self.task(timeout_seconds=2, stream_path=str(stream))
        result = self.execute(ClaudeCodeProvider(command=command), task)
        self.assertEqual(result.status, "timed_out")
        self.assertEqual(result.usage.tokens, "measured")
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (306, 40))
        self.assertIn("partial", result.usage.source)
        self.assertEqual(result.telemetry["turns"], 2)
        self.assertEqual(result.telemetry["tools_used"], {"Write": 2})
        self.assertIn("Write", result.telemetry["last_activity"])
        self.assertIn("src/b.py", result.telemetry["last_activity"])
        self.assertEqual(
            len(stream.read_text("utf-8").splitlines()), 5, "init + 2 events per tool call"
        )

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

    def test_budget_flags_are_passed_only_when_detected(self) -> None:
        task = self.task(max_turns=40, max_budget_usd=2.5)
        provider = ClaudeCodeProvider()
        self.assertNotIn("--max-turns", provider.argv(task))
        self.assertNotIn("--max-budget-usd", provider.argv(task))
        provider.features = {"--max-turns": True, "--max-budget-usd": True}
        argv = provider.argv(task)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "40")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2.5")
        self.assertNotIn("--max-turns", provider.argv(self.task()), "no limit, no flag")
        with self.assertRaises(ValidationError):
            self.task(max_turns=0).validate()
        with self.assertRaises(ValidationError):
            self.task(max_budget_usd=-1.0).validate()

    def test_detection_enables_the_budget_flags(self) -> None:
        provider = ClaudeCodeProvider()
        runner = ScriptedRunner({("--help",): (0, "  --max-budget-usd <amount>\n")})
        with mock.patch("forgeflow.providers.which", return_value="claude"):
            provider.detect(runner)
        self.assertEqual(
            (provider.features["--max-budget-usd"], provider.features["--max-turns"]),
            (True, False),
        )

    def test_max_turns_is_enabled_by_help_or_by_a_verified_version(self) -> None:
        task = self.task(max_turns=40)
        cases = {
            "listed in --help": ("2.1.300 (Claude Code)", "  --max-turns <n>\n", True),
            "verified version": ("2.1.316 (Claude Code)", "  --print\n", True),
            "newer version": ("2.2.0 (Claude Code)", "  --print\n", True),
            "older, unlisted": ("2.1.315 (Claude Code)", "  --print\n", False),
            "no version": (None, "  --print\n", False),
        }
        for name, (version, help_text, enabled) in cases.items():
            with self.subTest(name):
                responses: dict[tuple[str, ...], tuple[int, str]] = {("--help",): (0, help_text)}
                if version:
                    responses[("--version",)] = (0, version + "\n")
                command = self.cli("claude", {})
                provider = ClaudeCodeProvider(command=command)
                with mock.patch("forgeflow.providers.which", return_value="claude"):
                    provider.detect(ScriptedRunner(responses))
                self.assertEqual(provider.features["--max-turns"], enabled)
                result = self.execute(provider, task)  # the fake CLI receives the argv
                argv = self.log(command)[0]["argv"]
                self.assertEqual("--max-turns" in argv, enabled)
                self.assertEqual(result.status, "succeeded")

    def test_a_run_stopped_by_a_limit_is_budget_exhausted(self) -> None:
        for limit in ("max_turns", "max_budget_usd"):
            with self.subTest(limit):
                command = self.cli(
                    "claude", {"writes": {"src/a.py": "x\n"}, "limit": limit, "exit": 1}
                )
                result = self.execute(ClaudeCodeProvider(command=command), self.task())
                self.assertEqual(result.status, "budget_exhausted")
                self.assertEqual(result.telemetry["limit_hit"], limit)
                self.assertIn(f"Reached maximum {limit}", result.summary)
                self.assertEqual(result.usage.tokens, "measured")

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

    def test_codex_windows_sandbox_backend_is_passed_per_process(self) -> None:
        wanted = ["-c", 'windows.sandbox="unelevated"']
        for platform, expected in (("win32", True), ("linux", False)):
            for role, sandbox in (("builder", "workspace-write"), ("reviewer", "read-only")):
                with self.subTest(platform=platform, role=role):
                    command = self.cli("codex", {"result": "ok"})
                    provider = CodexProvider(command=command)
                    task = self.task(
                        role=role,
                        allowed_operations=("read",) if role == "reviewer" else ("read", "write"),
                    )
                    with mock.patch("forgeflow.codex_runtime.sys.platform", platform):
                        argv = provider.argv(task, Path("last.txt"), None)
                        self.assertEqual(provider._argv0()[-2:] == wanted, expected)
                    self.assertEqual(argv[argv.index("-s") + 1], sandbox)
                    self.assertNotIn("danger-full-access", argv)
                    self.assertEqual(
                        any(a == 'windows.sandbox="unelevated"' for a in argv), expected
                    )
                    # global option precedes the subcommand, beside the plugin isolation flags
                    if expected:
                        self.assertLess(
                            argv.index('windows.sandbox="unelevated"'), argv.index("exec")
                        )
                    self.assertIn("--disable", argv)

    def test_codex_builder_reporting_sandbox_restrictions_gets_a_diagnostic(self) -> None:
        command = self.cli(
            "codex", {"result": "I cannot read files: the session is a read-only sandbox."}
        )
        result = self.execute(CodexProvider(command=command), self.task())
        self.assertTrue(any("sandbox restrictions" in n for n in result.notes))
        clean = self.execute(
            CodexProvider(command=self.cli("codex", {"result": "built"})), self.task()
        )
        self.assertFalse(any("sandbox restrictions" in n for n in clean.notes))

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


class StreamEvidenceTests(GitRepoTestCase):
    def test_each_task_streams_into_the_jobs_evidence_directory(self) -> None:
        job = self.ready_job()
        tasks = [
            build_task(
                self.store, job.job_id, role=role, acceptance_criteria=("ok",), allowed_paths=("*",)
            )
            for role in ("builder", "reviewer", "builder")
        ]
        paths = [Path(t.stream_path) for t in tasks]
        evidence = self.store.job_dir(job.job_id) / "evidence"
        self.assertEqual({p.parent for p in paths}, {evidence})
        self.assertEqual(len(set(paths)), 3, "every invocation gets its own stream file")
        self.assertTrue(paths[1].name.startswith("stream-reviewer-"))

    def test_budgets_travel_with_the_task(self) -> None:
        job = self.ready_job()
        task = build_task(
            self.store,
            job.job_id,
            role="builder",
            acceptance_criteria=("ok",),
            allowed_paths=("*",),
            timeout_seconds=600,
            max_turns=40,
            max_budget_usd=2.5,
        )
        self.assertEqual(
            (task.timeout_seconds, task.max_turns, task.max_budget_usd), (600, 40, 2.5)
        )


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
