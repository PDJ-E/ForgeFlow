"""Interactive product discovery: the durable outcomes of a conversation about an idea.

The conversation itself happens in the host (Claude Code or Codex), not here. What is kept is
the small set of outcomes worth resuming from: the problem, users, goals, requirements,
constraints, assumptions, alternatives, decisions, open questions, non-goals and risks. Each is
an item with a status history, so a rejected option or a superseded decision is never lost and
an earlier decision can be reinstated.

A discovery belongs to a DRAFT job (ADR 0003: drafts are discussion-only and can never dispatch
code). It is stored as ``discovery.json`` beside ``job.json`` in the self-ignored runtime
directory, never published to GitHub, and changes only while the job is a draft.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from forgeflow.errors import ValidationError
from forgeflow.jobs import UNASSIGNED_PROVIDER, Job, JobState, Policy, Repo, utc_now
from forgeflow.store import JobStore

DOCUMENT = "discovery.json"
SCHEMA_VERSION = 1

# kind -> id prefix. Order is the order used when presenting a discovery.
KINDS: dict[str, str] = {
    "problem": "P",
    "user": "U",
    "goal": "G",
    "requirement": "R",
    "constraint": "C",
    "assumption": "A",
    "alternative": "O",
    "decision": "D",
    "risk": "K",
    "question": "Q",
    "non-goal": "N",
}
STATUSES = ("proposed", "accepted", "rejected", "superseded")
LIVE_STATUSES = frozenset({"proposed", "accepted"})
_EDITABLE = frozenset({JobState.DRAFT})
MAX_TEXT = 2000


@dataclass
class Item:
    id: str
    kind: str
    text: str
    status: str
    created_at: str
    updated_at: str
    note: str = ""
    supersedes: str | None = None
    history: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "note": self.note,
            "supersedes": self.supersedes,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Item:
        try:
            item = cls(
                id=str(d["id"]),
                kind=str(d["kind"]),
                text=str(d["text"]),
                status=str(d["status"]),
                created_at=str(d["created_at"]),
                updated_at=str(d["updated_at"]),
                note=str(d.get("note") or ""),
                supersedes=d.get("supersedes"),
                history=[dict(h) for h in d.get("history") or []],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"malformed discovery item: {exc}") from exc
        if item.kind not in KINDS or item.status not in STATUSES:
            raise ValidationError(f"discovery item {item.id} has an unknown kind or status")
        return item


@dataclass
class Discovery:
    job_id: str
    idea: str
    created_at: str
    items: list[Item] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "job_id": self.job_id,
            "idea": self.idea,
            "created_at": self.created_at,
            "items": [i.to_dict() for i in self.items],
        }

    @classmethod
    def from_dict(cls, d: Any, job_id: str) -> Discovery:
        if not isinstance(d, dict) or d.get("schema_version") != SCHEMA_VERSION:
            raise ValidationError(f"{DOCUMENT} of {job_id} has an unsupported format")
        if d.get("job_id") != job_id:
            raise ValidationError(f"{DOCUMENT} of {job_id} names job {d.get('job_id')!r}")
        return cls(
            job_id=job_id,
            idea=str(d.get("idea") or ""),
            created_at=str(d.get("created_at") or ""),
            items=[Item.from_dict(i) for i in d.get("items") or []],
        )

    def get(self, item_id: str) -> Item:
        for item in self.items:
            if item.id == item_id.upper():
                return item
        raise ValidationError(f"no discovery item {item_id!r}")

    def live(self, kind: str | None = None) -> list[Item]:
        return [
            i for i in self.items if i.status in LIVE_STATUSES and (kind is None or i.kind == kind)
        ]

    def next_id(self, kind: str) -> str:
        prefix = KINDS[kind]
        used = [int(i.id[len(prefix) :]) for i in self.items if i.kind == kind]
        return f"{prefix}{max(used, default=0) + 1}"


def _text(value: str, what: str) -> str:
    value = value.strip()
    if not value:
        raise ValidationError(f"{what} must not be empty")
    if len(value) > MAX_TEXT:
        raise ValidationError(f"{what} is longer than {MAX_TEXT} characters; summarise it")
    return value


def start(store: JobStore, title: str, idea: str, repo: Repo) -> tuple[Job, Discovery]:
    """Create a DRAFT job for an idea, with an empty discovery. No builder is assigned."""
    job = store.create(title, repo, Policy(builder=UNASSIGNED_PROVIDER, reviewer=None))
    now = utc_now()
    discovery = Discovery(job_id=job.job_id, idea=_text(idea or title, "idea"), created_at=now)
    store.edit_document(
        job.job_id,
        DOCUMENT,
        lambda _job, _doc: discovery.to_dict(),
        allowed_states=_EDITABLE,
        event="discovery-started",
    )
    return store.load(job.job_id), discovery


def load(store: JobStore, job_id: str) -> Discovery:
    data = store.read_document(job_id, DOCUMENT)
    if data is None:
        raise ValidationError(f"job {job_id} has no discovery record (start one with `idea new`)")
    return Discovery.from_dict(data, job_id)


def add(
    store: JobStore,
    job_id: str,
    kind: str,
    text: str,
    *,
    status: str = "proposed",
    note: str = "",
    supersedes: str | None = None,
) -> Item:
    """Record one outcome. ``supersedes`` replaces an earlier item, which is kept as history."""
    if kind not in KINDS:
        raise ValidationError(f"unknown kind {kind!r}; use one of: {', '.join(KINDS)}")
    if status not in ("proposed", "accepted", "rejected"):
        raise ValidationError("a new item is 'proposed', 'accepted' or 'rejected'")
    body = _text(text, "text")
    created: list[Item] = []

    def edit(_job: Job, doc: Any) -> Any:
        discovery = Discovery.from_dict(doc, job_id)
        now = utc_now()
        old_id = None
        if supersedes:
            old = discovery.get(supersedes)
            if old.kind != kind:
                raise ValidationError(f"{old.id} is a {old.kind}, not a {kind}")
            if old.status == "superseded":
                raise ValidationError(f"{old.id} is already superseded")
            _set_status(old, "superseded", f"replaced by a new {kind}", now)
            old_id = old.id
        item = Item(
            id=discovery.next_id(kind),
            kind=kind,
            text=body,
            status=status,
            created_at=now,
            updated_at=now,
            note=note.strip(),
            supersedes=old_id,
            history=[{"at": now, "status": status, "note": note.strip()}],
        )
        discovery.items.append(item)
        created.append(item)
        return discovery.to_dict()

    store.edit_document(
        job_id,
        DOCUMENT,
        edit,
        allowed_states=_EDITABLE,
        event="discovery-item-added",
        details={"kind": kind, "status": status, "supersedes": supersedes},
    )
    return created[0]


def mark(store: JobStore, job_id: str, item_id: str, status: str, *, note: str = "") -> Item:
    """Accept, reject or reinstate an item. Reinstating returns to an earlier decision."""
    if status not in ("proposed", "accepted", "rejected"):
        raise ValidationError("status must be proposed, accepted or rejected")
    changed: list[Item] = []

    def edit(_job: Job, doc: Any) -> Any:
        discovery = Discovery.from_dict(doc, job_id)
        item = discovery.get(item_id)
        now = utc_now()
        if status in LIVE_STATUSES and item.status == "superseded":
            # Returning to an earlier decision: its replacement stops being live.
            for other in discovery.items:
                if other.supersedes == item.id and other.status in LIVE_STATUSES:
                    _set_status(other, "superseded", f"{item.id} reinstated", now)
        _set_status(item, status, note.strip(), now)
        changed.append(item)
        return discovery.to_dict()

    store.edit_document(
        job_id,
        DOCUMENT,
        edit,
        allowed_states=_EDITABLE,
        event="discovery-item-marked",
        details={"item": item_id.upper(), "status": status},
    )
    return changed[0]


def _set_status(item: Item, status: str, note: str, now: str) -> None:
    item.status = status
    item.updated_at = now
    if note:
        item.note = note
    item.history.append({"at": now, "status": status, "note": note})


def render(discovery: Discovery, *, include_closed: bool = False) -> str:
    """A compact summary to resume the conversation from."""
    lines = [f"Idea: {discovery.idea}"]
    closed = 0
    for kind in KINDS:
        items = [i for i in discovery.items if i.kind == kind]
        shown = [i for i in items if include_closed or i.status in LIVE_STATUSES]
        closed += len(items) - len(shown)
        if not shown:
            continue
        lines.append(f"{kind.replace('-', ' ').capitalize()}s:")
        for i in shown:
            extra = f" (replaces {i.supersedes})" if i.supersedes else ""
            note = f" - {i.note}" if i.note else ""
            lines.append(f"  {i.id} [{i.status}] {i.text}{extra}{note}")
    if closed:
        lines.append(f"({closed} rejected or superseded item(s) hidden; use --all to see them)")
    if not discovery.items:
        lines.append("(no outcomes recorded yet)")
    return "\n".join(lines)


def accepted_text(items: Iterable[Item]) -> list[str]:
    return [f"{i.text}{'' if i.status == 'accepted' else ' (proposed)'}" for i in items]
