"""Guarded builder dispatch: the only path from an approved job to a provider writing code.

Checkpoint A wires this to ``MockProvider`` only; real providers raise until Checkpoint C.
"""

from __future__ import annotations

from forgeflow import fsutil
from forgeflow.errors import ForgeFlowError, ProviderError
from forgeflow.jobs import JobState
from forgeflow.providers import Provider, ProviderResult, ProviderTask
from forgeflow.store import JobStore, LeaseHandle


def run_builder(
    store: JobStore, lease: LeaseHandle, provider: Provider, task: ProviderTask
) -> ProviderResult:
    """Enter BUILDING, run the job's single builder, record evidence, then go to VERIFYING.

    Before anything runs, the job's worktree is re-verified against git (its own registered
    worktree, never the main checkout or another one) and the task must carry exactly the
    approved plan text. The caller keeps the lease afterwards, so verification and review
    continue under the same ownership. A failed build escalates to NEEDS_HUMAN instead of
    retrying blindly.
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
            mocked=False,
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
    if result.status == "succeeded":
        store.transition(job.job_id, JobState.VERIFYING, lease_token=lease.token)
    else:
        store.transition(
            job.job_id,
            JobState.NEEDS_HUMAN,
            lease_token=lease.token,
            reason=f"builder {provider.name} reported {result.status}",
        )
    return result
