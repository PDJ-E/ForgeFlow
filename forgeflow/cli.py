"""Command-line entry point: ``forgeflow`` / ``python -m forgeflow``.

Checkpoint A exposes read-only commands only. The console script is ``forgeflow`` rather than
``forge`` because ``forge`` is already a widely installed executable (Foundry); the short
``/forge`` and ``$forge`` names belong to the Claude Code and Codex skill adapters.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from forgeflow import __version__
from forgeflow.environment import EnvironmentReport, inspect_environment
from forgeflow.errors import ForgeFlowError
from forgeflow.gitrepo import main_worktree_root
from forgeflow.jobs import WORKER_STATES, Job
from forgeflow.proc import hostname, pid_alive
from forgeflow.store import JobStore, worker_status

NOT_YET = {
    "init": "B",
    "idea": "B",
    "design": "B",
    "approve": "B",
    "run": "C",
    "review": "C",
    "rework": "C",
    "resume": "C",
    "learn": "post-v0.1",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forgeflow",
        description="ForgeFlow software factory - Checkpoint A harness (read-only commands).",
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


# --- doctor -----------------------------------------------------------------------------------


def _doctor(start: Path, *, as_json: bool, network: bool) -> int:
    report = inspect_environment(start, network=network)
    if as_json:
        print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        print(_format_report(report))
    return 0 if report.ok else 1


def _format_report(report: EnvironmentReport) -> str:
    lines = ["ForgeFlow doctor (Checkpoint A): read-only; nothing was installed or changed.", ""]
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
    header = ("JOB", "STATE", "BUILDER", "ISSUE", "BRANCH", "WORKER", "UPDATED")
    rows = [header] + [
        (
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
        f"Job {job.job_id}: {job.title}",
        f"  state          {job.state.value}"
        + (f" (from {job.interrupted_from.value})" if job.interrupted_from else ""),
        f"  repository     {job.repo.slug or '-'}  base {job.repo.base_branch}",
        f"  policy         builder {job.policy.builder}, reviewer {job.policy.reviewer or '-'}",
        f"  plan           {job.plan.path or '-'}"
        + (f"  sha256 {job.plan.sha256[:12]}" if job.plan.sha256 else ""),
        "  plan approval  "
        + (
            f"{approval.approver} at {approval.approved_at} via {approval.method}"
            if approval
            else "none"
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
