"""Exception hierarchy. Every refusal is a typed error so callers can report it precisely."""

from __future__ import annotations


class ForgeFlowError(Exception):
    """Base class for all ForgeFlow errors."""


class ValidationError(ForgeFlowError):
    """Structured data (job record, task, configuration) is malformed."""


class InvalidTransitionError(ForgeFlowError):
    """A state transition is not allowed by the transition table or a guard."""


class ConcurrencyError(ForgeFlowError):
    """A record changed since it was read (revision mismatch)."""


class LockTimeoutError(ForgeFlowError):
    """An inter-process lock could not be acquired in time."""


class ActiveJobError(ForgeFlowError):
    """Another job already occupies the single execution slot, or already owns the issue."""


class LeaseError(ForgeFlowError):
    """Worker ownership of a job could not be established or verified."""


class DuplicateWorkerError(LeaseError):
    """A live worker already owns the job."""


class StaleLeaseError(LeaseError):
    """A previous worker died without releasing the job; explicit recovery is required."""


class WorktreeError(ForgeFlowError):
    """A worktree operation was refused to protect existing work."""


class GitError(ForgeFlowError):
    """A git command failed."""


class GitHubError(ForgeFlowError):
    """A GitHub CLI operation failed."""


class GitHubScopeError(GitHubError):
    """The GitHub token lacks a scope required for the operation."""

    def __init__(self, message: str, required_scope: str) -> None:
        super().__init__(message)
        self.required_scope = required_scope


class PathTooLongError(ForgeFlowError):
    """A file path exceeds what Windows allows without long-path support; nothing was written."""


class ToolNotFoundError(ForgeFlowError):
    """An external executable is not on PATH."""


class ProviderError(ForgeFlowError):
    """A provider is unavailable, misconfigured, or refused a task."""


class EvidenceError(ForgeFlowError):
    """An evidence file is missing, unreadable or does not match its record."""
