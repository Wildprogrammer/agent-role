"""Shared guarded Git validation used by lifecycle and git-operations."""

from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import unquote, urlsplit


class GitGuardError(ValueError):
    """Raised when a Git identity, transition, or command is unsafe."""


_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_WINDOWS_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class WorktreeEvidence:
    """A read-only snapshot of user changes observed before Git mutation."""

    tracked_changes: tuple[str, ...] = ()
    untracked_paths: tuple[str, ...] = ()


def validate_commit_sha(value: str, *, label: str = "commit") -> str:
    """Return a canonical full commit SHA or reject the value."""

    if not isinstance(value, str) or _FULL_SHA.fullmatch(value) is None:
        raise GitGuardError(f"{label} must be a lowercase full commit SHA")
    return value


def validate_repository_relative_path(value: str) -> str:
    """Return one normalized slash-separated path inside a repository."""

    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or _WINDOWS_DRIVE_PATH.match(value) is not None
        or "\\" in value
        or "\x00" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise GitGuardError("path must be a normalized repository-relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise GitGuardError("path must be a normalized repository-relative path")
    return value


def validate_branch_ref(value: str) -> str:
    """Require a safe canonical Git ref name without lifecycle policy."""

    if (
        not isinstance(value, str)
        or not value
        or len(value) > 255
        or value == "@"
        or value.startswith(("/", ".", "-"))
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(
            ord(character) < 32
            or ord(character) == 127
            or character in " ~^:?*[\\"
            for character in value
        )
    ):
        raise GitGuardError("branch ref is unsafe or noncanonical")
    if any(
        part in {"", ".", ".."}
        or part.startswith(".")
        or part.endswith((".", ".lock"))
        for part in value.split("/")
    ):
        raise GitGuardError("branch ref is unsafe or noncanonical")
    return value


def _validate_configured_remote_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or _REMOTE_NAME.fullmatch(value) is None
        or value in {".", ".."}
        or ".." in value
    ):
        raise GitGuardError("configured remote name is unsafe")
    return value


def normalize_https_push_url(value: object, label: str = "push URL") -> str:
    """Return the canonical HTTPS remote form used by every Git guard."""

    if not isinstance(value, str) or not value or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise GitGuardError(f"{label} is invalid")
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise GitGuardError(f"{label} must not contain credentials")
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.query
        or parsed.fragment
        or "\\" in parsed.path
    ):
        raise GitGuardError(f"{label} must be an HTTPS URL")
    decoded_path = unquote(parsed.path)
    if ".." in decoded_path.split("/"):
        raise GitGuardError(f"{label} path is unsafe")
    try:
        port = parsed.port
    except ValueError:
        raise GitGuardError(f"{label} is invalid") from None
    if port not in {None, 443}:
        raise GitGuardError(f"{label} must use the default HTTPS port")
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return f"https://{parsed.hostname.casefold()}{path}"


def validate_clean_worktree(evidence: WorktreeEvidence) -> WorktreeEvidence:
    """Reject mutation if tracked or untracked user changes are present."""

    if not isinstance(evidence, WorktreeEvidence):
        raise GitGuardError("worktree evidence is required")
    if evidence.tracked_changes or evidence.untracked_paths:
        raise GitGuardError(
            "worktree is not clean; preserve user changes and stop before mutation"
        )
    return evidence


def _is_safe_branch(value: str) -> bool:
    if (
        not value
        or value.startswith(("/", ".", "-"))
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(
            ord(character) < 32
            or ord(character) == 127
            or character in " ~^:?*[\\"
            for character in value
        )
    ):
        return False
    return all(
        part not in {"", ".", ".."}
        and not part.startswith(".")
        and not part.endswith((".", ".lock"))
        for part in value.split("/")
    )
