"""Command-line entry point: ``forgeflow`` / ``python -m forgeflow``.

The console script is ``forgeflow`` rather than ``forge`` because ``forge`` is already a widely
installed executable (Foundry); the short ``/forge`` and ``$forge`` names belong to the Claude
Code and Codex skill adapters, which call these commands.

Checkpoint B adds the planning workflow: ``init``, ``skills``, ``idea``, ``design``, ``approve``
and ``handoff``. None of them writes code, prepares a worktree or dispatches a builder.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from forgeflow import __version__, design, discovery, hostskills
from forgeflow.approval import (
    ApprovalContext,
    ApprovalDeclinedError,
    ApprovalUnavailableError,
    approve_plan,
    open_human_console,
)
from forgeflow.config import (
    DEFAULT_READY_STATUS,
    ProjectBinding,
    bind_project,
    config_path,
    load_binding,
)
from forgeflow.environment import EnvironmentReport, inspect_environment, linked_projects
from forgeflow.errors import ForgeFlowError
from forgeflow.github import GitHubClient
from forgeflow.gitrepo import discover_identity, main_worktree_root
from forgeflow.handoff import describe, handoff, issue_content
from forgeflow.jobs import WORKER_STATES, Job, Repo
from forgeflow.proc import hostname, pid_alive, run_command
from forgeflow.store import JobStore, worker_status

NOT_YET = {
    "run": "C",
    "review": "C",
    "rework": "C",
    "resume": "C",
    "learn": "post-v0.1",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forgeflow",
        description="ForgeFlow software factory: environment, job status and plan approval.",
    )
    parser.add_argument("--version", action="version", version=f"forgeflow {__version__}")
    parser.add_argument(
        "--repo", type=Path, default=None, help="repository to inspect (default: current directory)"
    )
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser("doctor", help="inspect tools, repository, GitHub and providers")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument("--offline", action="store_true", help="skip checks that contact GitHub")

    status = sub.add_parser("status", help="show local job state (read-only)")
    status.add_argument("job_id", nargs="?", help="show one job in detail")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument(
        "--events",
        type=int,
        default=10,
        help="number of recent events to show for one job (default 10)",
    )

    init = sub.add_parser("init", help="check the setup and bind this clone to a GitHub Project")
    init.add_argument("--project", type=int, help="number of the GitHub Project to bind")
    init.add_argument("--owner", help="Project owner (default: the repository owner)")
    init.add_argument(
        "--ready-status",
        default=DEFAULT_READY_STATUS,
        help=f"Status option for approved work (default {DEFAULT_READY_STATUS!r})",
    )

    skills = sub.add_parser("skills", help="Product Management / Engineering skills of a host")
    skills.add_argument("--host", choices=hostskills.HOSTS, default="claude-code")
    skills.add_argument("--json", action="store_true", help="machine-readable output")

    idea = sub.add_parser("idea", help="interactive discovery: record outcomes of a discussion")
    idea_sub = idea.add_subparsers(dest="action", required=True)
    new = idea_sub.add_parser("new", help="start discovery of a new idea (a draft job)")
    new.add_argument("title")
    new.add_argument("--idea", default="", help="the idea in the user's words")
    add = idea_sub.add_parser("add", help="record an outcome")
    add.add_argument("job_id")
    add.add_argument("kind", choices=list(discovery.KINDS))
    add.add_argument("text")
    add.add_argument("--status", choices=("proposed", "accepted", "rejected"), default="proposed")
    add.add_argument("--note", default="")
    add.add_argument("--supersedes", help="id of the item this one replaces")
    mark = idea_sub.add_parser("mark", help="accept, reject or reinstate an outcome")
    mark.add_argument("job_id")
    mark.add_argument("item_id")
    mark.add_argument("status", choices=("proposed", "accepted", "rejected"))
    mark.add_argument("--note", default="")
    show = idea_sub.add_parser("show", help="summary to resume discovery from")
    show.add_argument("job_id")
    show.add_argument("--all", action="store_true", help="include rejected and superseded")
    show.add_argument("--json", action="store_true")

    des = sub.add_parser("design", help="the engineering plan of a job")
    des_sub = des.add_subparsers(dest="action", required=True)
    for name, text in (
        ("new", "create the plan from the job's discovery (never overwrites)"),
        ("check", "validate the plan"),
        ("show", "print the plan and its approval state"),
    ):
        des_sub.add_parser(name, help=text).add_argument("job_id")
    submit = des_sub.add_parser("submit", help="pin the plan as a new version for approval")
    submit.add_argument("job_id")
    submit.add_argument("--reason", default="")
    revise = des_sub.add_parser("revise", help="withdraw a submitted or approved plan to edit it")
    revise.add_argument("job_id")
    revise.add_argument("--reason", required=True)

    approve = sub.add_parser(
        "approve", help="Gate A: approve a submitted plan (a human, at an interactive console)"
    )
    approve.add_argument("job_id")
    approve.add_argument("--issue", type=int, help="link this existing issue instead of a new one")
    approve.add_argument(
        "--no-handoff", action="store_true", help="record the approval only; publish later"
    )

    hand = sub.add_parser(
        "handoff", help="publish an approved plan as a GitHub issue + Project item"
    )
    hand.add_argument("job_id")
    hand.add_argument("--issue", type=int, help="link this existing issue instead of a new one")
    hand.add_argument("--dry-run", action="store_true", help="show what would be published")

    for name, checkpoint in NOT_YET.items():
        sub.add_parser(name, help=f"not implemented yet (Checkpoint {checkpoint})")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    start = (args.repo or Path.cwd()).resolve()
    try:
        if args.command == "doctor":
            return _doctor(start, as_json=args.json, network=not args.offline)
        if args.command in (None, "status"):
            job_id = getattr(args, "job_id", None)
            return _status(
                start,
                job_id,
                as_json=getattr(args, "json", False),
                events=getattr(args, "events", 10),
            )
        handler = _HANDLERS.get(args.command)
        if handler is not None:
            _resolve_job_argument(start, args)
            return handler(start, args)
        checkpoint = NOT_YET[args.command]
        print(
            f"forgeflow {args.command}: not implemented yet (planned for Checkpoint "
            f"{checkpoint}). Nothing was done.",
            file=sys.stderr,
        )
        return 2
    except ForgeFlowError as exc:
        print(f"forgeflow: {exc}", file=sys.stderr)
        return 1


# Commands that only read; they resolve references without assigning missing ones.
_READ_ONLY = {("idea", "show"), ("design", "check"), ("design", "show")}


def _resolve_job_argument(start: Path, args: argparse.Namespace) -> None:
    """Turn ``J3`` (or a full id) into the job id. Writing commands first give older jobs
    their short references, so every job can be addressed as J<n>."""
    token = getattr(args, "job_id", None)
    if not token:
        return
    store = _store(start)
    if (args.command, getattr(args, "action", None)) not in _READ_ONLY:
        store.assign_missing_refs()
    args.job_id = store.resolve(token)


# --- doctor -----------------------------------------------------------------------------------


def _doctor(start: Path, *, as_json: bool, network: bool) -> int:
    report = inspect_environment(start, network=network)
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_format_report(report))
    return 0 if report.ok else 1


def _format_report(report: EnvironmentReport) -> str:
    lines = [f"ForgeFlow doctor {__version__}: read-only; nothing was installed or changed.", ""]
    width = max(len(c.name) for c in report.checks)
    for check in report.checks:
        tag = f"[{check.status}]".ljust(9)
        req = "*" if check.required else " "
        lines.append(f"  {tag}{req} {check.name.ljust(width)}  {check.summary}")
    lines += ["", "  * required.  Result: " + ("OK" if report.ok else "required checks failed")]
    return "\n".join(lines)


# --- status -----------------------------------------------------------------------------------


def _lease_state(job: Job) -> str:
    """Describe worker ownership without taking any lock (status must stay read-only)."""
    lease = job.lease
    if lease is None:
        return "interrupted - recovery needed" if job.state in WORKER_STATES else "none"
    if lease.holder_host != hostname():
        return f"held on {lease.holder_host} (pid {lease.holder_pid}); not verifiable here"
    alive = pid_alive(lease.holder_pid)
    who = f"{lease.role} {lease.provider}, pid {lease.holder_pid}"
    if alive:
        return f"{who} (running)"
    if worker_status(lease) in ("running", "unverifiable"):
        return (
            f"{who} (process gone; provider pid {lease.worker_pid} still running - stop it, "
            "then recover)"
        )
    return f"{who} (process gone - recovery needed)"


def _status(start: Path, job_id: str | None, *, as_json: bool, events: int) -> int:
    root = main_worktree_root(start)
    store = JobStore.for_repo(root)
    if job_id:
        job_id = store.resolve(job_id)  # read-only: status never assigns references
        job = store.load(job_id)
        recent = store.events(job_id)[-events:] if events > 0 else []
        if as_json:
            print(
                json.dumps(
                    {"job": job.to_dict(), "lease_state": _lease_state(job), "events": recent},
                    indent=2,
                    sort_keys=True,
                )
            )
        else:
            print(_format_job(job, recent))
        return 0

    jobs, problems = store.list_jobs()
    if as_json:
        print(
            json.dumps(
                {
                    "runtime_dir": str(store.runtime_dir),
                    "jobs": [_summary_row(j) for j in jobs],
                    "unreadable": problems,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if not problems else 1
    if not jobs and not problems:
        print(f"No ForgeFlow jobs in {root}.")
        return 0
    header = ("REF", "JOB", "STATE", "BUILDER", "ISSUE", "BRANCH", "WORKER", "UPDATED")
    rows = [header] + [
        (
            j.ref or "-",
            j.job_id,
            j.state.value,
            j.policy.builder,
            f"#{j.github.issue.number}" if j.github.issue else "-",
            j.worktree.branch if j.worktree else "-",
            _lease_state(j),
            j.updated_at[:19] + "Z",
        )
        for j in jobs
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    for problem in problems:
        print(f"unreadable job record: {problem}", file=sys.stderr)
    return 0 if not problems else 1


def _summary_row(job: Job) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "ref": job.ref,
        "title": job.title,
        "state": job.state.value,
        "builder": job.policy.builder,
        "reviewer": job.policy.reviewer,
        "issue": job.github.issue.url if job.github.issue else None,
        "branch": job.worktree.branch if job.worktree else None,
        "lease_state": _lease_state(job),
        "updated_at": job.updated_at,
    }


def _format_job(job: Job, recent: list[dict[str, object]]) -> str:
    gh = job.github
    approval = job.plan.approval
    lines = [
        f"Job {job.label}: {job.title}",
        f"  state          {job.state.value}"
        + (f" (from {job.interrupted_from.value})" if job.interrupted_from else ""),
        f"  repository     {job.repo.slug or '-'}  base {job.repo.base_branch}",
        f"  policy         builder {job.policy.builder}, reviewer {job.policy.reviewer or '-'}",
        f"  plan           {job.plan.path or '-'}"
        + (f"  v{job.plan.version} sha256 {job.plan.sha256[:12]}" if job.plan.sha256 else ""),
        "  plan approval  "
        + (
            f"v{approval.subject_version} by {approval.approver} at {approval.approved_at} "
            f"via {approval.method}"
            if approval
            else "none"
        ),
        "  handoff        "
        + (
            (
                f"complete {gh.handoff.completed_at}, Status {gh.handoff.status or '-'}"
                if gh.handoff.completed_at
                else f"started {gh.handoff.started_at}, incomplete (run forgeflow handoff)"
            )
            if gh.handoff
            else "-"
        ),
        "  worktree       "
        + (
            f"{job.worktree.path} on {job.worktree.branch} (base {job.worktree.base_sha[:12]})"
            if job.worktree
            else "-"
        ),
        f"  issue          {gh.issue.url if gh.issue else '-'}",
        "  project        "
        + (
            ", ".join(
                f"#{p.project_number} {p.project_title} [{p.status or 'no status'}]"
                for p in gh.project_items
            )
            or gh.project_lookup
        ),
        f"  pull request   {gh.pull_request.url if gh.pull_request else '-'}",
        f"  attempts       build {job.attempts.build}, rework {job.attempts.rework}/"
        f"{job.attempts.max_rework}, resumes {job.attempts.resumes}",
        f"  worker         {_lease_state(job)}",
        f"  evidence       {len(job.evidence)} record(s)"
        + (f", {sum(e.mocked for e in job.evidence)} mocked" if job.evidence else ""),
        f"  created        {job.created_at}",
        f"  updated        {job.updated_at} (revision {job.revision})",
    ]
    if recent:
        lines.append("  recent events")
        for event in recent:
            lines.append(
                f"    {event.get('at', '?')}  {event.get('event', '?')}"
                f"  -> {event.get('state', '?')}"
            )
    return "\n".join(lines)


# --- Checkpoint B: discovery, design, Gate A and handoff ----------------------------------------


def _store(start: Path) -> JobStore:
    return JobStore.for_repo(main_worktree_root(start))


def _init(start: Path, args: argparse.Namespace) -> int:
    report = inspect_environment(start, network=True)
    print(_format_report(report))
    if not report.ok:
        print("\nFix the required checks first. Nothing was written.")
        return 1
    root = main_worktree_root(start)
    repo_check = report.get("repository")
    slug = repo_check.data.get("slug") if repo_check else None
    current = load_binding(root)
    print("")
    if current:
        print(
            f"Bound GitHub Project: #{current.number} {current.title} ({current.url}), "
            f"approved work goes to Status {current.ready_status!r}"
        )
    if args.project is None:
        projects = linked_projects(run_command, slug) if slug else None
        if projects:
            print("GitHub Projects visible for this repository:")
            for p in projects:
                print(f"  #{p['number']}  {p['title']}  {p['url']}")
        print("Run `forgeflow init --project N` to bind one. Nothing was written.")
        return 0
    owner = args.owner or (slug.split("/", 1)[0] if slug else None)
    if not owner:
        print("forgeflow: no GitHub remote; pass --owner", file=sys.stderr)
        return 1
    binding = bind_project(root, GitHubClient(), owner, args.project, args.ready_status)
    print(
        f"Bound GitHub Project #{binding.number} {binding.title} ({binding.url}); approved plans "
        f"will be placed in Status {binding.ready_status!r}.\nWrote {config_path(root)} "
        "(local to this clone, never committed). No Project, field or option was changed."
    )
    return 0


def _skills(start: Path, args: argparse.Namespace) -> int:
    found = hostskills.discover(args.host, repo_root=main_worktree_root(start))
    if args.json:
        print(json.dumps(found.to_dict(), indent=2, sort_keys=True))
    else:
        print(hostskills.render(found))
    return 0


def _idea(start: Path, args: argparse.Namespace) -> int:
    store = _store(start)
    if args.action == "new":
        ident = discover_identity(start)
        repo = Repo(ident.root, ident.slug, ident.remote_url, ident.base_branch)
        job, _ = discovery.start(store, args.title, args.idea, repo)
        ref = job.ref or job.job_id
        print(
            f"Started discovery {job.label}: {job.title}\n"
            "  state draft: discussion only; nothing can be built, approved or published yet\n"
            f'  record an outcome:  forgeflow idea add {ref} <kind> "<text>"\n'
            f"  resume later:       forgeflow idea show {ref}\n"
            f"  when ready:         forgeflow design new {ref}"
        )
        return 0
    if args.action == "add":
        item = discovery.add(
            store,
            args.job_id,
            args.kind,
            args.text,
            status=args.status,
            note=args.note,
            supersedes=args.supersedes,
        )
        replaced = f" (replaces {item.supersedes})" if item.supersedes else ""
        print(f"{item.id} [{item.status}] {item.text}{replaced}")
        return 0
    if args.action == "mark":
        item = discovery.mark(store, args.job_id, args.item_id, args.status, note=args.note)
        print(f"{item.id} [{item.status}] {item.text}")
        return 0
    job = store.load(args.job_id)
    found = discovery.load(store, args.job_id)
    if args.json:
        print(json.dumps({"state": job.state.value, **found.to_dict()}, indent=2, sort_keys=True))
    else:
        print(f"Discovery {job.label}: {job.title} (state {job.state.value})")
        print(discovery.render(found, include_closed=args.all))
    return 0


def _print_check(result: design.PlanCheck) -> None:
    for error in result.errors:
        print(f"  error:   {error}")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    for line in result.summary_lines():
        print(f"  {line}")


def _design(start: Path, args: argparse.Namespace) -> int:
    store = _store(start)
    job_id = args.job_id
    ref = store.load(job_id).ref or job_id
    if args.action == "new":
        path, created = design.scaffold(store, job_id)
        verb = "Created" if created else "Plan already exists (not overwritten):"
        print(f"{verb} {path}")
        print("Edit it with the user, then: forgeflow design check / submit " + ref)
        _print_check(design.check(store, job_id))
        return 0
    if args.action == "check":
        result = design.check(store, job_id)
        print(f"Plan of {ref}: " + ("ready to submit" if result.ok else "not ready"))
        _print_check(result)
        return 0 if result.ok else 1
    if args.action == "show":
        job = store.load(job_id)
        path = design.plan_file(store, job_id)
        text = path.read_text(encoding="utf-8") if path.exists() else "(no plan yet)\n"
        print(text.rstrip("\n"))
        print("-" * 78)
        print(f"{job.label}: state {job.state.value}; {_plan_line(job)}")
        if job.plan.sha256 and path.exists() and store.plan_digest(job) != job.plan.sha256:
            print(f"The file differs from submitted version {job.plan.version}; resubmit it.")
        return 0
    if args.action == "submit":
        job, result = design.submit(store, job_id, reason=args.reason)
        print(f"Submitted plan version {job.plan.version} of {job.label} for approval.")
        print(f"  sha256 {job.plan.sha256}")
        _print_check(result)
        print(
            "\nGate A: the plan is approved only by a human, in their own terminal "
            "(not through an agent):\n"
            f"  py -3.13 -m forgeflow approve {ref}      (Windows)\n"
            f"  python3.13 -m forgeflow approve {ref}    (Linux/macOS)"
        )
        return 0
    job = design.revise(store, job_id, args.reason)
    print(f"{job.label} is {job.state.value}; edit the plan, then submit it again.")
    return 0


def _approve(start: Path, args: argparse.Namespace) -> int:
    store = _store(start)
    job = store.load(args.job_id)
    binding = None if args.no_handoff else load_binding(store.repo_root)
    try:
        open_human_console()  # refuse before contacting GitHub or showing anything
        summary: list[str] = []
        if job.plan.sha256:
            try:
                text = store.plan_snapshot_text(job.job_id, job.plan.sha256, job.plan.version)
                summary = design.check_text(text).summary_lines()
            except ForgeFlowError:
                summary = []
        authorises = (
            ["recording the approval only; nothing is published until `forgeflow handoff`"]
            if args.no_handoff
            else describe(job, binding, GitHubClient(), args.issue)
        )
        job = approve_plan(store, args.job_id, ApprovalContext(summary, authorises))
    except ApprovalUnavailableError as exc:
        print(f"forgeflow: {exc}", file=sys.stderr)
        print(
            "Run it yourself in a terminal window (PowerShell, Windows Terminal, a shell):\n"
            f"  py -3.13 -m forgeflow approve {job.ref or args.job_id}",
            file=sys.stderr,
        )
        return 3
    except ApprovalDeclinedError as exc:
        print(f"Not approved: {exc}")
        return 1
    approval = job.plan.approval
    if approval is None:  # approve_plan returns only an approved job
        raise ForgeFlowError("the approval was not recorded")
    print(
        f"\nApproved plan version {approval.subject_version} of {job.label} "
        f"(sha256 {approval.subject_sha256[:16]}...)."
    )
    if args.no_handoff or binding is None:
        print(f"Publish it when ready: forgeflow handoff {job.ref or job.job_id}")
        return 0
    try:
        return _run_handoff(store, job.job_id, binding, args.issue)
    except ForgeFlowError as exc:
        print(f"forgeflow: handoff failed: {exc}", file=sys.stderr)
        print(
            "The approval is recorded. Retry safely with: forgeflow handoff "
            f"{job.ref or job.job_id}",
            file=sys.stderr,
        )
        return 1


def _handoff(start: Path, args: argparse.Namespace) -> int:
    store = _store(start)
    binding = load_binding(store.repo_root)
    if args.dry_run:
        job = store.verify_plan_approval(args.job_id)
        title, body = issue_content(job, store.approved_plan_text(job), binding)
        print("Dry run: nothing is written.")
        for line in describe(job, binding, GitHubClient(), args.issue):
            print(f"  would: {line}")
        print(f"  title: {title}\n  body:  {len(body)} characters\n" + "-" * 78)
        print(body)
        return 0
    return _run_handoff(store, args.job_id, binding, args.issue)


def _run_handoff(
    store: JobStore, job_id: str, binding: ProjectBinding | None, issue: int | None
) -> int:
    result = handoff(store, job_id, GitHubClient(), binding, link_number=issue)
    for action in result.actions or ["nothing to do: already handed off"]:
        print(f"  {action}")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    print(
        f"Issue {result.issue.url}\n"
        f"Project #{result.item.project_number} {result.item.project_title}: item "
        f"{result.item.item_id}, Status {result.item.status or '(none)'}\n"
        f"Job {result.job.label} stays {result.job.state.value}: no worktree, branch or builder "
        "(Checkpoint C prepares and dispatches the work)."
    )
    return 0


def _plan_line(job: Job) -> str:
    if not job.plan.sha256:
        return f"no submitted plan (versions so far: {job.plan.version})"
    approval = job.plan.approval
    state = (
        f"approved v{approval.subject_version} by {approval.approver} via {approval.method}"
        if approval
        else "awaiting approval"
    )
    return f"plan v{job.plan.version} sha256 {job.plan.sha256[:12]}, {state}"


_HANDLERS = {
    "init": _init,
    "skills": _skills,
    "idea": _idea,
    "design": _design,
    "approve": _approve,
    "handoff": _handoff,
}
