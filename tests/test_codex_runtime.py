from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from forgeflow import codex_runtime
from forgeflow.proc import CommandResult
from forgeflow.providers import CodexProvider, ProviderUnavailableError
from tests.test_providers import StubLease, _task


class CodexIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="forgeflow-isolation-")
        self.addCleanup(self.tmp.cleanup)
        # Windows CI's TEMP uses an 8.3 alias (RUNNER~1); runtime guards resolve it.
        self.root = Path(self.tmp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        patch = mock.patch.dict(
            os.environ,
            {
                "LOCALAPPDATA": str(self.root / "data"),
                "XDG_DATA_HOME": str(self.root / "data"),
                "CODEX_HOME": str(self.root / "personal"),
                "CODEX_CONFIG": "personal-settings",
                "CODEX_AUTH_TOKEN": "personal-token",
                "OPENAI_API_KEY": "personal-key",
            },
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.auth = (0, "Logged in using ChatGPT")
        self.mcp = "[]"
        self.plugins = '{"installed": [], "available": []}'

    @property
    def profile(self) -> Path:
        return self.root / "data" / "forgeflow" / "codex"

    def runner(self, argv: Any, **kwargs: Any) -> CommandResult:
        args = list(argv)
        self.calls.append((args, kwargs))
        output, code = "", 0
        if args[1:] == ["--version"]:
            output = "codex-cli 0.164.0"
        elif args[1:] == ["exec", "--help"]:
            output = " ".join(CodexProvider.feature_flags)
        elif args[1:] == ["login", "status"]:
            code, output = self.auth
        elif args[1:] == ["mcp", "list", "--json"]:
            output = self.mcp
        elif args[1:] == ["plugin", "list", "--json"]:
            output = self.plugins
        elif "exec" in args:
            kwargs["on_start"](123)
            message = "built"
            if "--output-schema" in args:
                message = json.dumps(
                    {
                        "verdict": "approve",
                        "summary": "reviewed",
                        "findings": [],
                        "snapshot_token": "token",
                        "inspection_complete": True,
                    }
                )
            Path(args[args.index("-o") + 1]).write_text(message, encoding="utf-8")
        else:
            self.fail(f"unexpected subprocess {args}")
        return CommandResult(tuple(args), code, output, "")

    def test_detection_uses_isolated_profile_without_personal_config_or_auth(self) -> None:
        personal = self.root / "personal"
        personal.mkdir()
        (personal / "config.toml").write_text('[mcp_servers.personal]\ncommand="secret"\n')
        (personal / "auth.json").write_text("personal-credentials")
        with mock.patch("forgeflow.providers.which", return_value="codex"):
            caps = CodexProvider().detect(self.runner)
        self.assertTrue(caps.available, caps.to_dict())
        self.assertGreaterEqual(len(self.calls), 5)
        for _, kwargs in self.calls:
            self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.profile))
            self.assertEqual(kwargs["cwd"], self.profile)
            for key in ("CODEX_CONFIG", "CODEX_AUTH_TOKEN", "OPENAI_API_KEY"):
                self.assertEqual(kwargs["env"][key], "")
        self.assertFalse((self.profile / "auth.json").exists())
        self.assertNotIn("personal", (self.profile / "config.toml").read_text())
        self.assertEqual(os.environ["CODEX_HOME"], str(personal))
        self.assertEqual((personal / "auth.json").read_text(), "personal-credentials")

    def test_missing_auth_has_scoped_interactive_login_instructions_and_no_secrets(self) -> None:
        self.auth = (1, "Not logged in personal-key")
        with mock.patch("forgeflow.providers.which", return_value="codex"):
            caps = CodexProvider().detect(self.runner)
        self.assertFalse(caps.available)
        self.assertIs(caps.authenticated, False)
        self.assertIn("codex login", caps.auth_detail)
        self.assertIn("CODEX_HOME", caps.auth_detail)
        self.assertIn(str(self.profile), caps.auth_detail)
        self.assertNotIn("personal-key", caps.auth_detail)
        self.assertFalse(any(args[1:] == ["login"] for args, _ in self.calls))

    def test_nonempty_or_unknown_inventories_fail_closed(self) -> None:
        for field, value in (
            ("mcp", '[{"name":"personal"}]'),
            ("mcp", "bad"),
            ("plugins", '{"installed":[{"id":"p"}],"available":[]}'),
            ("plugins", "[]"),
            ("plugins", '{"installed":[]}'),
            ("plugins", '{"available":[]}'),
            ("plugins", '{"installed":[],"available":[],"enabled":["new-source"]}'),
        ):
            with self.subTest(field=field, value=value):
                self.mcp, self.plugins = "[]", '{"installed":[],"available":[]}'
                setattr(self, field, value)
                with mock.patch("forgeflow.providers.which", return_value="codex"):
                    caps = CodexProvider().detect(self.runner)
                self.assertFalse(caps.available)
                self.assertIn("isolation", str(caps.to_dict()).lower())

    def test_builder_and_reviewer_verify_and_run_with_same_environment(self) -> None:
        for role in ("builder", "reviewer"):
            with self.subTest(role=role):
                self.calls.clear()
                provider = CodexProvider(runner=self.runner)
                lease = StubLease("ff-20261024-abcdef")
                task = _task(
                    worktree_path=str(self.work),
                    role=role,
                    allowed_operations=("read", "write") if role == "builder" else ("read",),
                )
                result = provider.execute(task, cast(Any, lease))
                self.assertEqual(result.status, "succeeded")
                self.assertTrue(any("isolation verified" in note for note in result.notes))
                if role == "reviewer":
                    self.assertEqual(result.verdict, "approve")
                for _, kwargs in self.calls:
                    self.assertEqual(kwargs["cwd"], self.work)
                    self.assertEqual(kwargs["env"]["CODEX_HOME"], str(self.profile))
                args, kwargs = self.calls[-1]
                self.assertEqual(
                    args[args.index("-s") + 1],
                    "workspace-write" if role == "builder" else "read-only",
                )
                self.assertEqual(kwargs["timeout"], task.timeout_seconds)
                self.assertEqual(lease.pids, [123, None])

    def test_repository_and_ancestor_config_block_execution_before_any_subprocess(self) -> None:
        for relative in (
            ".codex/config.toml",
            ".codex/hooks.json",
            ".agents/plugins/marketplace.json",
            ".claude-plugin/marketplace.json",
        ):
            with self.subTest(relative=relative):
                config = self.root / relative
                config.parent.mkdir(parents=True, exist_ok=True)
                config.write_text("repository configuration")
                self.calls.clear()
                try:
                    with self.assertRaisesRegex(ProviderUnavailableError, "isolation"):
                        CodexProvider(runner=self.runner).execute(
                            _task(worktree_path=str(self.work)),
                            cast(Any, StubLease("ff-20261024-abcdef")),
                        )
                    self.assertEqual(self.calls, [])
                finally:
                    config.unlink()

    def test_execution_without_login_never_invokes_model(self) -> None:
        self.auth = (1, "Not logged in")
        with self.assertRaisesRegex(ProviderUnavailableError, "codex login"):
            CodexProvider(runner=self.runner).execute(
                _task(worktree_path=str(self.work)), cast(Any, StubLease("ff-20261024-abcdef"))
            )
        self.assertFalse(any("exec" in args for args, _ in self.calls))

    def test_platform_paths_and_fallbacks(self) -> None:
        for platform, variable, fallback in (
            ("win32", "LOCALAPPDATA", Path("AppData/Local/forgeflow/codex")),
            ("linux", "XDG_DATA_HOME", Path(".local/share/forgeflow/codex")),
        ):
            with (
                self.subTest(platform=platform),
                mock.patch("forgeflow.codex_runtime.sys.platform", platform),
            ):
                with mock.patch.dict(os.environ, {variable: str(self.root / "chosen")}):
                    self.assertEqual(
                        codex_runtime.profile_path(), self.root / "chosen/forgeflow/codex"
                    )
                with (
                    mock.patch.dict(os.environ, {variable: ""}),
                    mock.patch.object(Path, "home", return_value=self.root),
                ):
                    self.assertEqual(codex_runtime.profile_path(), self.root / fallback)
                with mock.patch.dict(os.environ, {variable: "relative"}):
                    with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "absolute"):
                        codex_runtime.profile_path()

    def test_profile_tampering_and_system_sources_fail_closed_without_rewriting(self) -> None:
        runtime = codex_runtime.CodexRuntime.prepare(self.work)
        config = runtime.home / "config.toml"
        original = config.read_text()
        for text in (
            '[mcp_servers.personal]\ncommand="bad"\n',
            "[plugins.personal]\nenabled=true\n",
        ):
            config.write_text(text)
            with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "minimal"):
                codex_runtime.CodexRuntime.prepare(self.work)
            self.assertEqual(config.read_text(), text)
        config.write_text(original)
        system = self.root / "system-config"
        system.mkdir()
        with mock.patch.object(codex_runtime, "system_config_paths", return_value=(system,)):
            with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "system config"):
                codex_runtime.CodexRuntime.prepare(self.work)

    def test_unwritable_profile_blocks_detection_without_subprocess_or_fallback(self) -> None:
        with (
            mock.patch("forgeflow.providers.which", return_value="codex"),
            mock.patch.object(Path, "mkdir", side_effect=PermissionError("denied")),
        ):
            caps = CodexProvider().detect(self.runner)
        self.assertFalse(caps.available)
        self.assertIn("no personal-profile fallback", caps.isolation_error)
        self.assertEqual(self.calls, [])

    def test_inventory_failure_timeout_or_unknown_auth_blocks_execution(self) -> None:
        for failed in ("mcp", "plugin", "login"):
            for timed_out in (False, True):
                with self.subTest(failed=failed, timed_out=timed_out):
                    self.calls.clear()

                    def runner(
                        argv: Any, failed: str = failed, timed_out: bool = timed_out, **kwargs: Any
                    ) -> CommandResult:
                        if argv[1] == failed:
                            return CommandResult(
                                tuple(argv), 1, "secret-key", "secret-token", timed_out
                            )
                        return self.runner(argv, **kwargs)

                    with self.assertRaises(ProviderUnavailableError) as error:
                        CodexProvider(runner=runner).execute(
                            _task(worktree_path=str(self.work)),
                            cast(Any, StubLease("ff-20261024-abcdef")),
                        )
                    self.assertNotIn("secret", str(error.exception))
                    self.assertFalse(any("exec" in args for args, _ in self.calls))

    def test_preflight_checks_actual_workdir_and_doctor_reports_isolation(self) -> None:
        from forgeflow.environment import check_providers

        config = self.work / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('[mcp_servers.repo]\ncommand="bad"\n')
        with mock.patch(
            "forgeflow.providers.which",
            side_effect=lambda name: "codex" if name == "codex" else None,
        ):
            with self.assertRaisesRegex(ProviderUnavailableError, "isolation"):
                CodexProvider().preflight("reviewer", self.work)
            checks = check_providers(self.runner, self.work)
        codex = next(check for check in checks if check.name == "provider:codex")
        self.assertEqual(codex.status, "warn")
        self.assertIn(str(config), codex.summary)

    def test_profile_extensions_and_repo_location_are_rejected(self) -> None:
        runtime = codex_runtime.CodexRuntime.prepare(self.work)
        plugins = runtime.home / "plugins"
        plugins.mkdir()
        (plugins / "installed.json").write_text("{}")
        with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "extensions"):
            codex_runtime.CodexRuntime.prepare(self.work)
        (plugins / "installed.json").unlink()
        (self.root / ".git").mkdir()
        with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "outside any repository"):
            codex_runtime.CodexRuntime.prepare(self.work)

    def test_hardlinked_authentication_is_rejected_without_reading_credentials(self) -> None:
        runtime = codex_runtime.CodexRuntime.prepare(self.work)
        personal_auth = self.root / "personal-auth.json"
        personal_auth.write_text("do not read me")
        (runtime.home / "auth.json").hardlink_to(personal_auth)
        with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "linked authentication"):
            codex_runtime.CodexRuntime.prepare(self.work)
        self.assertEqual(personal_auth.read_text(), "do not read me")

    @unittest.skipIf(sys.platform == "win32", "Windows symlinks need developer mode or privileges")
    def test_linked_profile_parent_config_and_authentication_are_rejected(self) -> None:
        runtime = codex_runtime.CodexRuntime.prepare(self.work)
        config = runtime.home / "config.toml"
        original = config.read_text()
        target = self.root / "external-config"
        target.write_text(original)
        config.unlink()
        config.symlink_to(target)
        with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "linked profile config"):
            codex_runtime.CodexRuntime.prepare(self.work)
        config.unlink()
        config.write_text(original)
        auth = runtime.home / "auth.json"
        auth.symlink_to(self.root / "missing-auth")
        with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "linked authentication"):
            codex_runtime.CodexRuntime.prepare(self.work)
        auth.unlink()
        link = self.root / "linked-data"
        link.symlink_to(self.root / "data", target_is_directory=True)
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(link)}):
            with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "linked profile path"):
                codex_runtime.CodexRuntime.prepare(self.work)

    def test_unknown_successful_auth_and_timed_out_successful_inventory_are_blocked(self) -> None:
        self.auth = (0, "unknown response")
        with mock.patch("forgeflow.providers.which", return_value="codex"):
            caps = CodexProvider().detect(self.runner)
        self.assertFalse(caps.available)
        self.auth = (0, "Logged in using ChatGPT")

        def runner(argv: Any, **kwargs: Any) -> CommandResult:
            if argv[1] == "mcp":
                return CommandResult(tuple(argv), 0, "[]", "", timed_out=True)
            return self.runner(argv, **kwargs)

        with self.assertRaisesRegex(ProviderUnavailableError, "MCP"):
            CodexProvider(runner=runner).execute(
                _task(worktree_path=str(self.work)), cast(Any, StubLease("ff-20261024-abcdef"))
            )

    def test_inaccessible_configuration_source_cannot_be_treated_as_absent(self) -> None:
        source = self.work / ".codex" / "config.toml"
        original_lstat = Path.lstat

        def lstat(path: Path) -> os.stat_result:
            if path == source:
                raise PermissionError("cannot inspect config")
            return original_lstat(path)

        # Python 3.14+ predicates suppress all OSError; emulate that behavior here.
        with (
            mock.patch.object(Path, "lstat", lstat),
            mock.patch.object(Path, "exists", return_value=False),
            mock.patch.object(Path, "is_symlink", return_value=False),
        ):
            with self.assertRaisesRegex(codex_runtime.CodexRuntimeError, "permissions"):
                codex_runtime.CodexRuntime.prepare(self.work)

    def test_runner_passes_isolated_environment_to_popen_and_keeps_parent_unchanged(self) -> None:
        from forgeflow.proc import run_tracked

        runtime = codex_runtime.CodexRuntime.prepare(self.work)
        child = mock.Mock()
        child.communicate.return_value = ("", "")
        child.returncode = 0
        with (
            mock.patch("forgeflow.proc.shutil.which", return_value="codex"),
            mock.patch("forgeflow.proc.subprocess.Popen", return_value=child) as popen,
            mock.patch("forgeflow.proc._ProcessTree"),
        ):
            run_tracked(["codex", "exec"], cwd=self.work, env=runtime.env)
        effective = popen.call_args.kwargs["env"]
        self.assertEqual(effective["CODEX_HOME"], str(self.profile))
        self.assertEqual(effective["CODEX_AUTH_TOKEN"], "")
        self.assertEqual(effective["OPENAI_API_KEY"], "")
        self.assertEqual(effective["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(os.environ["CODEX_AUTH_TOKEN"], "personal-token")
        self.assertEqual(os.environ["OPENAI_API_KEY"], "personal-key")
