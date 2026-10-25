"""Discovery of Product Management / Engineering skills per host (fixtures, no real CLI)."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from unittest import mock

from forgeflow import hostskills
from forgeflow.proc import DEFAULT_TIMEOUT, CommandResult
from tests.support import rmtree


def _skill(root: Path, name: str) -> None:
    (root / "skills" / name).mkdir(parents=True)
    (root / "skills" / name / "SKILL.md").write_text("---\nname: x\n---\n", encoding="utf-8")


class HostSkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="forgeflow-skills-"))
        self.addCleanup(rmtree, self.tmp)

    def test_claude_code_lists_enabled_plugin_skills(self) -> None:
        pm = self.tmp / "pm"
        _skill(pm, "product-brainstorming")
        _skill(pm, "write-spec")
        eng = self.tmp / "eng"
        _skill(eng, "system-design")
        listed = [
            {"id": "product-management@synced", "enabled": True, "installPath": str(pm)},
            {"id": "engineering@synced", "enabled": False, "installPath": str(eng)},
            {"id": "superpowers@official", "enabled": True, "installPath": str(self.tmp)},
        ]

        def runner(
            argv: Sequence[str],
            *,
            cwd: Path | None = None,
            timeout: float = DEFAULT_TIMEOUT,
            input_text: str | None = None,
            env: Mapping[str, str] | None = None,
        ) -> CommandResult:
            return CommandResult(tuple(argv), 0, json.dumps(listed), "")

        with mock.patch.object(hostskills, "which", return_value="claude"):
            found = hostskills.claude_code_skills(runner)
        self.assertEqual(found.enabled(), ["product-management"])
        self.assertEqual(found.skills_for("idea"), ["product-management:product-brainstorming"])
        # Disabled engineering skills are not offered, even though they are on disk.
        self.assertEqual(found.skills_for("design"), ["product-management:write-spec"])
        self.assertNotIn("superpowers", found.plugins)

    def test_claude_code_absent(self) -> None:
        with mock.patch.object(hostskills, "which", return_value=None):
            found = hostskills.claude_code_skills()
        self.assertEqual(found.status, "unavailable")
        self.assertTrue(found.to_dict()["fallback"])

    def test_codex_reads_config_and_plugin_cache(self) -> None:
        home = self.tmp / "codex"
        (home).mkdir()
        (home / "config.toml").write_text(
            '[plugins."engineering@claude-plugins-official"]\nenabled = true\n'
            '[plugins."superpowers@claude-plugins-official"]\nenabled = true\n',
            encoding="utf-8",
        )
        _skill(
            home / "plugins" / "cache" / "claude-plugins-official" / "engineering" / "1.0",
            "architecture",
        )
        found = hostskills.codex_skills(home, user_home=self.tmp)
        self.assertEqual(found.enabled(), ["engineering"])
        self.assertEqual(found.skills_for("design"), ["engineering:architecture"])
        self.assertEqual(found.skills_for("idea"), [])

    def test_codex_user_skill_folders_are_recognised_by_provenance(self) -> None:
        home = self.tmp / "codex"
        home.mkdir()
        (home / "config.toml").write_text('model = "x"\n', encoding="utf-8")
        user = self.tmp / "user"
        skills = user / ".agents" / "skills"

        def adapted(folder: str, package: str, source: str) -> None:
            (skills / folder).mkdir(parents=True)
            (skills / folder / "SKILL.md").write_text(
                f"---\nname: {folder}\ndescription: x\nmetadata:\n"
                f"  source-package: {package}\n  source-version: 1.2.0\n"
                f"  source-skill: {source}\n---\n# body\n",
                encoding="utf-8",
            )

        adapted("pm-brainstorm", "Product Management-1.2.0-v40", "product-brainstorming")
        adapted("eng-system-design", "Engineering-1.2.0-v41", "system-design")
        adapted("my-notes", "Someone Else-0.1", "notes")  # not a Knowledge Work plugin
        (skills / "plain").mkdir()
        (skills / "plain" / "SKILL.md").write_text("---\nname: plain\n---\n", encoding="utf-8")

        found = hostskills.codex_skills(home, user_home=user)
        self.assertEqual(sorted(found.enabled()), ["engineering", "product-management"])
        self.assertEqual(found.skills_for("idea"), ["$pm-brainstorm"])
        self.assertEqual(found.canonical_for("idea"), ["product-management:product-brainstorming"])
        self.assertEqual(found.skills_for("design"), ["$eng-system-design"])
        self.assertFalse(found.to_dict()["fallback"])
        self.assertIn(
            "$pm-brainstorm (product-management:product-brainstorming)", hostskills.render(found)
        )

        # A repository-level copy takes precedence over the user-level one.
        repo = self.tmp / "repo"
        (repo / ".agents" / "skills" / "team-brainstorm").mkdir(parents=True)
        (repo / ".agents" / "skills" / "team-brainstorm" / "SKILL.md").write_text(
            "---\nname: team-brainstorm\nmetadata:\n  source-package: Product Management-1.3.0\n"
            "  source-skill: product-brainstorming\n---\n",
            encoding="utf-8",
        )
        found = hostskills.codex_skills(home, user_home=user, repo_root=repo)
        self.assertEqual(found.skills_for("idea"), ["$team-brainstorm"])

    def test_package_names_map_to_plugins(self) -> None:
        for package, plugin in (
            ("Product Management-1.2.0-v40", "product-management"),
            ("Engineering-1.2.0-v41", "engineering"),
            ("Productivity-1.3.1-v41", "productivity"),
            ("Engineering", "engineering"),
        ):
            with self.subTest(package):
                self.assertEqual(hostskills.plugin_of_package(package), plugin)

    def test_codex_without_plugins_falls_back(self) -> None:
        home = self.tmp / "codex"
        home.mkdir()
        (home / "config.toml").write_text('model = "x"\n', encoding="utf-8")
        found = hostskills.codex_skills(home, user_home=self.tmp)
        self.assertEqual(found.plugins, {})
        self.assertIn("built-in guidance", hostskills.render(found))
        self.assertEqual(
            hostskills.codex_skills(self.tmp / "missing", user_home=self.tmp).status, "unavailable"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
