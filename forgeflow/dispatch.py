"""Guarded builder and reviewer dispatch: the only paths from an approved job to a provider.

The builder writes files in the job's own worktree; ForgeFlow then asks git (not the provider)
what changed, refuses changes outside the task's allowed paths or inside protected ones, and
commits the rest itself. The reviewer never sees that worktree: it runs in a disposable
snapshot of exactly the commit whose checks passed (``git archive``), so even a reviewer that
ignored its read-only instructions could not change the build.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shutil
import stat
import tarfile
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from forgeflow import fsutil
from forgeflow.errors import ForgeFlowError, GitError, ProviderError
from forgeflow.gitrepo import git, git_ok, porcelain_status
from forgeflow.jobs import Evidence, Job, JobState
from forgeflow.providers import Provider, ProviderResult, ProviderTask
from forgeflow.store import JobStore, LeaseHandle

MAX_REVIEW_DIFF = 150_000  # characters of diff in the reviewer's prompt; the snapshot has all


def run_builder(
    store: JobStore,
    lease: LeaseHandle,
    provider: Provider,
    task: ProviderTask,
    *,
    commit_message: str | None = None,
) -> ProviderResult:
    """Enter BUILDING, run the job's single builder, record evidence, then go to VERIFYING.

    Before anything runs, the job's worktree is re-verified against git (its own registered
    worktree, never the main checkout or another one) and the task must carry exactly the
    approved plan text. The caller keeps the lease afterwards, so verification and review
    continue under the same ownership. A failed build escalates to NEEDS_HUMAN instead of
    retrying blindly.

    With ``commit_message`` ForgeFlow also checks the builder's changes against the task's
    paths and commits them (see ``commit_build``); out-of-scope changes stay in the worktree,
    uncommitted and untouched, and the job goes to NEEDS_HUMAN.
    """
    task.validate()
    job = store.load(lease.job_id)
    if task.role != "builder":
        raise ProviderError("run_builder only dispatches builder tasks")
    if task.job_id != job.job_id:
        raise ProviderError("the task belongs to a different job")
    if provider.name != job.policy.builder:
        raise ProviderError(
            f"provider {provider.name!r} is not this job's builder {job.policy.builder!r}"
        )
    try:
        worktree = store.job_worktree(job)
        approved_plan = store.approved_plan_text(job)
    except ForgeFlowError as exc:
        raise ProviderError(f"refusing to dispatch: {exc}") from exc
    if fsutil.normalized(task.worktree_path) != fsutil.normalized(worktree):
        raise ProviderError("the task does not target the job's worktree")
    approval = job.plan.approval  # approved_plan_text() verified it exists
    if (
        approval is None
        or task.plan_sha256 != approval.subject_sha256
        or task.plan_text != approved_plan
    ):
        raise ProviderError("the task does not carry the approved plan")

    # Gate A, rework bounds, the slot rule and lease ownership are enforced by this transition.
    store.transition(job.job_id, JobState.BUILDING, lease_token=lease.token)
    try:
        result = provider.execute(task, lease)
    except Exception as exc:
        store.record_evidence(
            job.job_id,
            "provider-error",
            "failed",
            {"provider": provider.name, "error": str(exc)},
            mocked=not getattr(provider, "real", False),
        )
        store.transition(
            job.job_id,
            JobState.NEEDS_HUMAN,
            lease_token=lease.token,
            reason=f"builder {provider.name} raised: {exc}",
        )
        raise
    store.record_evidence(
        job.job_id, "provider-result", result.status, result.to_dict(), mocked=result.mocked
    )
    if result.status != "succeeded":
        detail = f": {result.summary[:300]}" if result.summary else ""
        store.transition(
            job.job_id,
            JobState.NEEDS_HUMAN,
            lease_token=lease.token,
            reason=f"builder {provider.name} reported {result.status}{detail}",
        )
        return result
    if commit_message is not None:
        problem = commit_build(store, task, worktree, commit_message, mocked=result.mocked)
        if problem is not None:
            store.transition(
                job.job_id, JobState.NEEDS_HUMAN, lease_token=lease.token, reason=problem
            )
            return result
    store.transition(job.job_id, JobState.VERIFYING, lease_token=lease.token)
    return result


def changed_paths(worktree: Path) -> list[str]:
    """Every path git reports as changed, added, deleted or renamed (both names), untracked
    files included. Read with ``-z``, so unusual file names arrive exactly as written."""
    result = git(
        worktree, "status", "--porcelain=v1", "-z", "--untracked-files=all", read_only=True
    )
    if not result.ok:
        raise GitError(result.describe())
    entries = result.stdout.split("\0")
    paths: list[str] = []
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in status or "C" in status:  # the source name follows as its own entry
            paths.append(entries[i])
            i += 1
    return paths


def commit_build(
    store: JobStore,
    task: ProviderTask,
    worktree: Path,
    message: str,
    *,
    mocked: bool,
) -> str | None:
    """Commit the builder's changes, or return why they need a human. Never discards work.

    The changes are judged by git's own report. A path outside the allowed patterns, inside a
    protected one, or anything git cannot stage is left exactly where it is. A round with no
    change at all is fine when earlier rounds already committed work (the builder found
    nothing more to do); with no work on the branch at all it is a failed build.
    """
    paths = changed_paths(worktree)
    outside = [p for p in paths if not task.permits_path(p)]
    protected = [p for p in paths if task.permits_path(p) and task.is_protected(p)]
    if outside or protected:
        store.record_evidence(
            task.job_id,
            "scope-violation",
            "failed",
            {"outside_allowed": outside[:50], "protected": protected[:50], "changed": paths[:200]},
            mocked=mocked,
        )
        shown = ", ".join((outside + protected)[:5])
        return (
            f"the builder changed {len(outside) + len(protected)} path(s) it may not change "
            f"({shown}); nothing was committed or reverted, inspect the worktree"
        )
    head = git_ok(worktree, "rev-parse", "HEAD", read_only=True)
    if not paths:
        if head == task.base_sha:
            return "the builder made no changes; nothing to verify"
        store.record_evidence(
            task.job_id,
            "build-commit",
            "unchanged",
            {"commit": head, "files": []},
            mocked=mocked,
            commit=head,
        )
        return None
    add = git(worktree, "add", "-A", "--", ".")
    commit = git(worktree, "commit", "-q", "-m", message) if add.ok else add
    if not commit.ok:
        return f"committing the build failed ({commit.describe()}); the changes are uncommitted"
    sha = git_ok(worktree, "rev-parse", "HEAD", read_only=True)
    stat_text = git_ok(worktree, "diff", "--stat", f"{task.base_sha}..{sha}", read_only=True)
    store.record_evidence(
        task.job_id,
        "build-commit",
        "committed",
        {"commit": sha, "parent": head, "files": paths[:200], "diffstat": stat_text[-4000:]},
        mocked=mocked,
        commit=sha,
    )
    return None


def latest(job: Job, kind: str) -> Evidence | None:
    found = [e for e in job.evidence if e.kind == kind]
    return found[-1] if found else None


def passing_checks_for_round(job: Job) -> Evidence:
    """The current round's latest checks evidence if it passed and was really run."""
    checks = latest(job, "checks")
    if (
        checks is None
        or checks.build_round != job.attempts.build
        or checks.status != "passed"
        or checks.mocked
        or checks.commit is None
    ):
        raise ProviderError("the current build round has no passing deterministic checks")
    return checks


def run_reviewer(
    store: JobStore,
    lease: LeaseHandle,
    provider: Provider,
    task: ProviderTask,
    *,
    checks_summary: str,
) -> tuple[ProviderResult, Evidence]:
    """Review the validated commit in a disposable snapshot and record the review evidence.

    Only in REVIEWING, by the lease holder, on the commit whose checks passed in this round.
    The reviewer's verdict is recorded as given (with ``mocked`` from the provider); it never
    changes the job state, which is the caller's decision.
    """
    job = store.load(lease.job_id)
    if job.state is not JobState.REVIEWING:
        raise ProviderError(f"reviews run while reviewing, not in {job.state.value}")
    if job.lease is None or job.lease.token != lease.token or not lease.active:
        raise ProviderError("reviewing requires the caller to hold the job lease")
    if provider.name != job.policy.reviewer:
        raise ProviderError(
            f"provider {provider.name!r} is not this job's reviewer {job.policy.reviewer!r}"
        )
    worktree = store.job_worktree(job)
    checks = passing_checks_for_round(job)
    head = git_ok(worktree, "rev-parse", "HEAD", read_only=True)
    if head != checks.commit or porcelain_status(worktree):
        raise ProviderError("the worktree moved after its checks ran; run the checks again")
    diff = git_ok(worktree, "diff", "--stat", "--patch", f"{task.base_sha}..{head}", read_only=True)
    truncated = len(diff) > MAX_REVIEW_DIFF
    shown = diff[:MAX_REVIEW_DIFF] + (
        "\n... diff truncated; read the files in the snapshot ..." if truncated else ""
    )
    result, verified = review_in_snapshot(
        provider, lease, task, worktree, head, diff=shown, checks_summary=checks_summary
    )
    if git_ok(worktree, "rev-parse", "HEAD", read_only=True) != head or porcelain_status(worktree):
        raise ProviderError("the build worktree changed during the review; refusing its result")
    payload: dict[str, Any] = {
        **result.to_dict(),
        "commit": head,
        "diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        "diff_truncated": truncated,
        "snapshot": "git archive of the commit in a temporary directory, removed afterwards",
        # ForgeFlow's own finding, written last so nothing the provider returned can supply it.
        "review_access": {
            "version": REVIEW_ACCESS_VERSION,
            "verified": verified,
            "method": "snapshot-token",
        },
    }
    status = result.verdict if result.status == "succeeded" and result.verdict else result.status
    evidence = store.record_evidence(
        job.job_id, "review", status, payload, mocked=result.mocked, commit=head
    )
    return result, evidence


_SNAPSHOT_PREFIX = "forgeflow-review-"
# Written into every review snapshot with a fresh random value that is never in the prompt. The
# reviewer must return it: proof that it really read files in the snapshot, not just the diff.
REVIEW_TOKEN_FILE = ".forgeflow-review-token"  # noqa: S105 - a file name, not a secret
# Version of the "review_access" record ForgeFlow writes into review evidence. Evidence without
# it (recorded before this check existed) or with another version counts as unverified.
REVIEW_ACCESS_VERSION = 1


def review_access_verified(store: JobStore, job: Job, evidence: Evidence) -> bool:
    """True only if ForgeFlow itself recorded verified snapshot access for this review."""
    data = _evidence_payload(store, job, evidence)
    record = data.get("review_access")
    return (
        isinstance(record, dict)
        and record.get("version") == REVIEW_ACCESS_VERSION
        and record.get("verified") is True
    )


def verified_approval(store: JobStore, job: Job, commit: str | None) -> Evidence | None:
    """The approval this round may rely on, or None.

    The one rule for consuming a review, shared by resume (skip a review already done) and
    delivery: the latest review of the current round, of exactly ``commit``, approving, not
    mocked unless the caller accepts that elsewhere, and with ForgeFlow's own record that the
    reviewer proved it read the snapshot. Anything else is unverified and needs a fresh review.
    """
    review = latest(job, "review")
    if (
        review is None
        or commit is None
        or review.build_round != job.attempts.build
        or review.commit != commit
        or review.status != "approve"
        or not review_access_verified(store, job, review)
    ):
        return None
    return review


def _evidence_payload(store: JobStore, job: Job, evidence: Evidence) -> dict[str, Any]:
    try:
        data = fsutil.read_json(store.job_dir(job.job_id) / evidence.path)
    except (OSError, ValueError):
        return {}
    payload = data.get("payload") if isinstance(data, dict) else None
    return payload if isinstance(payload, dict) else {}


def access_proven(result: ProviderResult, token: str) -> bool:
    """The reviewer returned the snapshot's token and said its inspection was complete."""
    if result.status != "succeeded" or result.review is None:
        return False
    returned = str(result.review.get("snapshot_token", "")).strip()
    return returned == token and result.review.get("inspection_complete") is True


def review_in_snapshot(
    provider: Provider,
    lease: LeaseHandle,
    task: ProviderTask,
    worktree: Path,
    head: str,
    *,
    diff: str,
    checks_summary: str,
) -> tuple[ProviderResult, bool]:
    """Run the reviewer on a fresh snapshot of ``head``; return its result and whether
    ForgeFlow verified that it read the snapshot.

    A review whose ``snapshot_token`` is not the one written into the snapshot, or that says
    ``inspection_complete: false``, is returned as ``blocked`` with no verdict: an
    infrastructure problem for a human, never an approval and never a reason to rework.
    """
    remove_stale_snapshots(task.job_id)  # left behind by a review whose process was killed
    snapshot = make_snapshot(worktree, head, task.job_id)
    try:
        token_file = snapshot / REVIEW_TOKEN_FILE
        if token_file.exists():
            raise ProviderError(f"the commit itself contains {REVIEW_TOKEN_FILE}; cannot review it")
        token = secrets.token_hex(16)
        token_file.write_text(token + "\n", encoding="utf-8")
        review_task = replace(
            task,
            role="reviewer",
            worktree_path=str(snapshot),
            allowed_operations=("read",),
            review_commit=head,
            review_diff=diff,
            review_checks=checks_summary,
            review_token_file=REVIEW_TOKEN_FILE,
        )
        review_task.validate()
        result = provider.execute(review_task, lease)
    finally:
        _remove_tree(snapshot)
    return verify_review_access(result, token), access_proven(result, token)


def verify_review_access(result: ProviderResult, token: str) -> ProviderResult:
    if result.status != "succeeded" or result.review is None:
        return result
    if access_proven(result, token):
        return result
    returned = str(result.review.get("snapshot_token", "")).strip()
    why = (
        "it did not return the snapshot token, so it did not read the snapshot"
        if returned != token
        else "it reported that it could not inspect the files it needed"
    )
    return replace(
        result,
        status="blocked",
        verdict=None,
        summary=f"review incomplete: {why}. Its answer is not used. Reviewer said: "
        f"{result.summary[:300]}",
        notes=(*result.notes, f"review-access: {why}"),
    )


def remove_stale_snapshots(job_id: str) -> None:
    """Remove snapshots of ``job_id`` only (its id is in their names); a review holds the lease,
    so none of them can be in use."""
    for leftover in Path(tempfile.gettempdir()).glob(f"{_SNAPSHOT_PREFIX}{job_id}-*"):
        if leftover.is_dir():
            _remove_tree(leftover)


def make_snapshot(worktree: Path, commit: str, job_id: str = "") -> Path:
    """A fresh directory holding exactly the files of ``commit`` (no .git, no worktree link).

    Created with a plain ``mkdir``, not ``tempfile.mkdtemp``: mkdtemp's mode 0o700 becomes an
    owner-only ACL on Windows (Python 3.13+), which Codex's restricted sandbox identity cannot
    read ("Access is denied"; seen in the Checkpoint C live run).
    """
    label = f"{job_id}-" if job_id else ""
    for _ in range(5):
        target = Path(tempfile.gettempdir()) / f"{_SNAPSHOT_PREFIX}{label}{secrets.token_hex(6)}"
        try:
            target.mkdir()
            break
        except FileExistsError:
            continue
    else:  # pragma: no cover - 2**48 names
        raise ProviderError("could not create a review snapshot directory")
    archive = target.with_name(target.name + ".tar")
    try:
        git_ok(worktree, "archive", "--format=tar", "-o", str(archive), commit, read_only=True)
        with tarfile.open(archive) as tar:
            tar.extractall(target, filter="data")
    except BaseException:
        _remove_tree(target)
        raise
    finally:
        archive.unlink(missing_ok=True)
    return target


def _remove_tree(path: Path) -> None:
    def writable(func: Callable[..., object], name: str, _exc: BaseException) -> None:
        os.chmod(name, stat.S_IWRITE)
        func(name)

    shutil.rmtree(path, onexc=writable)
