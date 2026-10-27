"""The minimal, independent Codex configuration used only by managed subprocesses.

This is configuration isolation, not an OS security boundary. Never read or copy the
personal Codex profile, and never invoke interactive login from the orchestrator.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from forgeflow.errors import ProviderError, ToolNotFoundError
from forgeflow.proc import CommandResult, Runner

_CONFIG = 'cli_auth_credentials_store = "file"\n'
# Independent CODEX_HOME does not disable account-backed plugin discovery after login.
# Apply these global switches to probes and execution, then check their effective values.
DISABLED_FEATURES = ("plugins", "remote_plugin", "apps")
ISOLATION_ARGS = tuple(arg for feature in DISABLED_FEATURES for arg in ("--disable", feature))
# Native Windows needs an explicit sandbox backend; without it Codex can fall back to a
# read-only session even with --sandbox workspace-write. Passed per process, never persisted.
WINDOWS_SANDBOX_ARGS = ("-c", 'windows.sandbox="unelevated"')


def sandbox_args() -> tuple[str, ...]:
    return WINDOWS_SANDBOX_ARGS if sys.platform == "win32" else ()


_LOCAL_SOURCES = (
    ".codex/config.toml",
    ".codex/hooks.json",
    ".codex/hooks",
    ".codex/rules",
    ".agents/plugins/marketplace.json",
    ".claude-plugin/marketplace.json",
)


class CodexRuntimeError(ProviderError):
    """Isolation or independent authentication could not be established."""


class CodexAuthenticationError(CodexRuntimeError):
    """A human must authenticate the isolated profile."""


def profile_path() -> Path:
    """Persistent application data, independent of inherited CODEX_HOME."""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform.startswith("linux"):
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    else:
        raise CodexRuntimeError("Codex isolation supports Windows and Linux only")
    if not base.is_absolute():
        raise CodexRuntimeError("Codex isolation requires an absolute application-data path")
    return base / "forgeflow" / "codex"


def system_config_paths() -> tuple[Path, ...]:
    if sys.platform == "win32":
        # Codex uses ProgramData for system configuration on Windows.
        return (
            Path(os.environ.get("PROGRAMDATA") or "C:/ProgramData") / "OpenAI" / "Codex",
            Path.home() / ".codex" / "managed_config.toml",
        )
    return (Path("/etc/codex"),)


def _present(path: Path) -> bool:
    # Path.exists/is_symlink suppress OSError on newer Python versions. Security
    # checks must distinguish missing paths from paths we cannot inspect.
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _guard_sources(workdir: Path, profile: Path) -> None:
    # Include all ancestors: linked worktrees and reviewer snapshots need not contain
    # a normal .git directory, and trust/root detection has varied across CLI versions.
    for directory in (workdir, *workdir.parents):
        for relative in _LOCAL_SOURCES:
            if directory == Path.home() and relative.startswith(".codex/"):
                continue  # CODEX_HOME replaces this user-level source, not repository settings.
            source = directory / relative
            if _present(source):
                raise CodexRuntimeError(
                    f"Codex isolation blocked by configuration source {source}; "
                    "use a checkout without Codex settings/hooks/rules or plugin marketplaces"
                )
    for directory in system_config_paths():
        if _present(directory):
            raise CodexRuntimeError(
                "Codex isolation cannot establish a minimal runtime "
                f"with system config at {directory}"
            )
    for directory in (profile, *profile.parents):
        _present(directory)  # do not hide permission errors while examining ancestors
        if directory.is_symlink() or directory.is_junction():
            raise CodexRuntimeError(f"Codex isolation refuses linked profile path {directory}")
        if _present(directory / ".git"):
            raise CodexRuntimeError("Codex isolation profile must be outside any repository")
    if profile.resolve() == (Path.home() / ".codex").resolve():
        raise CodexRuntimeError("Codex isolation profile must differ from the personal profile")


_SYSTEM_SKILLS = ".system"
_SYSTEM_SKILLS_MARKER = ".codex-system-skills.marker"
_MAX_SYSTEM_SKILL_ENTRIES = 5000
_MAX_SYSTEM_SKILL_DEPTH = 8
_REPARSE_POINT = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _linked(path: Path, info: os.stat_result) -> bool:
    return (
        stat.S_ISLNK(info.st_mode)
        or path.is_junction()
        or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)
    )


def _check_skills(skills: Path) -> None:
    """Allow only Codex's own bundled skills (``skills/.system``), never user skills.

    Codex installs and refreshes ``.system`` itself: it replaces the whole directory
    whenever its embedded fingerprint, stored in the marker, no longer matches, so the
    content legitimately changes with each Codex release and is not pinned here. The
    layout is validated instead: nothing but ``.system`` beside it, and inside it only
    plain, unlinked directories and files within sane bounds. Content is not authenticated;
    a skill planted inside ``.system`` by the same user is not detected (see the docs).
    """

    def refuse(entry: Path, why: str) -> CodexRuntimeError:
        return CodexRuntimeError(f"Codex isolation refuses profile extensions at {entry}: {why}")

    try:
        info = skills.lstat()
    except FileNotFoundError:
        return
    if _linked(skills, info) or not stat.S_ISDIR(info.st_mode):
        raise refuse(skills, "not a plain directory")
    for child in skills.iterdir():
        if child.name != _SYSTEM_SKILLS:
            raise refuse(child, "only Codex's bundled skills/.system is accepted")
    system = skills / _SYSTEM_SKILLS
    try:
        info = system.lstat()
    except FileNotFoundError:
        return
    if _linked(system, info) or not stat.S_ISDIR(info.st_mode):
        raise refuse(system, "not a plain directory")
    pending = [(system, 0)]
    seen = 0
    while pending:
        directory, depth = pending.pop()
        for entry in directory.iterdir():
            seen += 1
            if seen > _MAX_SYSTEM_SKILL_ENTRIES or depth >= _MAX_SYSTEM_SKILL_DEPTH:
                raise refuse(entry, "bundled skills exceed the expected size")
            info = entry.lstat()
            if _linked(entry, info):
                raise refuse(entry, "linked path")
            if stat.S_ISDIR(info.st_mode):
                pending.append((entry, depth + 1))
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise refuse(entry, "not a plain file")
            elif directory == system and entry.name != _SYSTEM_SKILLS_MARKER:
                raise refuse(entry, "unexpected file")


@dataclass(frozen=True)
class CodexRuntime:
    home: Path
    cwd: Path
    env: dict[str, str]

    @classmethod
    def prepare(cls, workdir: Path | None = None) -> CodexRuntime:
        try:
            home = profile_path()
            target = workdir.resolve() if workdir is not None else home
            try:
                target_stat = target.stat()
            except FileNotFoundError:
                # Preflight precedes worktree creation. Probe from the independent profile,
                # but still inspect the intended worktree and every ancestor below.
                cwd = home
            else:
                if not stat.S_ISDIR(target_stat.st_mode):
                    raise CodexRuntimeError(f"Codex target {target} is not a directory")
                cwd = target
            _guard_sources(target, home)
            if cwd != target:
                _guard_sources(cwd, home)
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            config = home / "config.toml"
            if config.is_symlink() or config.is_junction():
                raise CodexRuntimeError("Codex isolation refuses a linked profile config")
            try:
                with config.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(_CONFIG)
            except FileExistsError:
                pass
            if config.read_text(encoding="utf-8") != _CONFIG:
                raise CodexRuntimeError(
                    f"Codex isolation requires the minimal ForgeFlow config at {config}; "
                    "remove custom settings from this managed profile before retrying"
                )
            auth = home / "auth.json"
            if _present(auth) and (
                auth.is_symlink() or auth.is_junction() or auth.stat().st_nlink > 1
            ):
                raise CodexRuntimeError("Codex isolation refuses linked authentication")
            _check_skills(home / "skills")
            for relative in (
                "plugins",
                "hooks",
                "rules",
                "hooks.json",
                "managed_config.toml",
            ):
                entry = home / relative
                if _present(entry) and (
                    entry.is_symlink()
                    or entry.is_junction()
                    or not entry.is_dir()
                    or (entry.is_dir() and any(entry.iterdir()))
                ):
                    raise CodexRuntimeError(
                        f"Codex isolation refuses profile extensions at {entry}"
                    )
            # The runner merges overrides into the inherited environment. Empty values
            # neutralize inherited Codex overrides and API credentials without mutating it.
            env = {key: "" for key in os.environ if key.upper().startswith("CODEX_")}
            env.update(
                CODEX_HOME=str(home),
                OPENAI_API_KEY="",
                OPENAI_ORG_ID="",
                OPENAI_ORGANIZATION="",
                OPENAI_PROJECT_ID="",
                OPENAI_BASE_URL="",
            )
            return cls(home, cwd, env)
        except (OSError, ValueError) as exc:
            raise CodexRuntimeError(
                "Codex isolation could not inspect the target or prepare the managed profile; "
                "check target/ancestor and application-data directory permissions "
                "(no personal-profile fallback)"
            ) from exc

    def login_instructions(self) -> str:
        if sys.platform == "win32":
            # Run in a separate PowerShell so the human's current environment stays intact.
            path = str(self.home).replace("'", "''")
            script = f"$env:CODEX_HOME = '{path}'; Set-Location -LiteralPath '{path}'; codex login"
            command = "powershell -NoProfile -Command '" + script.replace("'", "''") + "'"
        else:
            path = shlex.quote(str(self.home))
            command = f"(cd {path} && CODEX_HOME={path} codex login)"
        return f"Complete interactive login in your terminal with {command}, then retry ForgeFlow."

    def verify(self, runner: Runner, command: Sequence[str]) -> None:
        """Inventory the effective cwd, then check auth; never expose raw diagnostic output."""
        features = self._probe(runner, [*command, "features", "list"])
        effective: dict[str, list[str]] = {}
        if features.ok:
            for line in features.stdout.splitlines():
                fields = line.split()
                if fields and fields[0] in DISABLED_FEATURES:
                    effective.setdefault(fields[0], []).append(fields[-1])
        unsafe = [feature for feature in DISABLED_FEATURES if effective.get(feature) != ["false"]]
        if unsafe:
            raise CodexRuntimeError(
                "Codex isolation could not verify disabled plugin/account features: "
                + ", ".join(unsafe)
                + ". No model was run."
            )
        for args, kind in (
            (["mcp", "list", "--json"], "MCP"),
            (["plugin", "list", "--json"], "plugin"),
        ):
            result = self._probe(runner, [*command, *args])
            try:
                inventory = json.loads(result.stdout) if result.ok else None
            except ValueError:
                inventory = None
            clean = (
                inventory == []
                if kind == "MCP"
                else (
                    isinstance(inventory, dict)
                    and set(inventory) == {"installed", "available"}
                    and inventory.get("installed") == []
                    and inventory.get("available") == []
                )
            )
            if not clean:
                reason = " (probe timed out after 30 seconds)" if result.timed_out else ""
                raise CodexRuntimeError(
                    f"Codex isolation could not verify an empty {kind} inventory{reason}; "
                    "check managed/system configuration and CLI compatibility. No model was run."
                )
        auth = self._probe(runner, [*command, "login", "status"])
        text = (auth.stdout + auth.stderr).lower()
        if not auth.ok or "logged in" not in text or "not logged in" in text:
            raise CodexAuthenticationError(
                "Codex isolated profile authentication is missing or unverified. "
                + self.login_instructions()
            )

    def _probe(self, runner: Runner, argv: list[str]) -> CommandResult:
        try:
            return runner(argv, cwd=self.cwd, env=self.env, timeout=30)
        except (OSError, ToolNotFoundError) as exc:
            raise CodexRuntimeError(
                f"Codex isolation verification could not run the CLI in {self.cwd}; "
                "check the executable and directory permissions, then retry"
            ) from exc
