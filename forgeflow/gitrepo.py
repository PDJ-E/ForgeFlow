"""Read-mostly git helpers: repository discovery and identity."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from forgeflow.errors import GitError
from forgeflow.proc import CommandResult, Runner, redact_credentials, run_command

# Read-only inspection must not take .git/index.lock and collide with the user's own git use.
READ_ONLY_ENV = {"GIT_OPTIONAL_LOCKS": "0"}

_GITHUB_REMOTE = re.compile(
    r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"(?P<owner>[A-Za-z0-9-]+)/(?P<name>[A-Za-z0-9._-]+?)(?:\.git)?/?$"
)


def git(
    repo: Path, *args: str, runner: Runner = run_command, read_only: bool = False
) -> CommandResult:
    return runner(["git", "-C", str(repo), *args], env=READ_ONLY_ENV if read_only else None)


def git_ok(repo: Path, *args: str, runner: Runner = run_command, read_only: bool = False) -> str:
    result = git(repo, *args, runner=runner, read_only=read_only)
    if not result.ok:
        raise GitError(result.describe())
    return result.stdout.strip()


def github_slug_from_remote(url: str) -> str | None:
    """``owner/name`` for a github.com remote URL, else None."""
    match = _GITHUB_REMOTE.match(url.strip())
    if not match:
        return None
    return f"{match.group('owner')}/{match.group('name')}"


@dataclass(frozen=True)
class RepoIdentity:
    root: str
    slug: str | None
    remote_url: str | None
    base_branch: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "root": self.root,
            "slug": self.slug,
            "remote_url": self.remote_url,
            "base_branch": self.base_branch,
        }


def main_worktree_root(path: Path, runner: Runner = run_command) -> Path:
    """The main checkout's root, even when ``path`` is inside a linked worktree."""
    common = git_ok(
        path,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        runner=runner,
        read_only=True,
    )
    common_path = Path(common)
    if common_path.name != ".git":
        raise GitError(f"unsupported repository layout (bare or separate git dir): {common}")
    return common_path.parent


def discover_identity(path: Path, runner: Runner = run_command) -> RepoIdentity:
    root = main_worktree_root(path, runner)
    remote = git(root, "remote", "get-url", "origin", runner=runner, read_only=True)
    raw_url = remote.stdout.strip() if remote.ok else None
    head = git(
        root,
        "symbolic-ref",
        "--quiet",
        "--short",
        "refs/remotes/origin/HEAD",
        runner=runner,
        read_only=True,
    )
    base = head.stdout.strip().split("/", 1)[-1] if head.ok and head.stdout.strip() else "main"
    return RepoIdentity(
        root=str(root),
        slug=github_slug_from_remote(raw_url) if raw_url else None,
        # Never keep credentials embedded in the remote (https://user:token@host/...).
        remote_url=redact_credentials(raw_url) if raw_url else None,
        base_branch=base,
    )


def resolve_commit(repo: Path, ref: str, runner: Runner = run_command) -> str:
    return git_ok(
        repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", runner=runner, read_only=True
    )


def ref_exists(repo: Path, ref: str, runner: Runner = run_command) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", ref, runner=runner, read_only=True).ok


def default_base_ref(repo: Path, base_branch: str, runner: Runner = run_command) -> str:
    """Prefer the remote-tracking base so a stale or diverged local ``main`` is not used."""
    remote_ref = f"refs/remotes/origin/{base_branch}"
    if ref_exists(repo, remote_ref, runner):
        return f"origin/{base_branch}"
    return base_branch


def porcelain_status(repo: Path, runner: Runner = run_command) -> list[str]:
    # Not git_ok: stripping would eat the leading status column of the first entry.
    result = git(
        repo, "status", "--porcelain=v1", "--untracked-files=all", runner=runner, read_only=True
    )
    if not result.ok:
        raise GitError(result.describe())
    return [line for line in result.stdout.splitlines() if line.strip()]
