"""Which Anthropic Product Management and Engineering skills a host can use: discovered.

ForgeFlow reuses these skills during discovery and design instead of copying their content. It
does not invoke them itself: the host agent loads them through its own skill mechanism, by the
exact names reported here, and falls back to ForgeFlow's short built-in guidance when none is
installed.

* Claude Code: ``claude plugin list --json`` reports enabled plugins and their install paths;
  skill names are ``<plugin>:<skill>`` for each ``skills/<skill>/SKILL.md``.
* Codex: enabled plugins come from ``[plugins."<name>@<marketplace>"]`` in
  ``$CODEX_HOME/config.toml`` and their skills from the plugin cache. Skills installed as plain
  skill folders (``<repo>/.agents/skills``, ``~/.agents/skills``, ``$CODEX_HOME/skills``) are
  recognised by the provenance in their ``SKILL.md`` front matter (``source-package``,
  ``source-skill``), not by their folder names, and are invoked as ``$<name>``.

Each skill is reported under its canonical name (``product-management:write-spec``) together
with the exact name the host invokes it by, which differs between hosts. Discovery is read-only
and does not prove a running session will expose a skill; the adapters check their own
session's skill list before relying on one.
"""

from __future__ import annotations

import json
import os
import re
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

_FRONT_MATTER_FIELD = re.compile(r"^\s*([A-Za-z0-9_-]+):\s*(.*?)\s*$")
_PACKAGE_VERSION = re.compile(r"-\d[\w.]*(?:-v\d+)?$")


@dataclass
class PluginSkills:
    plugin: str
    enabled: bool
    version: str | None = None
    skills: list[str] = field(default_factory=list)  # canonical <plugin>:<skill> names
    invoke: dict[str, str] = field(default_factory=dict)  # canonical -> name in this host

    def add(self, canonical: str, invocation: str) -> None:
        if canonical not in self.skills:  # the first (most specific) location wins
            self.skills.append(canonical)
            self.invoke[canonical] = invocation


@dataclass
class HostSkills:
    host: str
    status: str  # "ok" | "unavailable" | "error"
    detail: str
    plugins: dict[str, PluginSkills] = field(default_factory=dict)

    def enabled(self) -> list[str]:
        return [name for name, p in self.plugins.items() if p.enabled]

    def _names(self) -> dict[str, str]:
        return {s: p.invoke.get(s, s) for p in self.plugins.values() if p.enabled for s in p.skills}

    def skills_for(self, stage: str) -> list[str]:
        """The names this host invokes the stage's skills by, for those it has."""
        names = self._names()
        return [names[s] for s in STAGE_HINTS.get(stage, ()) if s in names]

    def canonical_for(self, stage: str) -> list[str]:
        names = self._names()
        return [s for s in STAGE_HINTS.get(stage, ()) if s in names]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["stages"] = {stage: self.skills_for(stage) for stage in STAGE_HINTS}
        data["fallback"] = not any(self.skills_for(stage) for stage in STAGE_HINTS)
        return data


def _skills_in(plugin: str, install: Path) -> list[str]:
    found = sorted({p.parent.name for p in install.glob("**/skills/*/SKILL.md")})
    return [f"{plugin}:{s}" for s in found]


def _plugin_skills(plugin: str, enabled: bool, install: Path, version: str | None) -> PluginSkills:
    entry = PluginSkills(plugin=plugin, enabled=enabled, version=version)
    for name in _skills_in(plugin, install) if install.is_dir() else []:
        entry.add(name, name)
    return entry


def front_matter(skill_md: Path) -> dict[str, str]:
    """``key: value`` pairs of a SKILL.md front matter, nested keys flattened. Best effort."""
    try:
        lines = skill_md.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        match = _FRONT_MATTER_FIELD.match(line)
        if match and match.group(2):
            fields.setdefault(match.group(1), match.group(2).strip("'\""))
    return fields


def plugin_of_package(package: str) -> str:
    """``Product Management-1.2.0-v40`` -> ``product-management``."""
    return re.sub(r"\s+", "-", _PACKAGE_VERSION.sub("", package.strip()).lower())


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
        plugins[base] = _plugin_skills(
            base, bool(entry.get("enabled")), install, entry.get("version")
        )
    return HostSkills("claude-code", "ok", "from `claude plugin list --json`", plugins)


def codex_skills(
    codex_home: Path | None = None,
    *,
    user_home: Path | None = None,
    repo_root: Path | None = None,
) -> HostSkills:
    home = codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    config = home / "config.toml"
    sources: list[str] = []
    plugins: dict[str, PluginSkills] = {}
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        sources.append(str(config))
    except FileNotFoundError:
        data = {}
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return HostSkills("codex", "error", f"{config} could not be read: {exc}")
    for key, settings in (data.get("plugins") or {}).items():
        base, _, marketplace = str(key).partition("@")
        if base not in KNOWLEDGE_WORK_PLUGINS:
            continue
        enabled = bool((settings or {}).get("enabled", True))
        install = home / "plugins" / "cache" / marketplace / base
        plugins[base] = _plugin_skills(base, enabled, install, None)

    # Plain skill folders, most specific first: the repository, the user, then CODEX_HOME.
    roots = [repo_root / ".agents" / "skills"] if repo_root else []
    roots += [(user_home or Path.home()) / ".agents" / "skills", home / "skills"]
    for root in roots:
        found_here = False
        for skill_md in sorted(root.glob("*/SKILL.md")) if root.is_dir() else []:
            meta = front_matter(skill_md)
            package, source = meta.get("source-package"), meta.get("source-skill")
            if not package or not source:
                continue
            plugin = plugin_of_package(package)
            if plugin not in KNOWLEDGE_WORK_PLUGINS:
                continue
            entry = plugins.setdefault(
                plugin, PluginSkills(plugin, True, version=meta.get("source-version"))
            )
            entry.add(f"{plugin}:{source}", f"${meta.get('name') or skill_md.parent.name}")
            found_here = True
        if found_here:
            sources.append(str(root))
    if not sources:
        return HostSkills("codex", "unavailable", f"no Codex configuration or skills ({config})")
    return HostSkills("codex", "ok", "from " + ", ".join(sources), plugins)


def discover(host: str, runner: Runner = run_command, repo_root: Path | None = None) -> HostSkills:
    if host == "claude-code":
        return claude_code_skills(runner)
    if host == "codex":
        return codex_skills(repo_root=repo_root)
    raise ValueError(f"unknown host {host!r}; use one of {', '.join(HOSTS)}")


def render(found: HostSkills) -> str:
    lines = [f"Host {found.host}: {found.status} ({found.detail})"]
    for name in CORE_PLUGINS:
        plugin = found.plugins.get(name)
        state = (
            f"enabled ({len(plugin.skills)} skills)"
            if plugin and plugin.enabled
            else "not installed or disabled"
        )
        lines.append(f"  {name}: {state}")
    for stage in STAGE_HINTS:
        pairs = zip(found.skills_for(stage), found.canonical_for(stage), strict=True)
        names = [name if name == canon else f"{name} ({canon})" for name, canon in pairs]
        use = ", ".join(names) if names else "none - use ForgeFlow's built-in guidance"
        lines.append(f"  {stage}: {use}")
    return "\n".join(lines)
