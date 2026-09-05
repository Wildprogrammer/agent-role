"""Exact-file transactions for managed specialized-agent deployments."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .contracts import read_json_object

MANAGED_MARKER = ".agent-workflow-hub-deployment.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MARKER_IDENTITY_FIELDS = ("deployment_id", "agent_id", "host")


class ManagedTargetError(ValueError):
    """Raised when a target is outside the exact managed boundary."""


class TransactionApplyError(RuntimeError):
    """Raised when a transaction fails but rollback is reconciled."""


class TransactionOutcomeUnknown(TransactionApplyError):
    """Raised when target identity changed and rollback would be unsafe."""


@dataclass(frozen=True, kw_only=True)
class ManagedWrite:
    target: Path
    content: bytes
    expected_before_sha256: str | None

    def __post_init__(self) -> None:
        _validate_path(self.target, "write target")
        if type(self.content) is not bytes:
            raise ManagedTargetError("managed content must be bytes")
        if self.expected_before_sha256 is not None and not _is_sha256(
            self.expected_before_sha256
        ):
            raise ManagedTargetError(
                "expected_before_sha256 must be a lowercase SHA-256 or null"
            )


@dataclass(frozen=True, kw_only=True)
class TransactionResult:
    created: tuple[Path, ...]
    updated: tuple[Path, ...]
    backup_root: Path | None


def _is_sha256(value: object) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _is_reparse(path: Path, snapshot: os.stat_result | None = None) -> bool:
    try:
        current = snapshot if snapshot is not None else path.lstat()
    except OSError as exc:
        raise ManagedTargetError(f"cannot inspect path {path}: {exc}") from None
    if stat.S_ISLNK(current.st_mode):
        return True
    attributes = getattr(current, "st_file_attributes", 0)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & flag:
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            return bool(is_junction())
        except OSError as exc:
            raise ManagedTargetError(f"cannot inspect junction {path}: {exc}") from None
    return False


def _validate_path(path: Path, label: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ManagedTargetError(f"{label} must be an absolute Path")
    normalized = path.resolve(strict=False)
    broad_roots = {
        Path(path.anchor).resolve(strict=False),
        Path.home().resolve(strict=False),
        Path.cwd().resolve(strict=False),
    }
    if normalized in broad_roots:
        raise ManagedTargetError(f"{label} cannot be a broad root")
    for candidate in (path, *path.parents):
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if _is_reparse(candidate):
            raise ManagedTargetError(
                f"{label} cannot use a symlink, junction, or reparse path"
            )


def _stable_bytes(path: Path) -> bytes:
    try:
        before = path.lstat()
        if _is_reparse(path, before) or not stat.S_ISREG(before.st_mode):
            raise ManagedTargetError(f"managed path is not a regular file: {path}")
        content = path.read_bytes()
        after = path.lstat()
    except ManagedTargetError:
        raise
    except OSError as exc:
        raise ManagedTargetError(f"cannot read managed file {path}: {exc}") from None
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        getattr(before, "st_file_attributes", 0),
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        getattr(after, "st_file_attributes", 0),
    )
    if _is_reparse(path, after) or identity_before != identity_after:
        raise TransactionOutcomeUnknown(f"managed file changed while reading: {path}")
    return content


def _content_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_managed_target(
    target: Path,
    *,
    mode: Literal["create", "update"],
    expected_marker: Mapping[str, object],
) -> None:
    """Validate a create/update root without accepting unknown ownership."""

    _validate_path(target, "managed target")
    if mode not in ("create", "update"):
        raise ManagedTargetError("mode must be create or update")
    if not isinstance(expected_marker, Mapping):
        raise ManagedTargetError("expected_marker must be an object")
    for field in _MARKER_IDENTITY_FIELDS:
        value = expected_marker.get(field)
        if type(value) is not str or not value.strip():
            raise ManagedTargetError(f"expected marker {field} must be nonblank")

    if mode == "create":
        if target.exists() or target.is_symlink():
            raise ManagedTargetError("create target already exists")
        return

    if not target.is_dir():
        raise ManagedTargetError("update target must be an existing directory")
    marker_path = target / MANAGED_MARKER
    _validate_path(marker_path, "managed marker")
    if not marker_path.is_file() or _is_reparse(marker_path):
        raise ManagedTargetError("update target is missing its managed marker")
    try:
        actual = read_json_object(marker_path)
    except ValueError as exc:
        raise ManagedTargetError(f"invalid managed marker: {exc}") from None
    for field in _MARKER_IDENTITY_FIELDS:
        if actual.get(field) != expected_marker[field]:
            raise ManagedTargetError(
                f"managed marker {field} does not match the requested deployment"
            )


def _atomic_replace(target: Path, content: bytes) -> None:
    """Flush a same-directory temporary file before one atomic replace."""

    _validate_path(target, "atomic write target")
    target.parent.mkdir(parents=True, exist_ok=True)
    _validate_path(target.parent, "atomic write parent")
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=".awh-",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_text)
    replaced = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        replaced = True
    finally:
        if not replaced:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _write_backup(path: Path, content: bytes) -> None:
    _validate_path(path, "backup file")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise TransactionApplyError(f"cannot write transaction backup {path}: {exc}") from None


def _backup_path(backup_root: Path, target: Path) -> Path:
    identity = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
    path = backup_root / f"{identity}.bak"
    # Keep the exact destination and full digest; opt into Windows long-path I/O
    # without changing the host's global long-path policy. Rollback uses this
    # same function, so it can read the backup as well as create it.
    if os.name == "nt" and len(str(path)) >= 260:
        absolute = str(path.resolve(strict=False))
        if not absolute.startswith("\\\\?\\"):
            absolute = ("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\")
                        else "\\\\?\\" + absolute)
        return Path(absolute)
    return path


def reconcile_uncertain_write(
    target: Path,
    expected_sha256: str,
) -> Literal["applied", "not_applied", "outcome_unknown"]:
    """Reconcile one uncertain exact-file write by its expected digest."""

    if not _is_sha256(expected_sha256):
        raise ManagedTargetError("expected_sha256 must be a lowercase SHA-256")
    try:
        _validate_path(target, "reconcile target")
        if not target.exists() and not target.is_symlink():
            return "not_applied"
        if target.is_symlink() or _is_reparse(target) or not target.is_file():
            return "outcome_unknown"
        actual = _content_sha256(_stable_bytes(target))
    except (ManagedTargetError, TransactionOutcomeUnknown, OSError):
        return "outcome_unknown"
    return "applied" if actual == expected_sha256 else "outcome_unknown"


def apply_managed_transaction(
    writes: tuple[ManagedWrite, ...],
    *,
    backup_root: Path,
) -> TransactionResult:
    """Apply exact writes and roll back only bytes still owned by this attempt."""

    writes = tuple(writes)
    if not writes or not all(type(item) is ManagedWrite for item in writes):
        raise ManagedTargetError("writes must be a non-empty ManagedWrite tuple")
    _validate_path(backup_root, "backup_root")
    targets = [item.target.resolve(strict=False) for item in writes]
    if len(targets) != len(set(targets)):
        raise ManagedTargetError("managed write targets must be unique")
    for target in targets:
        if target == backup_root or backup_root in target.parents or target in backup_root.parents:
            raise ManagedTargetError("backup_root and write targets must be disjoint")

    originals: dict[Path, bytes] = {}
    for item in writes:
        _validate_path(item.target, "write target")
        if item.expected_before_sha256 is None:
            if item.target.exists() or item.target.is_symlink():
                raise ManagedTargetError(f"create write target already exists: {item.target}")
        else:
            if not item.target.is_file():
                raise ManagedTargetError(f"update write target does not exist: {item.target}")
            original = _stable_bytes(item.target)
            if _content_sha256(original) != item.expected_before_sha256:
                raise ManagedTargetError(f"update source digest changed: {item.target}")
            originals[item.target] = original

    if originals:
        backup_root.mkdir(parents=True, exist_ok=True)
        _validate_path(backup_root, "backup_root")
        for target, content in originals.items():
            _write_backup(_backup_path(backup_root, target), content)

    applied: list[ManagedWrite] = []
    try:
        for item in writes:
            intended = _content_sha256(item.content)
            try:
                _atomic_replace(item.target, item.content)
            except Exception as write_error:
                state = reconcile_uncertain_write(item.target, intended)
                if state == "applied":
                    applied.append(item)
                elif state == "outcome_unknown":
                    raise TransactionOutcomeUnknown(
                        f"write result is unknown: {item.target}"
                    ) from write_error
                raise
            applied.append(item)
            if reconcile_uncertain_write(item.target, intended) != "applied":
                raise TransactionOutcomeUnknown(
                    f"write could not be reconciled after replace: {item.target}"
                )
    except Exception as exc:
        unknown: list[Path] = []
        rollback_errors: list[Path] = []
        for item in reversed(applied):
            intended = _content_sha256(item.content)
            if reconcile_uncertain_write(item.target, intended) != "applied":
                unknown.append(item.target)
                continue
            try:
                if item.expected_before_sha256 is None:
                    item.target.unlink()
                else:
                    backup = _backup_path(backup_root, item.target)
                    original = _stable_bytes(backup)
                    if _content_sha256(original) != item.expected_before_sha256:
                        unknown.append(item.target)
                        continue
                    try:
                        _atomic_replace(item.target, original)
                    except Exception:
                        if (
                            reconcile_uncertain_write(
                                item.target, item.expected_before_sha256
                            )
                            != "applied"
                        ):
                            raise
                    if reconcile_uncertain_write(
                        item.target, item.expected_before_sha256
                    ) != "applied":
                        unknown.append(item.target)
            except Exception:
                rollback_errors.append(item.target)
        if unknown or rollback_errors or isinstance(exc, TransactionOutcomeUnknown):
            affected = ", ".join(str(path) for path in (*unknown, *rollback_errors))
            raise TransactionOutcomeUnknown(
                f"transaction outcome is unknown for: {affected or exc}"
            ) from exc
        raise TransactionApplyError(f"managed transaction failed: {exc}") from exc

    return TransactionResult(
        created=tuple(
            item.target for item in writes if item.expected_before_sha256 is None
        ),
        updated=tuple(
            item.target for item in writes if item.expected_before_sha256 is not None
        ),
        backup_root=backup_root if originals else None,
    )


__all__ = [
    "MANAGED_MARKER",
    "ManagedTargetError",
    "ManagedWrite",
    "TransactionApplyError",
    "TransactionOutcomeUnknown",
    "TransactionResult",
    "apply_managed_transaction",
    "reconcile_uncertain_write",
    "validate_managed_target",
]
