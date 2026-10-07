"""Resolve, validate, and freeze finite Skill source trees."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from agent_workflow_hub.contracts import ContractError, validate_skill
from agent_workflow_hub.frontmatter import FrontmatterError, parse_markdown

from .contracts import (
    DeploymentRequest,
    SkillFile,
    SkillSelection,
    SkillSnapshot,
    canonical_sha256,
)

_SKIPPED_DIRECTORIES = frozenset(
    (
        "outputs",
        "tests",
        "__pycache__",
        ".pytest_cache",
        ".git",
        ".codex-remote-attachments",
    )
)
_TEMP_SUFFIXES = (".tmp", ".temp", ".swp", ".swo", "~")


class SourceSnapshotError(ValueError):
    """Raised when a Skill source is invalid or unsafe to snapshot."""


class SourceDriftError(SourceSnapshotError):
    """Raised when source bytes change while a snapshot is being produced."""


def _is_reparse(path: Path, snapshot: os.stat_result | None = None) -> bool:
    try:
        current = snapshot if snapshot is not None else path.lstat()
    except OSError as exc:
        raise SourceSnapshotError(f"cannot inspect source path {path}: {exc}") from None
    if stat.S_ISLNK(current.st_mode):
        return True
    attributes = getattr(current, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if bool(attributes & reparse_flag):
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            return bool(is_junction())
        except OSError as exc:
            raise SourceSnapshotError(
                f"cannot inspect source junction {path}: {exc}"
            ) from None
    return False


def _assert_absolute(path: Path, label: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise SourceSnapshotError(f"{label} must be an absolute Path")


def _assert_no_reparse_ancestry(path: Path) -> None:
    _assert_absolute(path, "path")
    candidates = [path, *path.parents]
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        if _is_reparse(candidate):
            raise SourceSnapshotError(
                f"symlink, junction, or reparse path is not allowed: {candidate}"
            )


def _identity(snapshot: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        snapshot.st_dev,
        snapshot.st_ino,
        snapshot.st_mode,
        snapshot.st_size,
        snapshot.st_mtime_ns,
        getattr(snapshot, "st_file_attributes", 0),
    )


def _stable_read(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise SourceSnapshotError(f"cannot inspect source file {path}: {exc}") from None
    if _is_reparse(path, before) or not stat.S_ISREG(before.st_mode):
        raise SourceSnapshotError(f"source entry is not a regular file: {path}")
    try:
        content = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise SourceSnapshotError(f"cannot read source file {path}: {exc}") from None
    if _is_reparse(path, after) or _identity(before) != _identity(after):
        raise SourceDriftError(f"source changed while reading: {path}")
    return content, after


def _is_temporary_file(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered.startswith(".~")
        or lowered.startswith(".#")
        or lowered.endswith(_TEMP_SUFFIXES)
    )


def _source_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []

    def raise_walk_error(error: OSError) -> None:
        raise SourceSnapshotError(
            f"cannot enumerate Skill source {root}: {error}"
        )

    try:
        walker = os.walk(
            root,
            topdown=True,
            onerror=raise_walk_error,
            followlinks=False,
        )
        for current_text, directory_names, file_names in walker:
            current = Path(current_text)
            _assert_no_reparse_ancestry(current)

            retained_directories: list[str] = []
            for name in sorted(directory_names, key=lambda item: item.encode("utf-8")):
                candidate = current / name
                if _is_reparse(candidate):
                    raise SourceSnapshotError(
                        f"symlink, junction, or reparse directory is not allowed: {candidate}"
                    )
                if name not in _SKIPPED_DIRECTORIES:
                    retained_directories.append(name)
            directory_names[:] = retained_directories

            for name in sorted(file_names, key=lambda item: item.encode("utf-8")):
                if _is_temporary_file(name):
                    continue
                candidate = current / name
                if _is_reparse(candidate):
                    raise SourceSnapshotError(
                        f"symlink, junction, or reparse file is not allowed: {candidate}"
                    )
                files.append(candidate)
    except SourceSnapshotError:
        raise
    except OSError as exc:
        raise SourceSnapshotError(f"cannot enumerate Skill source {root}: {exc}") from None
    return tuple(
        sorted(
            files,
            key=lambda item: item.relative_to(root).as_posix().encode("utf-8"),
        )
    )


def resolve_skill_source(
    hub_root: Path,
    selection: SkillSelection,
) -> Path:
    """Resolve a selected Skill without accepting an alternate dependency source."""

    _assert_absolute(hub_root, "hub_root")
    _assert_no_reparse_ancestry(hub_root)
    if type(selection) is not SkillSelection:
        raise SourceSnapshotError("selection must be a SkillSelection")

    if selection.source_kind == "hub-workflow":
        expected_reference = f"workflows/{selection.name}"
        if selection.source.replace("\\", "/") != expected_reference:
            raise SourceSnapshotError(
                "Hub workflow source must match workflows/<selection-name>"
            )
        source = hub_root / "workflows" / selection.name
    elif selection.source_kind == "external-skill":
        source = Path(selection.source)
        _assert_absolute(source, "external Skill source")
    else:  # Defensive for direct construction despite the closed contract.
        raise SourceSnapshotError("unsupported Skill source kind")

    _assert_no_reparse_ancestry(source)
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise SourceSnapshotError(f"Skill source does not exist: {source}") from None
    if not resolved.is_dir():
        raise SourceSnapshotError(f"Skill source must be one directory: {resolved}")

    if selection.source_kind == "hub-workflow":
        expected_parent = (hub_root / "workflows").resolve(strict=True)
        if resolved.parent != expected_parent or resolved.name != selection.name:
            raise SourceSnapshotError("Hub workflow escaped the workflows root")
    elif resolved.name != selection.name:
        raise SourceSnapshotError("external Skill directory must match selection name")
    return resolved


def snapshot_skill(
    hub_root: Path,
    selection: SkillSelection,
) -> SkillSnapshot:
    """Validate and hash one complete, stable Skill bundle."""

    source = resolve_skill_source(hub_root, selection)
    skill_path = source / "SKILL.md"
    try:
        skill_before = skill_path.lstat()
    except OSError as exc:
        raise SourceSnapshotError(f"Skill is missing SKILL.md: {source}") from None
    if _is_reparse(skill_path, skill_before):
        raise SourceSnapshotError("SKILL.md cannot be a reparse path")
    try:
        frontmatter, body = parse_markdown(skill_path)
        contract = validate_skill(skill_path, frontmatter, body)
    except (FrontmatterError, ContractError, OSError, UnicodeError) as exc:
        raise SourceSnapshotError(f"invalid Skill {selection.name}: {exc}") from None
    if contract.name != selection.name:
        raise SourceSnapshotError(
            "SKILL.md name must exactly match the selected Skill name"
        )

    records: list[SkillFile] = []
    for path in _source_files(source):
        content, after = _stable_read(path)
        if path == skill_path and _identity(skill_before) != _identity(after):
            raise SourceDriftError("SKILL.md changed after contract validation")
        records.append(
            SkillFile(
                relative_path=path.relative_to(source).as_posix(),
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    if not any(record.relative_path == "SKILL.md" for record in records):
        raise SourceSnapshotError("Skill snapshot must contain SKILL.md")
    files = tuple(records)
    tree_sha256 = canonical_sha256([item.to_mapping() for item in files])
    return SkillSnapshot(
        selection=selection,
        files=files,
        tree_sha256=tree_sha256,
    )


def snapshot_composition(
    hub_root: Path,
    request: DeploymentRequest,
) -> tuple[SkillSnapshot, ...]:
    """Snapshot the explicit request composition in its declared order."""

    if type(request) is not DeploymentRequest:
        raise SourceSnapshotError("request must be a DeploymentRequest")
    primary = SkillSelection(
        name=request.primary_workflow,
        source_kind="hub-workflow",
        source=f"workflows/{request.primary_workflow}",
        reason="primary workflow selected for this deployment",
    )
    selections = (primary, *request.related_workflows, *request.auxiliary_skills)
    return tuple(snapshot_skill(hub_root, item) for item in selections)


def copy_snapshot(
    snapshot: SkillSnapshot,
    source: Path,
    destination: Path,
) -> None:
    """Copy verified source bytes without links or unlisted files."""

    if type(snapshot) is not SkillSnapshot:
        raise SourceSnapshotError("snapshot must be a SkillSnapshot")
    _assert_absolute(source, "source")
    _assert_absolute(destination, "destination")
    _assert_no_reparse_ancestry(source)
    _assert_no_reparse_ancestry(destination)
    try:
        resolved_source = source.resolve(strict=True)
        resolved_destination = destination.resolve(strict=False)
    except OSError as exc:
        raise SourceSnapshotError(f"cannot resolve copy paths: {exc}") from None
    if not resolved_source.is_dir():
        raise SourceSnapshotError("snapshot source must be a directory")
    if destination.exists() or destination.is_symlink():
        raise SourceSnapshotError("snapshot destination must not already exist")
    if resolved_destination == resolved_source or resolved_source in resolved_destination.parents:
        raise SourceSnapshotError("snapshot destination cannot be inside its source")

    payloads: list[tuple[SkillFile, bytes]] = []
    for record in snapshot.files:
        source_file = resolved_source.joinpath(*record.relative_path.split("/"))
        try:
            source_file.relative_to(resolved_source)
        except ValueError:
            raise SourceSnapshotError("snapshot file escaped its source") from None
        _assert_no_reparse_ancestry(source_file)
        content, _ = _stable_read(source_file)
        if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.sha256:
            raise SourceDriftError(
                f"source no longer matches snapshot: {record.relative_path}"
            )
        payloads.append((record, content))

    destination.mkdir(parents=True, exist_ok=False)
    _assert_no_reparse_ancestry(destination)
    for record, content in payloads:
        target = destination.joinpath(*record.relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        _assert_no_reparse_ancestry(target.parent)
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except OSError as exc:
            raise SourceSnapshotError(f"cannot copy snapshot file {target}: {exc}") from None


__all__ = [
    "SourceDriftError",
    "SourceSnapshotError",
    "copy_snapshot",
    "resolve_skill_source",
    "snapshot_composition",
    "snapshot_skill",
]
