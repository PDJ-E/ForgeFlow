"""The GitHub Project Status of a job's issue, derived from the job state in one place.

``STATUS_FOR_STATE`` is the only mapping from the local lifecycle to the board. Nothing else in
ForgeFlow (and neither host adapter) decides a Status. A sync is level-triggered: it reads the
job's current state, derives the Status, reads the item and writes only when they differ, then
reads it back. Running it twice, or after an interruption, converges on the same board, which
is what makes it idempotent and safe to retry (``forgeflow sync``).

* **Done** follows only from ``merged``, which ``JobStore.record_merge`` reaches only when
  GitHub reports the PR merged with the validated commit; never from an agent's claim.
* **Blocked** is ``needs_human``. The item returns to Ready, then Building, when a human
  continues the job.
* ``interrupted`` and ``cancelled`` leave the Status as it is: an interruption is transient,
  and a cancelled job's card is the human's to file.
* Starting work never closes the issue; ForgeFlow never closes issues at all. A merged PR that
  says "Closes #N" closes it, which is GitHub acting on the human's merge.

The owner disabled the Project's own status-changing workflows (ADR 0004), so ForgeFlow is the
single writer of Status on its items. A Status moved by hand is overwritten by the next sync,
and the event log records what it was.
"""

from __future__ import annotations

from dataclasses import dataclass

from forgeflow.config import ProjectBinding
from forgeflow.errors import ForgeFlowError, GitHubError
from forgeflow.github import GitHubClient, ProjectInfo
from forgeflow.jobs import BoardSync, Job, JobState, ProjectItemLink, utc_now
from forgeflow.store import JobStore

S = JobState
READY = "Ready"  # replaced by the binding's ready_status, chosen at `forgeflow init`

STATUS_FOR_STATE: dict[JobState, str | None] = {
    S.DRAFT: None,  # not on the board: unapproved ideas stay local
    S.AWAITING_PLAN_APPROVAL: None,
    S.PLAN_APPROVED: READY,
    S.READY: READY,
    S.BUILDING: "Building",
    S.VERIFYING: "QA",
    S.REVIEWING: "Review",
    S.AWAITING_MERGE_APPROVAL: "Review",  # Gate B: the draft PR waits for the human
    S.INTERRUPTED: None,  # unchanged until the job resumes
    S.NEEDS_HUMAN: "Blocked",
    S.MERGED: "Done",
    S.CANCELLED: None,
}


def status_for(state: JobState, binding: ProjectBinding) -> str | None:
    status = STATUS_FOR_STATE[state]
    return binding.ready_status if status == READY else status


@dataclass(frozen=True)
class SyncResult:
    job: Job
    target: str | None
    before: str | None
    after: str | None
    changed: bool

    def describe(self) -> str:
        if self.target is None:
            return f"Project Status left unchanged ({self.job.state.value})"
        if self.changed:
            return f"Project Status {self.before or '(none)'} -> {self.after}"
        return f"Project Status already {self.after}"


def sync(store: JobStore, job_id: str, client: GitHubClient, binding: ProjectBinding) -> SyncResult:
    """Make the job's Project item show the Status its current state maps to."""
    job = store.load(job_id)
    target = status_for(job.state, binding)
    issue = job.github.issue
    if target is None or issue is None or not job.repo.slug:
        return SyncResult(job, None, None, None, False)
    project = _project(client, binding)
    option_id = project.option_id(target)
    if option_id is None or project.status_field_id is None:
        names = ", ".join(project.status_options) or "none"
        raise GitHubError(
            f"Project #{project.number} has no Status option {target!r} (options: {names}); "
            "add it on the board. ForgeFlow does not create options"
        )
    item = _item(client, job.repo.slug, issue.number, project)
    before = item.status
    changed = (before or "").casefold() != target.casefold()
    if changed:
        client.set_project_status(project.id, item.item_id, project.status_field_id, option_id)
    items = client.issue_project_items(job.repo.slug, issue.number)
    after = next((i.status for i in items if i.item_id == item.item_id), None)
    record = BoardSync(job.state.value, after, utc_now())
    if (after or "").casefold() != target.casefold():
        record = BoardSync(job.state.value, after, record.synced_at, f"read back {after!r}")

    def apply(j: Job) -> None:
        j.github.project_items = items
        j.github.board = record

    job = store.update(
        job_id,
        apply,
        event="board-synced",
        details={"state": job.state.value, "from": before, "to": after, "changed": changed},
    )
    if record.error:
        raise GitHubError(
            f"Project Status of issue #{issue.number} reads {after!r} after setting {target!r}"
        )
    return SyncResult(job, target, before, after, changed)


def _project(client: GitHubClient, binding: ProjectBinding) -> ProjectInfo:
    project = client.project_info(binding.owner, binding.number)
    if project.id != binding.project_id:
        raise GitHubError(
            f"GitHub Project #{binding.number} of {binding.owner} is no longer the Project bound "
            f"by `forgeflow init` ({binding.title}); run init again"
        )
    return project


def _item(client: GitHubClient, slug: str, number: int, project: ProjectInfo) -> ProjectItemLink:
    mine = [i for i in client.issue_project_items(slug, number) if i.project_url == project.url]
    if not mine:
        raise GitHubError(
            f"issue #{number} is not on Project #{project.number}; run `forgeflow handoff`"
        )
    return mine[0]


class BoardListener:
    """Store listener: sync the board after every transition; record, never raise, failures.

    A failed sync (GitHub unreachable, a missing option) leaves the job as it is and records the
    error in ``github.board``; the next transition or ``forgeflow sync`` converges.
    """

    def __init__(self, store: JobStore, client: GitHubClient, binding: ProjectBinding) -> None:
        self.store = store
        self.client = client
        self.binding = binding
        self.last: SyncResult | None = None
        self.errors: list[str] = []

    def __call__(self, job: Job) -> None:
        try:
            self.last = sync(self.store, job.job_id, self.client, self.binding)
        except ForgeFlowError as exc:
            self.errors.append(str(exc))
            failed = BoardSync(job.state.value, None, utc_now(), str(exc)[:500])

            def apply(j: Job) -> None:
                j.github.board = failed

            self.store.update(
                job.job_id, apply, event="board-sync-failed", details={"error": str(exc)[:500]}
            )

    def __repr__(self) -> str:
        return f"BoardListener(Project #{self.binding.number})"
