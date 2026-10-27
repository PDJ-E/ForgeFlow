"""``forgeflow doctor``: read-only inspection of the host, repository and providers.

The Codex probe may create its minimal managed application-data profile; it never logs in
or changes the personal profile or repository. Optional capabilities
that are absent are reported as such; only missing *required* capabilities fail the report.
No model is invoked, so whether plugin skills are usable inside provider subprocesses is
reported as "not probed" rather than guessed.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from forgeflow import MIN_PYTHON, hostskills
from forgeflow.errors import ForgeFlowError, ToolNotFoundError
from forgeflow.gitrepo import discover_identity, git, porcelain_status
from forgeflow.hostskills import KNOWLEDGE_WORK_PLUGINS
from forgeflow.proc import CommandResult, Runner, run_command, which
from forgeflow.providers import REAL_PROVIDERS, CodexProvider, ProviderCapabilities
from forgeflow.store import RUNTIME_RELATIVE, JobStore

MIN_GIT = (2, 31)  # `git rev-parse --path-format=absolute`
SKILL_ADAPTERS = {
    "claude": Path(".claude") / "skills" / "forge" / "SKILL.md",
    "codex": Path(".agents") / "skills" / "forge" / "SKILL.md",
}

OK, WARN, MISSING, ERROR, INFO = "ok", "warn", "missing", "error", "info"


@dataclass
class Check:
    name: str
    status: str
    required: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in (OK, WARN, INFO)


@dataclass
class EnvironmentReport:
    checks: list[Check]

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks if c.required)

    def get(self, name: str) -> Check | None:
        return next((c for c in self.checks if c.name == name), None)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "checks": [asdict(c) for c in self.checks]}


def _version_tuple(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(g) for g in match.groups() if g is not None) if match else ()


def _try(runner: Runner, argv: list[str], timeout: float = 30) -> CommandResult | None:
    try:
        return runner(argv, timeout=timeout)
    except ToolNotFoundError:
        return None


def check_python() -> Check:
    version = tuple(sys.version_info[:3])
    text = ".".join(str(v) for v in version)
    data = {"version": text, "executable": sys.executable}
    if version[:2] < MIN_PYTHON:
        need = ".".join(str(v) for v in MIN_PYTHON)
        return Check(
            "python",
            ERROR,
            True,
            f"Python {text} at {sys.executable} is older than {need}; on Windows run "
            f"`py -{need} -m forgeflow`",
            data,
        )
    return Check("python", OK, True, f"Python {text} ({sys.executable})", data)


def check_git(runner: Runner) -> Check:
    if which("git") is None:
        return Check("git", MISSING, True, "git is not on PATH")
    result = _try(runner, ["git", "--version"])
    text = result.stdout.strip() if result and result.ok else ""
    version = _version_tuple(text)
    if version and version[:2] < MIN_GIT:
        return Check("git", ERROR, True, f"{text}: 2.31 or newer is required", {"version": text})
    return Check("git", OK, True, text or "git (version unknown)", {"version": text})


def check_repository(start: Path, runner: Runner) -> tuple[Check, Path | None]:
    try:
        identity = discover_identity(start, runner)
    except ForgeFlowError as exc:
        return Check("repository", ERROR, True, f"not inside a git repository: {exc}"), None
    root = Path(identity.root)
    branch = git(root, "branch", "--show-current", runner=runner, read_only=True)
    dirty = porcelain_status(root, runner)
    data = {
        **identity.to_dict(),
        "current_branch": branch.stdout.strip() or None,
        "uncommitted_paths": len(dirty),
    }
    summary = f"{identity.slug or 'no GitHub remote'} at {root}, base branch {identity.base_branch}"
    if dirty:
        summary += f"; {len(dirty)} uncommitted path(s) in the main checkout (left untouched)"
    status = OK if identity.slug else WARN
    return Check("repository", status, True, summary, data), root


def check_runtime(root: Path) -> Check:
    store = JobStore.for_repo(root)
    jobs, problems = store.list_jobs()
    data = {"runtime_dir": str(root / RUNTIME_RELATIVE), "jobs": len(jobs), "unreadable": problems}
    if problems:
        return Check("runtime", WARN, False, f"{len(problems)} unreadable job record(s)", data)
    return Check("runtime", INFO, False, f"{len(jobs)} local job(s)", data)


def check_github(runner: Runner, slug: str | None, network: bool) -> list[Check]:
    if which("gh") is None:
        return [
            Check(
                "gh",
                MISSING,
                False,
                "GitHub CLI not found; GitHub Issues/Projects linkage is unavailable",
            )
        ]
    version = _try(runner, ["gh", "--version"])
    version_text = version.stdout.strip().splitlines()[0] if version and version.ok else "gh"
    if not network:
        return [Check("gh", INFO, False, f"{version_text}; authentication not checked (offline)")]
    auth = _try(runner, ["gh", "auth", "status"])
    if auth is None or not auth.ok:
        detail = auth.describe() if auth else "gh not runnable"
        return [Check("gh", WARN, False, f"{version_text}; not authenticated ({detail})")]
    checks = [Check("gh", OK, False, f"{version_text}; authenticated")]

    scopes = _token_scopes(runner)
    if scopes is None:
        checks.append(
            Check(
                "github-project",
                WARN,
                False,
                "token scopes could not be determined; Project access unverified",
            )
        )
        return checks
    data: dict[str, Any] = {"token_scopes": sorted(scopes)}
    if not {"read:project", "project"} & scopes:
        checks.append(
            Check(
                "github-project",
                WARN,
                False,
                "GitHub Project board not discoverable: the token lacks `read:project`. "
                "Run `gh auth refresh -s read:project` (add `-s project` later for board updates).",
                data,
            )
        )
        return checks
    if slug:
        projects = linked_projects(runner, slug)
        data["projects"] = projects
        if projects is None:
            checks.append(Check("github-project", WARN, False, "Project lookup failed", data))
        elif not projects:
            checks.append(
                Check(
                    "github-project",
                    WARN,
                    False,
                    f"no GitHub Project is linked to {slug} or its owner",
                    data,
                )
            )
        else:
            names = ", ".join(f"#{p['number']} {p['title']}" for p in projects)
            checks.append(Check("github-project", OK, False, f"Projects visible: {names}", data))
    return checks


def _token_scopes(runner: Runner) -> set[str] | None:
    result = _try(runner, ["gh", "api", "--include", "user"])
    if result is None or not result.ok:
        return None
    for line in result.stdout.splitlines():
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "x-oauth-scopes":
            return {s.strip() for s in value.split(",") if s.strip()}
        if not line.strip():
            break  # end of headers; fine-grained tokens send no scope header
    return None


def linked_projects(runner: Runner, slug: str) -> list[dict[str, Any]] | None:
    owner, _, name = slug.partition("/")
    query = (
        "query($owner: String!, $name: String!) {"
        " repository(owner: $owner, name: $name) { projectsV2(first: 20) {"
        "  nodes { number title url closed } } }"
        " repositoryOwner(login: $owner) { ... on ProjectV2Owner { projectsV2(first: 20) {"
        "  nodes { number title url closed } } } } }"
    )
    result = _try(
        runner,
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
        ],
    )
    if result is None or not result.ok:
        return None
    try:
        data = json.loads(result.stdout)["data"]
    except (ValueError, KeyError, TypeError):
        return None
    seen: dict[int, dict[str, Any]] = {}
    for scope, source in (
        ("repository", data.get("repository")),
        ("owner", data.get("repositoryOwner")),
    ):
        for node in ((source or {}).get("projectsV2") or {}).get("nodes") or []:
            if node and not node.get("closed"):
                entry = seen.setdefault(
                    int(node["number"]),
                    {
                        "number": int(node["number"]),
                        "title": node["title"],
                        "url": node["url"],
                        "linked_to_repository": False,
                    },
                )
                if scope == "repository":
                    entry["linked_to_repository"] = True
    return sorted(seen.values(), key=lambda p: p["number"])


def check_providers(runner: Runner, workdir: Path | None = None) -> list[Check]:
    checks: list[Check] = []
    caps_list: list[ProviderCapabilities] = []
    for name, cls in REAL_PROVIDERS.items():
        provider = cls()
        caps = (
            provider.detect(runner, workdir=workdir)
            if isinstance(provider, CodexProvider)
            else provider.detect(runner)
        )
        caps_list.append(caps)
        if not caps.installed:
            checks.append(
                Check(f"provider:{name}", MISSING, False, "; ".join(caps.notes), caps.to_dict())
            )
            continue
        status = OK if caps.available else WARN
        auth = {True: "authenticated", False: "not authenticated", None: "authentication unknown"}[
            caps.authenticated
        ]
        missing = [flag for flag, present in caps.features.items() if not present]
        summary = f"{caps.version or 'version unknown'}; {auth} ({caps.auth_detail})"
        if caps.isolation_error:
            summary += f"; {caps.isolation_error}"
        if missing:
            summary += f"; options not found in help: {', '.join(missing)}"
        checks.append(Check(f"provider:{name}", status, False, summary, caps.to_dict()))
    available = [c.provider for c in caps_list if c.available]
    if available:
        checks.append(
            Check(
                "providers",
                OK,
                False,
                f"available builders/reviewers: {', '.join(available)} "
                "(`forgeflow run` checks each again before dispatching it)",
                {"available": available},
            )
        )
    else:
        checks.append(
            Check(
                "providers", WARN, False, "no authenticated provider found; jobs cannot be executed"
            )
        )
    return checks


def check_plugins(runner: Runner) -> Check:
    found = hostskills.claude_code_skills(runner)
    if found.status == "unavailable":
        return Check(
            "anthropic-plugins",
            MISSING,
            False,
            "Claude Code not installed; Knowledge Work plugins cannot be checked",
        )
    if found.status != "ok":
        return Check("anthropic-plugins", WARN, False, found.detail)
    enabled = [n for n in KNOWLEDGE_WORK_PLUGINS if n in found.enabled()]
    absent = [n for n in KNOWLEDGE_WORK_PLUGINS if n not in enabled]
    data = {
        "plugins": {name: asdict(p) for name, p in found.plugins.items()},
        "subprocess_access": "not probed by doctor (needs a model call)",
    }
    summary = f"enabled: {', '.join(enabled) or 'none'}"
    if absent:
        summary += f"; not enabled: {', '.join(absent)} (ForgeFlow falls back to its own prompts)"
    core = set(hostskills.CORE_PLUGINS)
    return Check("anthropic-plugins", OK if core <= set(enabled) else WARN, False, summary, data)


def check_skill_adapters(root: Path) -> Check:
    present = {host: (root / rel).is_file() for host, rel in SKILL_ADAPTERS.items()}
    summary = ", ".join(f"{host}: {'present' if ok else 'absent'}" for host, ok in present.items())
    return Check(
        "skill-adapters",
        INFO,
        False,
        f"/forge (Claude Code) and $forge (Codex) adapters in this repo - {summary}",
        {"present": present},
    )


def check_knowledge() -> Check:
    return Check(
        "knowledge",
        INFO,
        False,
        "knowledge sources are not implemented until Checkpoint D; ForgeFlow does not "
        "need Obsidian or MCP to run",
    )


def inspect_environment(
    start: Path, runner: Runner = run_command, network: bool = True
) -> EnvironmentReport:
    checks = [check_python(), check_git(runner)]
    repo_check, root = check_repository(start, runner)
    checks.append(repo_check)
    if root is not None:
        checks.append(check_runtime(root))
    checks.extend(check_github(runner, repo_check.data.get("slug"), network))
    checks.extend(check_providers(runner, root or start))
    checks.append(check_plugins(runner))
    if root is not None:
        checks.append(check_skill_adapters(root))
    checks.append(check_knowledge())
    return EnvironmentReport(checks)
