"""Deliver a validated, reviewed commit as one draft pull request, then stop at Gate B.

Steps, each safe to repeat after an interruption:

1. **Preconditions** (before anything leaves the machine): the job is reviewing under this
   lease; the current round's checks passed on the worktree's clean HEAD; the review of that
   same commit approved it; no evidence of the round is mocked.
2. **Intent:** ``github.delivery`` records the branch and a trace marker before the first push.
3. **Push** the commit to ``refs/heads/forge/<job-id>`` on ``origin``. Never a force push, never
   the base branch: a rejected push stops for a human.
4. **One PR:** reuse the linked PR, else find one for the branch (GitHub allows one open PR per
   head and base), else create it as a draft. A closed or merged PR for the branch is a human
   matter. A PR a human marked ready is turned back into a draft, because Gate B is the
   human's decision on exactly this commit.
5. **Verify** that GitHub reports the validated commit as the PR head, then enter
   ``awaiting_merge_approval`` through ``JobStore.request_merge_approval``, which checks all of
   it again. ForgeFlow never merges.
"""

from __future__ import annotations

import platform
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from forgeflow import __version__, design, fsutil
from forgeflow.dispatch import latest, passing_checks_for_round
from forgeflow.errors import GitError, GitHubError, InvalidTransitionError
from forgeflow.github import GitHubClient, PullRequestSummary
from forgeflow.gitrepo import git, git_ok, porcelain_status
from forgeflow.jobs import Delivery, Job, JobState, PullRequestLink, utc_now
from forgeflow.store import JobStore, LeaseHandle

PR_BODY_LIMIT = 60_000  # GitHub allows 65,536 characters


@dataclass
class DeliveryResult:
    job: Job
    pull_request: PullRequestSummary
    actions: list[str] = field(default_factory=list)


def marker_for(job: Job) -> str:
    plan = job.plan.approval.subject_sha256 if job.plan.approval else "unknown"
    return f"<!-- forgeflow:trace job={job.job_id} plan_sha256={plan} -->"


def round_mocked_kinds(job: Job) -> list[str]:
    return sorted(
        {e.kind for e in job.evidence if e.build_round == job.attempts.build and e.mocked}
    )


def deliver(
    store: JobStore,
    lease: LeaseHandle,
    client: GitHubClient,
    *,
    sleep: Callable[[float], None] = time.sleep,
    head_wait_seconds: float = 30.0,
) -> DeliveryResult:
    job_id = lease.job_id
    job = store.load(job_id)
    if job.state is not JobState.REVIEWING or job.lease is None or job.lease.token != lease.token:
        raise InvalidTransitionError("delivery runs while reviewing, by the job's lease holder")
    mocked = round_mocked_kinds(job)
    if mocked:
        raise InvalidTransitionError(
            f"this build round includes mocked evidence ({', '.join(mocked)}); a simulated "
            "build is never pushed or delivered"
        )
    checks = passing_checks_for_round(job)
    review = latest(job, "review")
    if (
        review is None
        or review.build_round != job.attempts.build
        or review.commit != checks.commit
        or review.status != "approve"
    ):
        raise InvalidTransitionError("the validated commit has no approving review this round")
    if job.worktree is None or not job.repo.slug or job.github.issue is None:
        raise InvalidTransitionError("the job has no worktree, GitHub repository or issue")
    worktree = store.job_worktree(job)
    if porcelain_status(worktree):
        raise InvalidTransitionError("the worktree has uncommitted changes")
    head = git_ok(worktree, "rev-parse", "HEAD", read_only=True)
    if head != checks.commit:
        raise InvalidTransitionError("the worktree moved after its checks ran")
    branch = job.worktree.branch
    if not branch.startswith("forge/") or branch == job.repo.base_branch:
        raise InvalidTransitionError(f"refusing to deliver from branch {branch!r}")
    slug = job.repo.slug
    actions: list[str] = []

    # 2. intent
    record = job.github.delivery
    if record is None:
        record = Delivery(marker_for(job), branch, utc_now())
        job = store.update(
            job_id, _set_delivery(record), event="delivery-started", details={"branch": branch}
        )

    # 3. push, never forced
    push = git(worktree, "push", "--porcelain", "origin", f"{head}:refs/heads/{branch}")
    if not push.ok:
        raise GitError(
            f"pushing {branch} was refused ({push.describe()}); ForgeFlow never force-pushes. "
            "Inspect the remote branch."
        )
    record = Delivery(record.marker, record.branch, record.started_at, head, record.pr_number)
    job = store.update(
        job_id, _set_delivery(record), event="branch-pushed", details={"commit": head}
    )
    actions.append(f"pushed {head[:12]} to {branch}")

    # 4. exactly one draft PR
    title = design.check_text(store.approved_plan_text(job)).title or job.title
    body = pr_body(store, job, head)
    pr = _existing_pr(client, job, slug, branch)
    if pr is None:
        pr = client.create_draft_pull_request(
            slug, head=branch, base=job.repo.base_branch, title=title, body=body
        )
        actions.append(f"opened draft PR #{pr.number}")
    else:
        pr = client.update_pull_request(slug, pr.number, title=title, body=body)
        actions.append(f"updated PR #{pr.number}")
        if not pr.draft:
            client.convert_to_draft(pr.node_id)
            actions.append(f"turned PR #{pr.number} back into a draft for Gate B")
    record = Delivery(record.marker, record.branch, record.started_at, head, pr.number)

    def link(j: Job) -> None:
        j.github.delivery = record
        j.github.pull_request = PullRequestLink(pr.number, pr.url, True, "OPEN")

    job = store.update(job_id, link, event="pr-linked", details={"pr": pr.url})

    # 5. GitHub must report the validated commit as the head before Gate B
    deadline = time.monotonic() + head_wait_seconds
    while True:
        observed = client.view_pull_request(slug, pr.number)
        if observed.head_sha == head and observed.is_draft:
            break
        if time.monotonic() >= deadline:
            raise GitHubError(
                f"PR #{pr.number} reports head {observed.head_sha[:12]} "
                f"(draft: {observed.is_draft}), not {head[:12]}; run `forgeflow resume` to retry"
            )
        sleep(2.0)
    job = store.request_merge_approval(job_id, lease.token, client)
    actions.append(f"Gate B: PR #{pr.number} awaits the owner's merge decision")
    return DeliveryResult(job, pr, actions)


def _set_delivery(record: Delivery) -> Callable[[Job], None]:
    def apply(job: Job) -> None:
        job.github.delivery = record

    return apply


def _existing_pr(
    client: GitHubClient, job: Job, slug: str, branch: str
) -> PullRequestSummary | None:
    candidates = client.pull_requests_for_branch(slug, branch)
    linked = job.github.pull_request
    if linked is not None:
        match = [p for p in candidates if p.number == linked.number]
        if not match:
            raise GitHubError(f"the linked PR #{linked.number} is not a PR from {branch}")
        candidates = match
    open_prs = [p for p in candidates if p.state == "open"]
    if len(open_prs) > 1:
        raise GitHubError(f"several open PRs come from {branch}; close the extra ones")
    if open_prs:
        pr = open_prs[0]
        if pr.base_ref != job.repo.base_branch:
            raise GitHubError(f"PR #{pr.number} targets {pr.base_ref}, not {job.repo.base_branch}")
        return pr
    if candidates:
        pr = candidates[0]
        what = "merged" if pr.merged else "closed"
        raise GitHubError(
            f"PR #{pr.number} from {branch} is {what}; reopen it or decide the job by hand. "
            "ForgeFlow does not open a second PR for one job"
        )
    return None


# --- the PR description ----------------------------------------------------------------------


def _round_lines(job: Job) -> list[str]:
    lines = []
    for n in range(1, job.attempts.build + 1):
        kinds = [e for e in job.evidence if e.build_round == n]
        checks = [e for e in kinds if e.kind == "checks"]
        reviews = [e for e in kinds if e.kind == "review"]
        parts = []
        if checks:
            parts.append(f"checks {checks[-1].status}")
        if reviews:
            parts.append(f"review: {reviews[-1].status}")
        if any(e.kind == "human-feedback" for e in kinds):
            parts.append("human rework request")
        lines.append(f"- Round {n}: " + (", ".join(parts) or "build only"))
    return lines


def _payload(store: JobStore, job: Job, kind: str) -> dict[str, Any]:
    item = latest(job, kind)
    if item is None:
        return {}
    data = fsutil.read_json(store.job_dir(job.job_id) / item.path)
    payload = data.get("payload") if isinstance(data, dict) else None
    return payload if isinstance(payload, dict) else {}


def _private(text: str, job: Job) -> str:
    """Remove local paths before anything is published."""
    for path in filter(None, (job.worktree.path if job.worktree else None, job.repo.root)):
        text = text.replace(path, "<worktree>").replace(path.replace("\\", "/"), "<worktree>")
    return text


def pr_body(store: JobStore, job: Job, head: str) -> str:
    approval = job.plan.approval
    issue = job.github.issue
    checks = _payload(store, job, "checks")
    review = _payload(store, job, "review")
    build = _payload(store, job, "provider-result")
    commit = _payload(store, job, "build-commit")
    base = job.worktree.base_sha if job.worktree else "?"
    rows = [
        f"| {c.get('name', ' '.join(c.get('argv', [])))} | "
        f"{'passed' if c.get('ok') else 'failed'} | {c.get('returncode')} | "
        f"{c.get('seconds', '-')} s |"
        for c in checks.get("commands", [])
    ]
    findings = review.get("findings") or []
    minor = [f for f in findings if str(f).startswith("[minor]")]
    notes = list(build.get("notes") or []) + list(review.get("notes") or [])
    limitations = [f"Reviewer suggestion not addressed: {f}" for f in minor] + [
        f"Provider note: {n}" for n in notes
    ]
    if "codex" in (job.policy.builder, job.policy.reviewer):
        limitations.append(
            "Codex ran with the user's own MCP servers and plugins; ForgeFlow cannot disable "
            "them per run."
        )
    limitations.append(
        f"Checks ran locally on {platform.system()} {platform.release()} with ForgeFlow "
        f"{__version__}; this repository's CI runs separately on this PR."
    )
    sections = [
        f"Implements #{issue.number if issue else '?'}: the plan approved at Gate A (version "
        f"{approval.subject_version if approval else '?'}, sha256 "
        f"`{approval.subject_sha256 if approval else '?'}`).",
        "",
        f"Closes #{issue.number}" if issue else "",
        "",
        "> **Draft opened by ForgeFlow.** Gate B: merging is the owner's decision. ForgeFlow "
        "never merges, and an approving review does not authorise a merge or a deployment.",
        "",
        "## Summary of changes",
        "",
        _private(str(build.get("summary") or "(the builder gave no summary)"), job).strip(),
        "",
        "```",
        _private(str(commit.get("diffstat") or ""), job).strip("\n") or "(no diffstat)",
        "```",
        "",
        f"## Validation (commit `{head}`)",
        "",
        "Real evidence: ForgeFlow ran these repository-configured commands itself on this "
        "exact commit, in the job's isolated worktree.",
        "",
        "| Check | Result | Exit code | Time |",
        "| --- | --- | --- | --- |",
        *rows,
        "",
        f"## Review ({job.policy.reviewer}, read-only snapshot of `{head[:12]}`)",
        "",
        f"Verdict: **{review.get('verdict', '?')}**. "
        + _private(str(review.get("summary") or ""), job).strip(),
        "",
        *([_private(f"- {f}", job) for f in findings] or ["- No findings."]),
        "",
        "Rounds (bounded rework, at most "
        f"{job.attempts.max_rework} automatic rounds before a human is asked):",
        "",
        *_round_lines(job),
        "",
        "## Known limitations",
        "",
        *[_private(f"- {line}", job) for line in limitations],
        "",
        "## Traceability",
        "",
        f"- Job: `{job.job_id}` ({job.ref or '-'}), builder `{job.policy.builder}`, reviewer "
        f"`{job.policy.reviewer}`",
        f"- Branch: `{job.worktree.branch if job.worktree else '?'}` from base `{base[:12]}`",
        f"- Validated and reviewed commit: `{head}`",
        "",
        f"{marker_for(job)}\n<!-- forgeflow:commit {head} -->",
    ]
    body = "\n".join(sections)
    if len(body) > PR_BODY_LIMIT:
        body = body[: PR_BODY_LIMIT - 200] + f"\n\n(truncated)\n\n{marker_for(job)}\n"
    return body
