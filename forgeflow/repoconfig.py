"""Repository-configured build settings: deterministic checks and the builder's path scope.

The configuration is the committed file ``.forge/forgeflow.toml`` of the target repository::

    [checks]
    timeout_seconds = 900            # per command (default 600)

    [[checks.commands]]
    name = "unit tests"
    argv = ["{python}", "-m", "unittest", "discover", "-s", "tests", "-t", "."]

    [build]
    allowed_paths = ["*"]                           # glob patterns the builder may change
    protected_paths = [".forge/*", ".github/workflows/*"]   # changes here need a human

``{python}`` in an argument is replaced with the interpreter running ForgeFlow (3.13+), so a
configuration works on Windows, where a bare ``python`` may be an older install.

The file is read with ``git show`` from a commit the builder cannot change: the job's base
commit, or, when the base has no configuration yet, the main checkout's ``HEAD``. It is never
read from the builder's worktree, so a builder cannot weaken the checks that judge its own
work. Where it came from is recorded with the check evidence.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from forgeflow.errors import ValidationError
from forgeflow.gitrepo import git, git_ok
from forgeflow.proc import Runner, run_command

CONFIG_PATH = ".forge/forgeflow.toml"
DEFAULT_CHECK_TIMEOUT = 600
DEFAULT_ALLOWED_PATHS = ("*",)
# The check configuration and CI definitions decide what "validated" means.
DEFAULT_PROTECTED_PATHS = (".forge/*", ".github/workflows/*")


@dataclass(frozen=True)
class CheckCommand:
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class RepoConfig:
    checks: tuple[CheckCommand, ...]
    check_timeout: int = DEFAULT_CHECK_TIMEOUT
    allowed_paths: tuple[str, ...] = DEFAULT_ALLOWED_PATHS
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    source: dict[str, str] = field(default_factory=dict)  # {"path", "commit", "origin"}

    @property
    def argv_list(self) -> tuple[tuple[str, ...], ...]:
        return tuple(c.argv for c in self.checks)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks)


def parse(text: str, *, python: str = sys.executable) -> RepoConfig:
    """Validate the TOML text of a configuration. Raises ValidationError with the reason."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValidationError(f"{CONFIG_PATH} is not valid TOML: {exc}") from exc
    checks_table = _table(data, "checks")
    raw_commands = checks_table.get("commands")
    if not isinstance(raw_commands, list) or not raw_commands:
        raise ValidationError(f"{CONFIG_PATH} needs at least one [[checks.commands]] entry")
    commands: list[CheckCommand] = []
    for i, entry in enumerate(raw_commands):
        where = f"checks.commands[{i}]"
        if not isinstance(entry, dict):
            raise ValidationError(f"{CONFIG_PATH}: {where} must be a table")
        argv = entry.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(a, str) and a for a in argv)
        ):
            raise ValidationError(f"{CONFIG_PATH}: {where}.argv must be a non-empty string list")
        name = entry.get("name", " ".join(argv))
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"{CONFIG_PATH}: {where}.name must be a non-empty string")
        commands.append(
            CheckCommand(name.strip(), tuple(a.replace("{python}", python) for a in argv))
        )
    timeout = checks_table.get("timeout_seconds", DEFAULT_CHECK_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 0 < timeout <= 24 * 3600:
        raise ValidationError(f"{CONFIG_PATH}: checks.timeout_seconds must be 1..86400")
    build = _table(data, "build", optional=True)
    allowed = _patterns(build, "allowed_paths", DEFAULT_ALLOWED_PATHS)
    protected = _patterns(build, "protected_paths", DEFAULT_PROTECTED_PATHS)
    if not allowed:
        raise ValidationError(f"{CONFIG_PATH}: build.allowed_paths must not be empty")
    return RepoConfig(tuple(commands), timeout, allowed, protected)


def _table(data: dict[str, Any], key: str, *, optional: bool = False) -> dict[str, Any]:
    value = data.get(key)
    if value is None and optional:
        return {}
    if not isinstance(value, dict):
        raise ValidationError(f"{CONFIG_PATH} needs a [{key}] table")
    return value


def _patterns(table: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise ValidationError(f"{CONFIG_PATH}: build.{key} must be a list of glob patterns")
    for pattern in value:
        p = PurePosixPath(pattern)
        if p.is_absolute() or ".." in p.parts or "\\" in pattern or ":" in pattern:
            raise ValidationError(f"{CONFIG_PATH}: {pattern!r} must be repository-relative")
    return tuple(value)


def _show(repo: Path, commit: str, runner: Runner) -> str | None:
    result = git(repo, "show", f"{commit}:{CONFIG_PATH}", runner=runner, read_only=True)
    return result.stdout if result.ok else None


def load(
    repo_root: Path, base_sha: str, *, runner: Runner = run_command, python: str = sys.executable
) -> RepoConfig:
    """The configuration at the job's base commit, else at the main checkout's HEAD.

    Raises ValidationError when neither commit has one: ForgeFlow does not invent checks, and
    a build is never judged without them.
    """
    candidates = [("base", base_sha)]
    head = git_ok(repo_root, "rev-parse", "HEAD", runner=runner, read_only=True)
    if head != base_sha:
        candidates.append(("main-checkout-head", head))
    for origin, commit in candidates:
        text = _show(repo_root, commit, runner)
        if text is not None:
            config = parse(text, python=python)
            source = {"path": CONFIG_PATH, "commit": commit, "origin": origin}
            return RepoConfig(
                config.checks,
                config.check_timeout,
                config.allowed_paths,
                config.protected_paths,
                source,
            )
    raise ValidationError(
        f"no {CONFIG_PATH} in the job's base commit {base_sha[:12]} or in the main checkout's "
        "HEAD; commit one that lists the repository's checks (see docs/getting-started.md). "
        "ForgeFlow does not build without deterministic checks."
    )
