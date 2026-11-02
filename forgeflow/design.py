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
# Plan size contract (ADR 0006 §5): oversized work is split into jobs before Gate A.
MAX_TASKS = 6
MAX_CRITERIA_PER_TASK = 6
MAX_FILES_PER_TASK = 8
PLAN_SIZE_WARNING = 16_000  # characters; well under MAX_PLAN_CHARS

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
# A criterion's text, continued on indented lines that do not start another bullet.
_CRITERION_TEXT = re.compile(
    r"^\s*[-*] \[[ xX]\] (.+(?:\n(?![ \t]*[-*] )[ \t]+\S.*)*)", re.MULTILINE
)
# What every task's builder sees besides its own task (ADR 0006 §1).
FRAME_SECTIONS = ("Problem and objective", "Requirements", "Scope")
# "- **Files:** `a.py`, `b.py`" or "Files: a.py, b.py", continued on indented lines.
_FILES = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}Files:\*{0,2}(.*(?:\n(?![ \t]*[-*] )[ \t]+.*)*)", re.MULTILINE
)
_BACKTICKED = re.compile(r"`[^`\n]+`")
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
    files: int = 0  # entries on its Files: line; 0 when it has none


@dataclass(frozen=True)
class PlanTask:
    """One ``### T<n>`` task as the builder sees it: its text in full and what it names."""

    id: str
    title: str
    body: str  # heading included
    criteria: tuple[str, ...]
    files: tuple[str, ...]


@dataclass(frozen=True)
class PlanParts:
    """A plan cut into what every task needs (the frame) and the tasks themselves."""

    title: str
    frame: tuple[str, ...]  # the objective, requirements and scope sections, headings included
    tasks: tuple[PlanTask, ...]
    whole_plan: bool  # no parseable task: the one task holds the whole plan

    def task(self, task_id: str) -> PlanTask:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise ValidationError(f"the plan has no task {task_id}")


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
        lines += [
            f"  {t.id}: {t.title} ({t.criteria} criteria, {t.files} files)" for t in self.tasks
        ]
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

    seen: set[str] = set()
    for chunk in _task_chunks(text):
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
        elif criteria > MAX_CRITERIA_PER_TASK:
            result.errors.append(
                f"task {task_id} has {criteria} acceptance criteria; at most "
                f"{MAX_CRITERIA_PER_TASK}: split the task"
            )
        files = _files(chunk)
        if files is None:
            result.errors.append(f"task {task_id} has no 'Files:' line (the files it changes)")
        elif files > MAX_FILES_PER_TASK:
            result.errors.append(
                f"task {task_id} lists {files} files; at most {MAX_FILES_PER_TASK}: split the task"
            )
        result.tasks.append(Task(task_id, title, criteria, files or 0))
    if any(k.startswith("tasks") for k in keys) and not result.tasks:
        result.errors.append("'## Tasks' has no '### T<n>: <title>' task")
    if len(result.tasks) > MAX_TASKS:
        result.errors.append(
            f"the plan has {len(result.tasks)} tasks; at most {MAX_TASKS} per job: split the "
            "work into separate jobs"
        )

    if TODO_MARK in text:
        result.errors.append(f"unresolved {TODO_MARK} placeholders remain")
    if len(text) > MAX_PLAN_CHARS:
        result.errors.append(
            f"the plan is {len(text)} characters; GitHub issue bodies allow about "
            f"{MAX_PLAN_CHARS}. Shorten it or split the work into separate jobs"
        )
    if PLAN_SIZE_WARNING < len(text) <= MAX_PLAN_CHARS:
        result.warnings.append(
            f"the plan is {len(text)} characters (over {PLAN_SIZE_WARNING}); consider whether "
            "to split it into smaller jobs"
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


def _sections(text: str) -> list[tuple[str, str]]:
    """Every ``##`` section as (heading, its text from the heading to the next one)."""
    heads = list(_H2.finditer(text))
    ends = [m.start() for m in heads[1:]] + [len(text)]
    return [(m.group(1), text[m.start() : end]) for m, end in zip(heads, ends, strict=False)]


def _task_chunks(text: str) -> list[str]:
    """The ``###`` blocks of the (last) ``## Tasks`` section, each up to the next ``###``."""
    body = ""
    for heading, section in _sections(text):
        if _section_key(heading).startswith("tasks"):
            body = section.split("\n", 1)[1] if "\n" in section else ""
    starts = [m.start() for m in _H3.finditer(body)]
    ends = starts[1:] + [len(body)]
    return [body[start:end] for start, end in zip(starts, ends, strict=False)]


def _file_list(chunk: str) -> tuple[str, ...] | None:
    """The files a task's Files: line names: per comma-separated entry its backticked paths,
    or the entry itself. ``None`` when it has no such line or the line names nothing."""
    match = _FILES.search(chunk)
    if match is None:
        return None
    names: list[str] = []
    for entry in match.group(1).split(","):
        quoted = [q.strip("`") for q in _BACKTICKED.findall(entry)]
        names += quoted or ([entry.strip()] if entry.strip() else [])
    return tuple(names) or None


def _files(chunk: str) -> int | None:
    found = _file_list(chunk)
    return len(found) if found else None


def _criteria(text: str) -> tuple[str, ...]:
    found = (" ".join(m.split()) for m in _CRITERION_TEXT.findall(text))
    return tuple(dict.fromkeys(found))


def plan_parts(text: str) -> PlanParts:
    """Cut an approved plan into its frame and its tasks (ADR 0006 §1). Pure.

    A plan without any ``### T<n>: <title>`` task under ``## Tasks`` (older plans) yields one
    task holding the whole plan, with ``whole_plan`` set.
    """
    titles = _H1.findall(text)
    title = titles[0].strip() if titles else ""
    frame_keys = [_section_key(name) for name in FRAME_SECTIONS]
    frame = tuple(
        section.strip()
        for heading, section in _sections(text)
        if any(_section_key(heading).startswith(key) for key in frame_keys)
    )
    tasks = []
    for chunk in _task_chunks(text):
        match = _TASK.match(chunk)
        if match:
            criteria = _criteria(chunk)
            files = _file_list(chunk) or ()
            tasks.append(PlanTask(match.group(1), match.group(2), chunk.strip(), criteria, files))
    if tasks:
        return PlanParts(title, frame, tuple(tasks), whole_plan=False)
    criteria = _criteria(text)
    whole = PlanTask("T1", title or "the plan", text.strip(), criteria, ())
    return PlanParts(title, frame, (whole,), whole_plan=True)


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
    hidden = len(found.items) - len(found.live())
    closed = (
        f"\n<!-- {hidden} rejected or superseded discovery item(s) not shown: "
        f"forgeflow idea show {job.ref or job.job_id} --all -->\n"
        if hidden
        else ""
    )
    risks = live("risk") + [f"Assumption: {a}" for a in live("assumption")]
    questions = live("question")
    return f"""# {job.title}
{closed}
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

{_bullets(live("alternative"), "options considered and why this one")}

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

- **Files:** {TODO_MARK}: the files this task changes (at most {MAX_FILES_PER_TASK})
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
