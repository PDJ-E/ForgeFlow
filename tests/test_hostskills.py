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
        found = hostskills.codex_skills(home)
        self.assertEqual(found.enabled(), ["engineering"])
        self.assertEqual(found.skills_for("design"), ["engineering:architecture"])
        self.assertEqual(found.skills_for("idea"), [])

    def test_codex_without_plugins_falls_back(self) -> None:
        home = self.tmp / "codex"
        home.mkdir()
        (home / "config.toml").write_text('model = "x"\n', encoding="utf-8")
        found = hostskills.codex_skills(home)
        self.assertEqual(found.plugins, {})
        self.assertIn("built-in guidance", hostskills.render(found))
        self.assertEqual(hostskills.codex_skills(self.tmp / "missing").status, "unavailable")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
