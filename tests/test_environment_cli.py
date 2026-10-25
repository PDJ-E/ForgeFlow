from __future__ import annotations

import contextlib
import io
import json
import re
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from forgeflow import cli
from forgeflow.environment import (
    Check,
    EnvironmentReport,
    _token_scopes,
    check_github,
    inspect_environment,
)
from forgeflow.proc import DEFAULT_TIMEOUT, CommandResult, run_command
from tests.support import PROJECT_ROOT, GitRepoTestCase


def _gh_runner(scopes: str | None) -> object:
    def runner(
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        tail = tuple(argv[1:])
        if tail == ("--version",):
            return CommandResult(tuple(argv), 0, "gh version 2.103.2\n", "")
        if tail == ("auth", "status"):
            return CommandResult(tuple(argv), 0, "Logged in\n", "")
        if tail == ("api", "--include", "user"):
            header = f"X-Oauth-Scopes: {scopes}\n" if scopes is not None else ""
            return CommandResult(tuple(argv), 0, f"HTTP/2.0 200 OK\n{header}\n{{}}", "")
        if tail[:2] == ("api", "graphql"):
            body = {
                "data": {
                    "repository": {
                        "projectsV2": {
                            "nodes": [
                                {"number": 3, "title": "ForgeFlow dev", "url": "u", "closed": False}
                            ]
                        }
                    },
                    "repositoryOwner": {"projectsV2": {"nodes": []}},
                }
            }
            return CommandResult(tuple(argv), 0, json.dumps(body), "")
        return CommandResult(tuple(argv), 1, "", "unexpected")

    return runner


class GitHubCheckTests(unittest.TestCase):
    def run_check(self, scopes: str | None) -> list[Check]:
        with mock.patch("forgeflow.environment.which", return_value="gh"):
            return check_github(_gh_runner(scopes), "o/n", network=True)  # type: ignore[arg-type]

    def test_missing_project_scope_is_reported_with_fix(self) -> None:
        project = self.run_check("gist, read:org, repo, workflow")[-1]
        self.assertEqual(project.status, "warn")
        self.assertIn("gh auth refresh -s read:project", project.summary)

    def test_projects_listed_when_scope_present(self) -> None:
        project = self.run_check("repo, read:project")[-1]
        self.assertEqual(project.status, "ok")
        self.assertIn("#3 ForgeFlow dev", project.summary)

    def test_unknown_scopes(self) -> None:
        self.assertEqual(self.run_check(None)[-1].status, "warn")

    def test_scope_header_parsing(self) -> None:
        self.assertEqual(
            _token_scopes(_gh_runner("a, b")),  # type: ignore[arg-type]
            {"a", "b"},
        )

    def test_offline_skips_network(self) -> None:
        with mock.patch("forgeflow.environment.which", return_value="gh"):
            checks = check_github(_gh_runner("repo"), "o/n", network=False)  # type: ignore[arg-type]
        self.assertEqual([c.status for c in checks], ["info"])


def _offline_runner(argv: Sequence[str], **kwargs: object) -> CommandResult:
    """Real git, but no provider/GitHub/plugin processes (keeps the test hermetic and fast)."""
    if argv[0] == "git":
        return run_command(argv, **kwargs)  # type: ignore[arg-type]
    return CommandResult(tuple(argv), 1, "", "disabled in tests")


class InspectEnvironmentTests(GitRepoTestCase):
    def test_report_in_repository(self) -> None:
        report = inspect_environment(self.repo, runner=_offline_runner, network=False)
        self.assertTrue(report.ok, report.to_dict())
        repo = report.get("repository")
        assert repo is not None
        self.assertEqual(repo.data["slug"], "example/demo")
        knowledge = report.get("knowledge")
        assert knowledge is not None
        self.assertIn("does not need Obsidian", knowledge.summary)

    def test_outside_repository_fails_required_check(self) -> None:
        outside = self.tmp / "not-a-repo"
        outside.mkdir()
        report = inspect_environment(outside, runner=_offline_runner, network=False)
        self.assertFalse(report.ok)

    def test_doctor_writes_nothing(self) -> None:
        inspect_environment(self.repo, runner=_offline_runner, network=False)
        self.assertFalse((self.repo / ".forge").exists())


class CliTests(GitRepoTestCase):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--repo", str(self.repo), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_status_empty_is_read_only(self) -> None:
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertIn("No ForgeFlow jobs", out)
        self.assertFalse((self.repo / ".forge").exists())

    def test_status_lists_and_details_jobs(self) -> None:
        job = self.ready_job()
        revision = self.store.load(job.job_id).revision
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        self.assertRegex(out, rf"{job.job_id}\s+ready\s+mock\s+#7")
        code, out, _ = self.run_cli("status", job.job_id)
        self.assertRegex(out, r"plan approval  v1 by .+ via interactive-console")
        self.assertIn("project        #1 ForgeFlow [Todo]", out)
        code, out, _ = self.run_cli("status", job.job_id, "--json")
        self.assertEqual(json.loads(out)["job"]["state"], "ready")
        self.assertEqual(self.store.load(job.job_id).revision, revision)

    def test_status_works_from_inside_a_job_worktree(self) -> None:
        job = self.ready_job()
        assert job.worktree is not None
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--repo", job.worktree.path, "status"])
        self.assertEqual(code, 0)
        self.assertIn(job.job_id, out.getvalue())

    def test_unknown_job_is_an_error(self) -> None:
        code, _, err = self.run_cli("status", "ff-20000101-000000")
        self.assertEqual(code, 1)
        self.assertIn("no such job", err)

    def test_unimplemented_operations_do_nothing(self) -> None:
        for command in cli.NOT_YET:
            with self.subTest(command):
                code, _, err = self.run_cli(command)
                self.assertEqual(code, 2)
                self.assertIn("not implemented", err)
        self.assertFalse((self.repo / ".forge").exists())

    def test_doctor_exit_code_follows_required_checks(self) -> None:
        failing = EnvironmentReport([Check("git", "missing", True, "no git")])
        with mock.patch("forgeflow.cli.inspect_environment", return_value=failing):
            code, out, _ = self.run_cli("doctor", "--offline")
        self.assertEqual(code, 1)
        self.assertIn("required checks failed", out)


class SkillAdapterTests(unittest.TestCase):
    """The /forge (Claude Code) and $forge (Codex) adapters stay thin and in sync with the CLI."""

    ADAPTERS = (
        PROJECT_ROOT / ".claude" / "skills" / "forge" / "SKILL.md",
        PROJECT_ROOT / ".agents" / "skills" / "forge" / "SKILL.md",
    )

    def test_adapters_declare_the_forge_skill(self) -> None:
        for path in self.ADAPTERS:
            with self.subTest(str(path)):
                text = path.read_text(encoding="utf-8")
                front = text.split("---")[1]
                self.assertRegex(front, r"(?m)^name:\s*forge\s*$")
                self.assertRegex(front, r"(?m)^description:\s*\S")

    def test_adapters_only_run_implemented_commands(self) -> None:
        implemented = {"doctor", "status", "init", "skills", "idea", "design", "handoff"}
        for path in self.ADAPTERS:
            with self.subTest(str(path)):
                text = path.read_text(encoding="utf-8")
                used = set(re.findall(r"python -m forgeflow (\w+)", text))
                self.assertEqual(used, implemented)
                for op in cli.NOT_YET:
                    self.assertIn(f"`{op}`", text, f"{op} must be listed as not implemented")

    def test_adapters_never_run_approve_themselves(self) -> None:
        for path in self.ADAPTERS:
            with self.subTest(str(path)):
                text = path.read_text(encoding="utf-8")
                self.assertNotRegex(text, r"(?m)^\|.*python -m forgeflow approve")
                self.assertIn("py -3.13 -m forgeflow approve", text)  # told to the human

    def test_adapters_share_the_same_operations(self) -> None:
        texts = [p.read_text(encoding="utf-8") for p in self.ADAPTERS]
        commands = [
            sorted(
                {
                    re.sub(r"--host [\w-]+", "--host <host>", c)
                    for c in re.findall(r"`python -m forgeflow ([^`]+)`", t)
                }
            )
            for t in texts
        ]
        self.assertEqual(commands[0], commands[1])


if __name__ == "__main__":
    unittest.main()
