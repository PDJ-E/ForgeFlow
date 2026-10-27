"""Hand an approved plan off to GitHub: one issue, on the configured Project, in its Ready status.

GitHub becomes the source of truth for the approved work (ADR 0001); the job keeps references.
What is published is exactly the approved plan (from its snapshot) plus a short trace footer
and a hidden marker naming the job and plan hash. Discovery notes are never published.

Idempotent and resumable. Each step is recorded in the job before the next starts, and every
step first looks for what an interrupted earlier run may already have done:

1. intent: ``github.handoff`` records the plan hash and the marker before anything is written;
2. issue: reuse the linked issue, else find one carrying the marker among the repository's
   newest issues (a list query, not the eventually consistent search API), else create it;
3. Project item: reuse the issue's item on the configured Project, else add it;
4. Status: set the configured option only on an item that has no status yet, so a status a
   human set on the board is never overridden; then read the item back and record it.

Gate A is re-verified before every GitHub write, and a per-job OS lock keeps two handoffs of
one job from racing. Handoff never prepares a worktree, assigns a builder or leaves
PLAN_APPROVED: ``forgeflow.pipeline`` does that.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from forgeflow import design, fsutil
from forgeflow.config import ProjectBinding, check_project
from forgeflow.errors import (
    ActiveJobError,
    ForgeFlowError,
    GitHubError,
    InvalidTransitionError,
    ValidationError,
)
from forgeflow.github import GitHubClient, ProjectInfo, RepositoryInfo
from forgeflow.jobs import Handoff, IssueLink, Job, JobState, ProjectItemLink, utc_now
from forgeflow.store import JobStore


@dataclass(frozen=True)
class HandoffTarget:
    repo: RepositoryInfo
    project: ProjectInfo
    status_name: str
    status_option_id: str


@dataclass
class HandoffResult:
    job: Job
    issue: IssueLink
    item: ProjectItemLink
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def marker_for(job_id: str, plan_sha256: str) -> str:
    return f"<!-- forgeflow:trace job={job_id} plan_sha256={plan_sha256} -->"


def issue_content(
    job: Job, plan_text: str, project: ProjectBinding | None = None
) -> tuple[str, str]:
    """The issue title and body for an approved plan: its text verbatim, plus a trace.

    The footer names the Project by a Markdown link to its URL, never as "Project #N": GitHub
    turns "#N" into a link to issue or pull request N of the repository.
    """
    approval = job.plan.approval
    if approval is None:
        raise InvalidTransitionError("Gate A: the plan has not been approved")
    title = design.check_text(plan_text).title or job.title
    board = f" Tracked on the GitHub Project [{project.title}]({project.url})." if project else ""
    footer = (
        "\n\n---\n"
        f"<sub>Approved plan handed off by ForgeFlow: job `{job.job_id}`, plan version "
        f"{approval.subject_version or job.plan.version}, sha256 `{approval.subject_sha256}`, "
        f"approved {approval.approved_at} at an interactive console.{board} Implementation "
        "has not started and no builder has been dispatched.</sub>\n\n"
        f"{marker_for(job.job_id, approval.subject_sha256)}\n"
    )
    return title, plan_text.rstrip() + footer


def resolve_target(client: GitHubClient, slug: str, binding: ProjectBinding) -> HandoffTarget:
    """Read the repository and Project now and check they can receive the handoff."""
    repo = client.repository_info(slug)
    if not repo.has_issues:
        raise GitHubError(f"issues are disabled in {slug}")
    project = client.project_info(binding.owner, binding.number)
    if project.id != binding.project_id:
        raise GitHubError(
            f"GitHub Project #{binding.number} of {binding.owner} is no longer the Project "
            f"bound by `forgeflow init` ({binding.title}); run init again"
        )
    option_id = check_project(project, binding.ready_status)
    return HandoffTarget(repo, project, binding.ready_status, option_id)


def describe(
    job: Job, binding: ProjectBinding | None, client: GitHubClient | None, link_number: int | None
) -> list[str]:
    """What approving this plan will publish, for the Gate A screen. Reads GitHub only."""
    if binding is None or not job.repo.slug:
        return [
            "recording the approval only: no GitHub Project is configured, so nothing is "
            "published until `forgeflow init --project N` and `forgeflow handoff` run"
        ]
    if job.github.issue is not None:
        return [
            f"nothing new: the plan is already on issue #{job.github.issue.number} "
            f"({job.github.issue.url}); handoff only re-checks it and finishes its Project item"
        ]
    where = f"GitHub issue #{link_number} (as a comment)" if link_number else "a NEW GitHub issue"
    target_line = f"in {job.repo.slug}"
    status = f"Status '{binding.ready_status}'"
    project = f"the GitHub Project '{binding.title}' ({binding.url})"
    if client is not None:
        try:
            target = resolve_target(client, job.repo.slug, binding)
            target_line = f"in {target.repo.slug} ({target.repo.visibility} repository)"
        except ForgeFlowError as exc:
            target_line += f" (could not be verified now: {exc}; handoff re-checks it)"
    return [
        f"publishing the plan text above verbatim to {where} {target_line}",
        f"adding that issue to {project} with {status} (an existing status is left as is)",
    ]


def handoff(
    store: JobStore,
    job_id: str,
    client: GitHubClient,
    binding: ProjectBinding | None,
    *,
    link_number: int | None = None,
) -> HandoffResult:
    lock = fsutil.FileLock(store.job_dir(job_id) / "handoff.lock", timeout=0)
    if not lock.try_acquire():
        raise ActiveJobError(f"another handoff of job {job_id} is running")
    try:
        return _handoff(store, job_id, client, binding, link_number)
    finally:
        lock.release()


def _handoff(
    store: JobStore,
    job_id: str,
    client: GitHubClient,
    binding: ProjectBinding | None,
    link_number: int | None,
) -> HandoffResult:
    job = store.load(job_id)
    if job.state is not JobState.PLAN_APPROVED:
        raise InvalidTransitionError(
            f"job {job_id} is {job.state.value}; handoff publishes an approved plan "
            "(state plan_approved) and nothing else"
        )
    if binding is None:
        raise ValidationError(
            "no GitHub Project is configured for this clone; run `forgeflow init --project N`"
        )
    slug = job.repo.slug
    if not slug:
        raise ValidationError("the job's repository has no GitHub remote")
    job = store.verify_plan_approval(job_id)
    approval = job.plan.approval
    if approval is None:  # verify_plan_approval guarantees one
        raise InvalidTransitionError("Gate A: the plan has not been approved")
    plan_text = store.approved_plan_text(job)
    title, body = issue_content(job, plan_text, binding)
    marker = marker_for(job_id, approval.subject_sha256)
    target = resolve_target(client, slug, binding)
    result_actions: list[str] = []
    warnings: list[str] = []

    def gate() -> None:  # immediately before every write
        store.verify_plan_approval(job_id)

    # 1. intent
    record = job.github.handoff
    if record is not None and record.plan_sha256 != approval.subject_sha256:
        raise InvalidTransitionError("an earlier handoff of this job published another plan")
    if record is None:
        mode = "link" if link_number else "create"
        intent = Handoff(approval.subject_sha256, mode, marker, utc_now())
        job = store.update(
            job_id,
            _set_handoff(intent),
            event="handoff-started",
            details={"mode": mode, "plan_sha256": approval.subject_sha256},
        )
        record = intent
    linked = job.github.issue
    if link_number and linked is not None and linked.number != link_number:
        raise ValidationError(f"job {job_id} is already linked to issue #{linked.number}")
    if link_number and linked is None and record.mode != "link":
        record = Handoff(record.plan_sha256, "link", record.marker, record.started_at)
        job = store.update(job_id, _set_handoff(record), event="handoff-mode", details={})

    # 2. issue
    if linked is None:
        if record.mode == "link":
            number = link_number
            if number is None:
                raise ValidationError("this handoff links an existing issue; pass --issue N")
            issue = client.view_issue(slug, number)
            if issue.state != "OPEN":
                raise ValidationError(f"{slug}#{number} is {issue.state}, not OPEN")
            owner = store.issue_owner(slug, number, exclude=job_id)
            if owner:
                raise ActiveJobError(f"issue {slug}#{number} is already owned by job {owner}")
            if not any(marker in c for c in client.issue_comments(slug, number)):
                gate()
                client.add_issue_comment(slug, number, body)
                result_actions.append(f"commented the approved plan on issue #{number}")
        else:
            found = sorted(
                (i.link for i in client.recent_issues(slug) if marker in i.body),
                key=lambda link: link.number,
            )
            if found:
                issue = found[0]
                result_actions.append(f"found issue #{issue.number} from an interrupted run")
                if len(found) > 1:
                    extra = ", ".join(f"#{i.number}" for i in found[1:])
                    warnings.append(f"other issues carry this plan's marker: {extra}")
            else:
                gate()
                issue = client.create_issue(slug, title, body)
                result_actions.append(f"created issue #{issue.number}")

        def link(j: Job) -> None:
            j.github.issue = issue

        job = store.update_if_issue_free(
            job_id, slug, issue.number, link, details={"issue": issue.url, "mode": record.mode}
        )
    else:
        issue = client.view_issue(slug, linked.number)
        if issue.state != "OPEN":
            raise GitHubError(
                f"linked issue #{issue.number} is {issue.state}; reopen it or cancel the job"
            )

    # 3. Project item
    project_url = target.project.url
    mine = [
        i for i in client.issue_project_items(slug, issue.number) if i.project_url == project_url
    ]
    if mine:
        item_id, status = mine[0].item_id, mine[0].status
    else:
        gate()
        item_id, status = client.add_project_item(target.project.id, issue.node_id), None
        result_actions.append(f"added issue #{issue.number} to Project #{target.project.number}")

    # 4. Status, never overriding one already set on the board
    field_id = target.project.status_field_id  # resolve_target checked that it exists
    if status is None and field_id is not None:
        gate()
        client.set_project_status(target.project.id, item_id, field_id, target.status_option_id)
        result_actions.append(f"set Status to {target.status_name!r}")
    elif status is not None and status.casefold() != target.status_name.casefold():
        warnings.append(
            f"the Project item already has Status {status!r}; ForgeFlow left it unchanged"
        )

    items = client.issue_project_items(slug, issue.number)
    item = next((i for i in items if i.item_id == item_id or i.project_url == project_url), None)
    if item is None:
        raise GitHubError(
            f"issue #{issue.number} is not visible on Project #{target.project.number} after "
            "adding it; run handoff again"
        )
    done = Handoff(
        record.plan_sha256,
        record.mode,
        record.marker,
        record.started_at,
        completed_at=utc_now(),
        project_url=project_url,
        status=item.status,
    )

    def complete(j: Job) -> None:
        j.github.project_items = items
        j.github.project_lookup = "ok"
        j.github.handoff = done

    job = store.update(
        job_id,
        complete,
        event="handoff-completed",
        details={
            "issue": issue.url,
            "project_item": item.item_id,
            "status": item.status,
            "actions": result_actions,
        },
    )
    return HandoffResult(job, issue, item, result_actions, warnings)


def _set_handoff(record: Handoff) -> Callable[[Job], None]:
    def apply(job: Job) -> None:
        job.github.handoff = record

    return apply
