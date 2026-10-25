"""Engineering design: one reviewable plan document per job, built from its discovery.

The plan is a Markdown file at ``.forge/runtime/jobs/<job-id>/plan.md`` (local, self-ignored).
It is the single document for the job's decision: there is no second spec, task file or ADR
draft generated beside it. The host agent and the human edit it together; ``check`` validates
the few things later stages rely on; ``submit`` pins its exact bytes as a new plan version for
Gate A (``JobStore.submit_plan``); ``revise`` withdraws a submitted or approved-but-unpublished
version so it can change.

Structure, kept deliberately small:

* one ``# Title`` line (it becomes the GitHub issue title);
* required ``##`` sections: Problem and objective, Scope, Acceptance criteria, Verification,
  Tasks; recommended: Requirements, Proposed solution, Alternatives and trade-offs,
  Dependencies and risks;
* under ``## Tasks``, each task is ``### T<n>: <title>`` with at least one ``- [ ]`` criterion.

The approved plan is published verbatim as the GitHub issue body, so it must fit GitHub's
limit and should not contain private notes or local paths; ``check`` warns about the latter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from forgeflow import discovery, fsutil
from forgeflow.errors import InvalidTransitionError, ValidationError
from forgeflow.jobs import Job, JobState
from forgeflow.store import RUNTIME_RELATIVE, JobStore

PLAN_NAME = "plan.md"
# GitHub rejects issue bodies over 65,536 characters; the trace footer needs the rest.
MAX_PLAN_CHARS = 60_000
TODO_MARK = "TODO(forgeflow)"

REQUIRED_SECTIONS = (
    "Problem and objective",
    "Scope",
    "Acceptance criteria",
    "Verification",
    "Tasks",
)
RECOMMENDED_SECTIONS = (
    "Requirements",
    "Proposed solution",
    "Alternatives and trade-offs",
    "Dependencies and risks",
)

_H1 = re.compile(r"^# (?!#)(.+?)\s*$", re.MULTILINE)
_H2 = re.compile(r"^## (?!#)(.+?)\s*$", re.MULTILINE)
_TASK = re.compile(r"^### (T\d+)\s*[:.\-—]\s*(.+?)\s*$", re.MULTILINE)
_H3 = re.compile(r"^### ", re.MULTILINE)
_CRITERION = re.compile(r"^\s*[-*] \[ \] \S", re.MULTILINE)
# "Project #2", "GitHub Project ForgeFlow Development #2": GitHub links "#2" to issue/PR 2.
_PROJECT_NUMBER = re.compile(r"(?i)\bprojects?\b[^\n#`]{0,40}#\d+")
# A POSIX home path counts only where it starts a token, so github.com/users/x/ does not match.
_LOCAL_PATH = re.compile(
    r"(?i:\b[a-z]:\\users\\)|(?<![\w.:/-])(?:/home/[a-z0-9_.-]+/|/Users/[A-Za-z0-9_.-]+/)"
)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    criteria: int


@dataclass
class PlanCheck:
    title: str | None = None
    sections: list[str] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary_lines(self) -> list[str]:
        lines = [f"Tasks        {len(self.tasks)}"]
        lines += [f"  {t.id}: {t.title} ({t.criteria} criteria)" for t in self.tasks]
        return lines


def plan_relpath(job_id: str) -> str:
    return (PurePosixPath(*RUNTIME_RELATIVE.parts) / "jobs" / job_id / PLAN_NAME).as_posix()


def plan_file(store: JobStore, job_id: str) -> Path:
    return store.job_dir(job_id) / PLAN_NAME


def _section_key(heading: str) -> str:
    return re.sub(r"\s+", " ", heading.strip().lower().replace("&", "and"))


def check_text(text: str) -> PlanCheck:
    result = PlanCheck()
    titles = _H1.findall(text)
    if len(titles) != 1:
        result.errors.append("the plan needs exactly one '# Title' line")
    else:
        title = titles[0].strip()
        result.title = title
        if len(title) > 256:
            result.errors.append("the title is longer than GitHub's 256 characters")

    headings = [(m.group(1), m.start(), m.end()) for m in _H2.finditer(text)]
    result.sections = [h for h, _, _ in headings]
    keys = [_section_key(h) for h in result.sections]
    for required in REQUIRED_SECTIONS:
        if not any(k.startswith(_section_key(required)) for k in keys):
            result.errors.append(f"missing section '## {required}'")
    for recommended in RECOMMENDED_SECTIONS:
        if not any(k.startswith(_section_key(recommended)) for k in keys):
            result.warnings.append(f"no '## {recommended}' section (add it if relevant)")

    tasks_body = ""
    for i, (heading, _start, end) in enumerate(headings):
        if _section_key(heading).startswith("tasks"):
            stop = headings[i + 1][1] if i + 1 < len(headings) else len(text)
            tasks_body = text[end:stop]
    starts = [m.start() for m in _H3.finditer(tasks_body)]
    seen: set[str] = set()
    for n, pos in enumerate(starts):
        chunk = tasks_body[pos : starts[n + 1] if n + 1 < len(starts) else len(tasks_body)]
        match = _TASK.match(chunk)
        if not match:
            first = chunk.splitlines()[0]
            result.errors.append(f"task heading {first!r} is not '### T<n>: <title>'")
            continue
        task_id, title = match.group(1), match.group(2)
        if task_id in seen:
            result.errors.append(f"task {task_id} appears twice")
        seen.add(task_id)
        criteria = len(_CRITERION.findall(chunk))
        if not criteria:
            result.errors.append(f"task {task_id} has no '- [ ]' acceptance criterion")
        result.tasks.append(Task(task_id, title, criteria))
    if any(k.startswith("tasks") for k in keys) and not result.tasks:
        result.errors.append("'## Tasks' has no '### T<n>: <title>' task")

    if TODO_MARK in text:
        result.errors.append(f"unresolved {TODO_MARK} placeholders remain")
    if len(text) > MAX_PLAN_CHARS:
        result.errors.append(
            f"the plan is {len(text)} characters; GitHub issue bodies allow about "
            f"{MAX_PLAN_CHARS}. Shorten it or split the work into separate jobs"
        )
    if _LOCAL_PATH.search(text):
        result.warnings.append(
            "the plan contains a local user path; it is published verbatim to GitHub on handoff"
        )
    project_ref = _PROJECT_NUMBER.search(text)
    if project_ref:
        result.warnings.append(
            f"{project_ref.group(0)!r}: on GitHub '#N' links to issue or pull request N of the "
            "repository, not to a Project; name the Project and link its URL instead"
        )
    return result


def _bullets(items: list[str], placeholder: str) -> str:
    if not items:
        return f"- {TODO_MARK}: {placeholder}"
    return "\n".join(f"- {line}" for line in items)


def scaffold_text(job: Job, found: discovery.Discovery) -> str:
    def live(kind: str) -> list[str]:
        return discovery.accepted_text(found.live(kind))

    problem = live("problem") + [f"Users: {u}" for u in live("user")]
    objective = live("goal")
    constraints = live("constraint")
    decisions = live("decision")
    rejected = [
        f"{i.text} (rejected{': ' + i.note if i.note else ''})"
        for i in found.items
        if i.kind in ("alternative", "decision") and i.status == "rejected"
    ]
    risks = live("risk") + [f"Assumption: {a}" for a in live("assumption")]
    questions = live("question")
    return f"""# {job.title}

## Problem and objective

{_bullets(problem, "who has which problem, and why it matters")}

Objective:

{_bullets(objective, "the outcome this work achieves")}

## Requirements

### Functional

{_bullets(live("requirement"), "what the change must do")}

### Non-functional

{_bullets(constraints, "performance, security, compatibility or other qualities")}

## Proposed solution

- {TODO_MARK}: the design: components, data flow, interfaces, and how it fits the existing \
architecture and ADRs
{chr(10).join(f"- Decision: {d}" for d in decisions)}

## Alternatives and trade-offs

{_bullets(live("alternative") + rejected, "options considered and why this one")}

## Scope

### In scope

- {TODO_MARK}: what this plan delivers

### Out of scope

{_bullets(live("non-goal"), "what this plan deliberately does not do")}

## Dependencies and risks

{_bullets(risks, "dependencies, risks and mitigations")}
{chr(10).join(f"- Open question: {q}" for q in questions)}

## Acceptance criteria

- [ ] {TODO_MARK}: observable criterion

## Verification

- {TODO_MARK}: tests and checks that prove the criteria (unit, integration, manual)

## Tasks

### T1: {TODO_MARK} first task

{TODO_MARK}: what to change, where, and how it is verified.

- [ ] {TODO_MARK}: acceptance criterion
"""


def scaffold(store: JobStore, job_id: str) -> tuple[Path, bool]:
    """Create the job's plan from its discovery. Never overwrites: returns (path, created)."""
    job = store.load(job_id)
    if job.state is not JobState.DRAFT:
        raise InvalidTransitionError(
            f"job {job_id} is {job.state.value}; revise it first (forgeflow design revise)"
        )
    path = plan_file(store, job_id)
    if path.exists():
        return path, False
    text = scaffold_text(job, discovery.load(store, job_id))
    fsutil.atomic_write_text(path, text)
    return path, True


def check(store: JobStore, job_id: str) -> PlanCheck:
    path = plan_file(store, job_id)
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise ValidationError(f"job {job_id} has no plan yet (forgeflow design new)") from exc
    try:
        return check_text(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{path} is not UTF-8") from exc


def submit(store: JobStore, job_id: str, *, reason: str = "") -> tuple[Job, PlanCheck]:
    """Validate the plan and pin it as the next version awaiting Gate A."""
    result = check(store, job_id)
    if not result.ok:
        raise ValidationError(
            "the plan is not ready to submit:\n  - " + "\n  - ".join(result.errors)
        )
    job = store.load(job_id)
    if job.state is JobState.AWAITING_PLAN_APPROVAL:
        # A new version supersedes the pending one; the old snapshot stays in plans/.
        store.transition(job_id, JobState.DRAFT, reason="superseded by a new plan version")
    elif job.state is not JobState.DRAFT:
        raise InvalidTransitionError(
            f"job {job_id} is {job.state.value}; only a draft or a pending plan can be "
            "(re)submitted"
        )
    return store.submit_plan(job_id, plan_relpath(job_id), reason=reason), result


def revise(store: JobStore, job_id: str, reason: str) -> Job:
    """Withdraw a submitted, or approved but not yet handed off, plan back to DRAFT."""
    if not reason.strip():
        raise ValidationError("a reason is required to revise a submitted or approved plan")
    job = store.load(job_id)
    if job.state is JobState.DRAFT:
        return job
    return store.transition(job_id, JobState.DRAFT, reason=reason)
