"""Local per-clone configuration: which GitHub Project approved work is handed off to.

Stored in ``.forge/runtime/config.json`` (self-ignored, never committed, nothing secret). It is
written only by ``forgeflow init --project N``, which first reads the Project and checks that its
Status field has the option ForgeFlow will use. ForgeFlow never creates Projects, fields or
options; if the board changes, handoff re-reads it and stops with a clear error.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from forgeflow import fsutil
from forgeflow.errors import GitHubError, ValidationError
from forgeflow.github import GitHubClient, ProjectInfo
from forgeflow.jobs import utc_now
from forgeflow.store import RUNTIME_RELATIVE

CONFIG_NAME = "config.json"
SCHEMA_VERSION = 1
DEFAULT_READY_STATUS = "Ready"


@dataclass(frozen=True)
class ProjectBinding:
    owner: str
    number: int
    project_id: str
    title: str
    url: str
    ready_status: str  # Status option that approved, handed-off work is placed in
    bound_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Any) -> ProjectBinding:
        try:
            return cls(
                owner=str(d["owner"]),
                number=int(d["number"]),
                project_id=str(d["project_id"]),
                title=str(d["title"]),
                url=str(d["url"]),
                ready_status=str(d["ready_status"]),
                bound_at=str(d["bound_at"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError(f"malformed github_project in {CONFIG_NAME}: {exc}") from exc


def config_path(repo_root: Path) -> Path:
    return repo_root / RUNTIME_RELATIVE / CONFIG_NAME


def load_binding(repo_root: Path) -> ProjectBinding | None:
    try:
        data = fsutil.read_json(config_path(repo_root))
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise ValidationError(f"{config_path(repo_root)} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(f"{config_path(repo_root)} has an unsupported format")
    project = data.get("github_project")
    return ProjectBinding.from_dict(project) if project else None


def check_project(project: ProjectInfo, ready_status: str) -> str:
    """The option id ForgeFlow will set, or a GitHubError explaining what the board lacks."""
    if project.closed:
        raise GitHubError(f"GitHub Project #{project.number} {project.title!r} is closed")
    if not project.status_field_id:
        raise GitHubError(
            f"GitHub Project #{project.number} has no single-select 'Status' field; "
            "ForgeFlow does not create fields"
        )
    option = project.option_id(ready_status)
    if option is None:
        names = ", ".join(project.status_options) or "none"
        raise GitHubError(
            f"GitHub Project #{project.number} has no Status option {ready_status!r} "
            f"(options: {names}); choose one with --ready-status. ForgeFlow does not create "
            "options"
        )
    return option


def bind_project(
    repo_root: Path,
    client: GitHubClient,
    owner: str,
    number: int,
    ready_status: str = DEFAULT_READY_STATUS,
) -> ProjectBinding:
    project = client.project_info(owner, number)
    check_project(project, ready_status)
    exact = next(n for n in project.status_options if n.casefold() == ready_status.casefold())
    binding = ProjectBinding(
        owner=owner,
        number=project.number,
        project_id=project.id,
        title=project.title,
        url=project.url,
        ready_status=exact,
        bound_at=utc_now(),
    )
    fsutil.write_self_ignoring_gitignore(repo_root / RUNTIME_RELATIVE)
    fsutil.atomic_write_json(
        config_path(repo_root),
        {"schema_version": SCHEMA_VERSION, "github_project": binding.to_dict()},
    )
    return binding
