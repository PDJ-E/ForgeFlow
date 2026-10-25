"""Which Anthropic Product Management and Engineering skills a host can use: discovered.

ForgeFlow reuses these skills during discovery and design instead of copying their content. It
does not invoke them itself: the host agent loads them through its own skill mechanism, by the
exact names reported here, and falls back to ForgeFlow's short built-in guidance when none is
installed.

* Claude Code: ``claude plugin list --json`` reports enabled plugins and their install paths;
  skill names are ``<plugin>:<skill>`` for each ``skills/<skill>/SKILL.md``.
* Codex: enabled plugins come from ``[plugins."<name>@<marketplace>"]`` in
  ``$CODEX_HOME/config.toml`` and their skills from the plugin cache.

Discovery is read-only and does not prove a running session will expose a skill; the adapters
check their own session's skill list before relying on one.
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.errors import ToolNotFoundError
from forgeflow.proc import Runner, run_command, which

CORE_PLUGINS = ("product-management", "engineering")
KNOWLEDGE_WORK_PLUGINS = ("engineering", "product-management", "data", "productivity")
HOSTS = ("claude-code", "codex")

# Which skill to reach for at each stage, if the host has it. Names are matched against what
# discovery found; nothing is invoked that was not discovered.
STAGE_HINTS = {
    "idea": ("product-management:product-brainstorming",),
    "design": (
        "product-management:write-spec",
        "engineering:system-design",
        "engineering:architecture",
        "engineering:testing-strategy",
    ),
}


@dataclass
class PluginSkills:
    plugin: str
    enabled: bool
    version: str | None = None
    skills: list[str] = field(default_factory=list)


@dataclass
class HostSkills:
    host: str
    status: str  # "ok" | "unavailable" | "error"
    detail: str
    plugins: dict[str, PluginSkills] = field(default_factory=dict)

    def enabled(self) -> list[str]:
        return [name for name, p in self.plugins.items() if p.enabled]

    def skills_for(self, stage: str) -> list[str]:
        available = {s for p in self.plugins.values() if p.enabled for s in p.skills}
        return [s for s in STAGE_HINTS.get(stage, ()) if s in available]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stages"] = {stage: self.skills_for(stage) for stage in STAGE_HINTS}
        data["fallback"] = not any(self.skills_for(stage) for stage in STAGE_HINTS)
        return data


def _skills_in(plugin: str, install: Path) -> list[str]:
    found = sorted({p.parent.name for p in install.glob("**/skills/*/SKILL.md")})
    return [f"{plugin}:{s}" for s in found]


def claude_code_skills(runner: Runner = run_command) -> HostSkills:
    if which("claude") is None:
        return HostSkills("claude-code", "unavailable", "Claude Code is not installed")
    try:
        result = runner(["claude", "plugin", "list", "--json"], timeout=60)
    except ToolNotFoundError:
        return HostSkills("claude-code", "unavailable", "Claude Code is not installed")
    if not result.ok:
        return HostSkills("claude-code", "error", "`claude plugin list --json` failed")
    try:
        listed = json.loads(result.stdout)
    except ValueError:
        return HostSkills("claude-code", "error", "plugin list output was not JSON")
    plugins: dict[str, PluginSkills] = {}
    for entry in listed if isinstance(listed, list) else []:
        base = str(entry.get("id", "")).split("@", 1)[0]
        if base not in KNOWLEDGE_WORK_PLUGINS:
            continue
        install = Path(str(entry.get("installPath", "")))
        plugins[base] = PluginSkills(
            plugin=base,
            enabled=bool(entry.get("enabled")),
            version=entry.get("version"),
            skills=_skills_in(base, install) if install.is_dir() else [],
        )
    return HostSkills("claude-code", "ok", "from `claude plugin list --json`", plugins)


def codex_skills(codex_home: Path | None = None) -> HostSkills:
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    config = home / "config.toml"
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HostSkills("codex", "unavailable", f"no Codex configuration at {config}")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return HostSkills("codex", "error", f"{config} could not be read: {exc}")
    plugins: dict[str, PluginSkills] = {}
    for key, settings in (data.get("plugins") or {}).items():
        base, _, marketplace = str(key).partition("@")
        if base not in KNOWLEDGE_WORK_PLUGINS:
            continue
        enabled = bool((settings or {}).get("enabled", True))
        install = home / "plugins" / "cache" / marketplace / base
        plugins[base] = PluginSkills(
            plugin=base,
            enabled=enabled,
            skills=_skills_in(base, install) if install.is_dir() else [],
        )
    return HostSkills("codex", "ok", f"from {config}", plugins)


def discover(host: str, runner: Runner = run_command) -> HostSkills:
    if host == "claude-code":
        return claude_code_skills(runner)
    if host == "codex":
        return codex_skills()
    raise ValueError(f"unknown host {host!r}; use one of {', '.join(HOSTS)}")


def render(found: HostSkills) -> str:
    lines = [f"Host {found.host}: {found.status} ({found.detail})"]
    for name in CORE_PLUGINS:
        plugin = found.plugins.get(name)
        state = "enabled" if plugin and plugin.enabled else "not installed or disabled"
        lines.append(f"  {name}: {state}")
    for stage in STAGE_HINTS:
        names = found.skills_for(stage)
        use = ", ".join(names) if names else "none - use ForgeFlow's built-in guidance"
        lines.append(f"  {stage}: {use}")
    return "\n".join(lines)
