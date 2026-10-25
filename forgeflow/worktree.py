"""Per-job git worktrees under ``.forge/worktrees/<job-id>`` on branch ``forge/<job-id>``.

Safety rules:

* The main checkout is never switched, reset, stashed or cleaned; only ``git worktree add``
  and read-only queries run against it.
* An existing directory, branch or worktree that this job did not create is never reused,
  repaired, pruned or removed automatically. Each such case is a ``WorktreeError`` naming
  the conflict so a human can decide.
* Removal goes through ``git worktree remove`` without ``--force``, and only for a clean,
  registered worktree that ForgeFlow created. The branch is always kept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from forgeflow import fsutil
from forgeflow.errors import GitError, ValidationError, WorktreeError
from forgeflow.gitrepo import (
    default_base_ref,
    git,
    git_ok,
    porcelain_status,
    ref_exists,
    resolve_commit,
)
from forgeflow.jobs import Job, JobState, Worktree, branch_for
from forgeflow.proc import Runner, run_command
from forgeflow.store import JobStore

WORKTREES_RELATIVE = Path(".forge") / "worktrees"

# Worktree creation is part of execution, so it happens only after Gate A.
_PREPARE_STATES = frozenset({JobState.PLAN_APPROVED})
# Re-attaching an already-recorded worktree is allowed while the job still needs it.
_REVISIT_STATES = frozenset(
    {
        JobState.PLAN_APPROVED,
        JobState.READY,
        JobState.INTERRUPTED,
        JobState.NEEDS_HUMAN,
        JobState.AWAITING_MERGE_APPROVAL,
    }
)


@dataclass(frozen=True)
class WorktreeEntry:
    path: str
    head: str | None
    branch: str | None  # full ref, e.g. refs/heads/forge/ff-...
    detached: bool = False
    locked: bool = False
    prunable: bool = False


@dataclass(frozen=True)
class WorktreeStatus:
    path: str
    exists: bool
    registered: bool
    branch: str | None
    head: str | None
    dirty_paths: list[str] = field(default_factory=list)
    prunable: bool = False

    @property
    def dirty(self) -> bool:
        return bool(self.dirty_paths)


def parse_worktree_list(porcelain: str) -> list[WorktreeEntry]:
    entries: list[WorktreeEntry] = []
    block: dict[str, str] = {}
    for line in [*porcelain.splitlines(), ""]:
        if not line.strip():
            if "worktree" in block:
                entries.append(
                    WorktreeEntry(
                        path=block["worktree"],
                        head=block.get("HEAD"),
                        branch=block.get("branch"),
                        detached="detached" in block,
                        locked="locked" in block,
                        prunable="prunable" in block,
                    )
                )
            block = {}
            continue
        key, _, value = line.partition(" ")
        block[key] = value
    return entries


class WorktreeManager:
    def __init__(
        self, repo_root: Path, runner: Runner = run_command, worktrees_dir: Path | None = None
    ) -> None:
        self.repo_root = repo_root
        self.runner = runner
        self.worktrees_dir = worktrees_dir or repo_root / WORKTREES_RELATIVE

    def path_for(self, job_id: str) -> Path:
        return self.worktrees_dir / job_id

    def list_entries(self) -> list[WorktreeEntry]:
        out = git_ok(
            self.repo_root, "worktree", "list", "--porcelain", runner=self.runner, read_only=True
        )
        return parse_worktree_list(out)

    def _entry_for(self, path: Path) -> WorktreeEntry | None:
        key = fsutil.normalized(path)
        for entry in self.list_entries():
            if fsutil.normalized(entry.path) == key:
                return entry
        return None

    def _branch_checked_out_elsewhere(self, branch: str, path: Path) -> str | None:
        key = fsutil.normalized(path)
        for entry in self.list_entries():
            if entry.branch == f"refs/heads/{branch}" and fsutil.normalized(entry.path) != key:
                return entry.path
        return None

    def _prepare_parent(self) -> None:
        fsutil.write_self_ignoring_gitignore(self.worktrees_dir)
        probe = self.worktrees_dir / "probe"
        check = git(
            self.repo_root,
            "check-ignore",
            "--quiet",
            "--no-index",
            str(probe),
            runner=self.runner,
            read_only=True,
        )
        if not check.ok:
            raise WorktreeError(
                f"{self.worktrees_dir} is not ignored by git; refusing to create worktrees that "
                "would appear as untracked files in the main checkout"
            )

    def inspect(self, path: Path) -> WorktreeStatus:
        entry = self._entry_for(path)
        exists = path.exists()
        dirty: list[str] = []
        if entry is not None and exists:
            dirty = porcelain_status(path, self.runner)
        return WorktreeStatus(
            path=str(path),
            exists=exists,
            registered=entry is not None,
            branch=entry.branch.removeprefix("refs/heads/") if entry and entry.branch else None,
            head=entry.head if entry else None,
            dirty_paths=dirty,
            prunable=bool(entry and entry.prunable),
        )

    def create_or_reattach(self, job: Job, base_ref: str | None = None) -> Worktree:
        """Create the job's worktree, or verify and reuse the one the job already recorded."""
        branch = branch_for(job.job_id)
        path = self.path_for(job.job_id)
        fsutil.ensure_within(self.worktrees_dir, path)
        recorded = job.worktree

        if recorded is not None:
            if fsutil.normalized(recorded.path) != fsutil.normalized(path) or (
                recorded.branch != branch
            ):
                raise WorktreeError(
                    "the job's recorded worktree does not match its expected "
                    "location; refusing to guess"
                )
            return self._reattach(job, recorded, path, branch)

        if job.state not in _PREPARE_STATES:
            raise WorktreeError(
                f"worktrees are created only after plan approval (job is {job.state.value})"
            )
        self._prepare_parent()
        entry = self._entry_for(path)
        if entry is not None:
            raise WorktreeError(
                f"{path} is already a registered worktree not recorded by this "
                "job; inspect it manually"
            )
        if path.exists() and any(path.iterdir()):
            raise WorktreeError(f"{path} already exists and is not empty; refusing to reuse it")
        if ref_exists(self.repo_root, f"refs/heads/{branch}", self.runner):
            raise WorktreeError(
                f"branch {branch} already exists and is not owned by this job "
                "record; refusing to reuse it"
            )

        base = base_ref or default_base_ref(self.repo_root, job.repo.base_branch, self.runner)
        try:
            base_sha = resolve_commit(self.repo_root, base, self.runner)
        except GitError as exc:
            raise WorktreeError(f"base ref {base!r} does not resolve to a commit") from exc
        # An explicit commit (not the branch name) as start point: no upstream tracking of main.
        git_ok(
            self.repo_root, "worktree", "add", "-b", branch, str(path), base_sha, runner=self.runner
        )
        return Worktree(
            path=str(path),
            branch=branch,
            base_ref=base,
            base_sha=base_sha,
            created_by_forgeflow=True,
        )

    def _reattach(self, job: Job, recorded: Worktree, path: Path, branch: str) -> Worktree:
        if job.state not in _REVISIT_STATES:
            raise WorktreeError(f"cannot revisit the worktree while the job is {job.state.value}")
        entry = self._entry_for(path)
        if entry is not None:
            if entry.branch != f"refs/heads/{branch}":
                raise WorktreeError(
                    f"{path} is checked out on {entry.branch or 'a detached HEAD'}"
                    f", not {branch}; refusing to touch it"
                )
            if entry.prunable or not path.exists():
                raise WorktreeError(
                    f"{path} is registered but missing on disk; git reports it as "
                    "prunable. Decide manually whether to `git worktree prune`."
                )
            return recorded  # present and correct: resume in place, dirty or not
        if path.exists() and any(path.iterdir()):
            raise WorktreeError(
                f"{path} exists but is not a registered worktree; refusing to overwrite it"
            )
        if not recorded.created_by_forgeflow:
            raise WorktreeError(
                f"branch {branch} was not created by ForgeFlow; refusing to attach it"
            )
        if not ref_exists(self.repo_root, f"refs/heads/{branch}", self.runner):
            raise WorktreeError(f"branch {branch} recorded by the job no longer exists")
        elsewhere = self._branch_checked_out_elsewhere(branch, path)
        if elsewhere:
            raise WorktreeError(f"branch {branch} is checked out at {elsewhere}")
        git_ok(self.repo_root, "worktree", "add", str(path), branch, runner=self.runner)
        return recorded

    def remove(self, recorded: Worktree) -> None:
        """Remove a clean worktree ForgeFlow created. Never forces; the branch is kept."""
        path = Path(recorded.path)
        fsutil.ensure_within(self.worktrees_dir, path)
        if not recorded.created_by_forgeflow:
            raise WorktreeError(f"{path} was not created by ForgeFlow; refusing to remove it")
        status = self.inspect(path)
        if not status.registered:
            raise WorktreeError(f"{path} is not a registered worktree; refusing to remove it")
        if status.branch != recorded.branch:
            raise WorktreeError(f"{path} is on {status.branch}, not {recorded.branch}")
        if status.dirty:
            raise WorktreeError(
                f"{path} has {len(status.dirty_paths)} uncommitted change(s); refusing to remove it"
            )
        git_ok(self.repo_root, "worktree", "remove", str(path), runner=self.runner)


def prepare_job_worktree(
    store: JobStore, manager: WorktreeManager, job_id: str, base_ref: str | None = None
) -> Job:
    """Create (or verify) the job's worktree and record it in the job."""
    job = store.load(job_id)
    # normalized() also resolves 8.3 short names (e.g. RUNNER~1) and junctions, not just case.
    if fsutil.normalized(job.repo.root) != fsutil.normalized(manager.repo_root):
        raise ValidationError("the worktree manager belongs to a different repository")
    worktree = manager.create_or_reattach(job, base_ref)

    def record(j: Job) -> None:
        j.worktree = worktree

    # No revision check: nothing else writes `worktree`, and failing here would strand a
    # freshly created worktree that the next attempt must then refuse as unrecorded.
    return store.update(
        job_id,
        record,
        event="worktree-prepared",
        details={"path": worktree.path, "branch": worktree.branch, "base_sha": worktree.base_sha},
    )
