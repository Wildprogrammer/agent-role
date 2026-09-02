from __future__ import annotations

import hmac
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .contracts import ContractError, SkillContract, validate_skill
from .frontmatter import FrontmatterError, parse_markdown_text
from .repository import (
    TextSnapshot,
    _directory_identity,
    _open_windows_directory_guard,
    _stable_file_state,
    load_text_snapshot,
)

_FINGERPRINT = re.compile(r"[0-9a-f]{64}", re.ASCII)
_METADATA_FIELDS = frozenset({"source-path", "source-fingerprint"})
_FRONTMATTER_FIELDS = frozenset({"name", "description", "metadata"})
_STALE = "stale shim; rebuild registration"
_BODY = """# Registered workflow shim

Read the canonical source named by `metadata.source-path` before acting.
Verify its SHA-256 against `metadata.source-fingerprint`.
On any mismatch, stop and rebuild this registration.
"""


class RegistrationError(ValueError):
    pass


@dataclass(frozen=True)
class _SafeWriteResult:
    file_identity: tuple[int, int]
    parent_identity: tuple[int, int]


def _reject_unsafe_path_text(path: Path, message: str) -> None:
    try:
        text = os.fspath(path)
        text.encode("utf-8", errors="strict")
    except (TypeError, UnicodeError, ValueError):
        raise RegistrationError(message) from None
    if not text or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in text
    ):
        raise RegistrationError(message)


def _absolute_path(path: Path, message: str) -> Path:
    if not isinstance(path, Path):
        raise RegistrationError(message)
    _reject_unsafe_path_text(path, message)
    try:
        absolute = Path(os.path.abspath(path))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise RegistrationError(message) from None
    _reject_unsafe_path_text(absolute, message)
    return absolute


def _is_reparse(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(
        file_attributes & reparse_flag
    )


def _existing_components_are_safe(path: Path) -> bool:
    current = Path(path.anchor)
    try:
        parts = path.parts[1:] if path.anchor else path.parts
        for part in parts:
            current /= part
            try:
                current_stat = os.lstat(current)
            except FileNotFoundError:
                break
            if _is_reparse(current_stat):
                return False
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False
    return True


def _load_source(source: Path) -> tuple[Path, TextSnapshot, SkillContract]:
    source = _absolute_path(source, "invalid source skill")
    if (
        source.name != "SKILL.md"
        or not _existing_components_are_safe(source)
    ):
        raise RegistrationError("invalid source skill")
    try:
        snapshot = load_text_snapshot(
            Path(source.anchor),
            source,
            "source Skill",
        )
        frontmatter, body = parse_markdown_text(snapshot.content, source)
        contract = validate_skill(source, frontmatter, body)
    except (ContractError, FrontmatterError):
        raise RegistrationError("invalid source skill") from None
    return source, snapshot, contract


def fingerprint_skill(source: Path) -> str:
    _, snapshot, _ = _load_source(source)
    return snapshot.sha256


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(
        os.path.normpath(second)
    )


def _validate_target(source: Path, slug: str, target: Path) -> Path:
    target = _absolute_path(target, "invalid target path")
    host_root = target.parent.parent
    try:
        home = Path.home().absolute()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise RegistrationError("invalid target path") from None
    filesystem_root = Path(target.anchor)
    if (
        target.name != "SKILL.md"
        or target.parent.name != slug
        or not host_root.name
        or _same_path(target, source)
        or _same_path(host_root, filesystem_root)
        or _same_path(host_root, home)
    ):
        raise RegistrationError("invalid target path")
    try:
        os.lstat(target)
    except FileNotFoundError:
        pass
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise RegistrationError("invalid target path") from None
    else:
        raise RegistrationError("target already exists")
    if not _existing_components_are_safe(target):
        raise RegistrationError("invalid target path")
    return target


def _description(slug: str) -> str:
    return f"Registered pointer to the canonical {slug} Skill."


def _shim_text(slug: str, source: Path, digest: str) -> str:
    source_value = json.dumps(source.as_posix(), ensure_ascii=False)
    fingerprint_value = json.dumps(digest)
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {_description(slug)}\n"
        "metadata:\n"
        f"  source-path: {source_value}\n"
        f"  source-fingerprint: {fingerprint_value}\n"
        "---\n"
        f"{_BODY}"
    )


def _file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _close_descriptors(descriptors: list[int]) -> None:
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _remove_own_partial(target: Path, identity: tuple[int, int] | None) -> None:
    if identity is None:
        return
    try:
        target_stat = os.lstat(target)
        if (
            stat.S_ISREG(target_stat.st_mode)
            and not _is_reparse(target_stat)
            and _file_identity(target_stat) == identity
        ):
            target.unlink()
    except (FileNotFoundError, OSError, RuntimeError, UnicodeError, ValueError):
        pass


def _remove_posix_partial(
    parent_descriptor: int,
    filename: str,
    identity: tuple[int, int] | None,
) -> None:
    if identity is None:
        return
    try:
        target_stat = os.stat(
            filename,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            stat.S_ISREG(target_stat.st_mode)
            and not _is_reparse(target_stat)
            and _file_identity(target_stat) == identity
        ):
            os.unlink(filename, dir_fd=parent_descriptor)
    except (FileNotFoundError, OSError, RuntimeError, UnicodeError, ValueError):
        pass


def _guard_windows_directory(directory: Path) -> int:
    try:
        directory_stat = os.lstat(directory)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise RegistrationError("invalid target path") from None
    if _is_reparse(directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
        raise RegistrationError("invalid target path")
    descriptor, issue = _open_windows_directory_guard(
        directory,
        _directory_identity(directory_stat),
    )
    if issue is not None or descriptor is None:
        raise RegistrationError("invalid target path")
    return descriptor


def _guard_windows_target_parent(target: Path) -> list[int]:
    descriptors: list[int] = []
    current = Path(target.anchor)
    try:
        descriptors.append(_guard_windows_directory(current))
        for part in target.parent.parts[1:]:
            current /= part
            try:
                current_stat = os.lstat(current)
            except FileNotFoundError:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                current_stat = os.lstat(current)
            except (OSError, RuntimeError, UnicodeError, ValueError):
                raise RegistrationError("invalid target path") from None
            if (
                _is_reparse(current_stat)
                or not stat.S_ISDIR(current_stat.st_mode)
            ):
                raise RegistrationError("invalid target path")
            descriptors.append(_guard_windows_directory(current))
    except Exception:
        _close_descriptors(descriptors)
        raise
    return descriptors


def _validate_created_path(target: Path, identity: tuple[int, int]) -> None:
    target_stat = os.lstat(target)
    if (
        _is_reparse(target_stat)
        or not stat.S_ISREG(target_stat.st_mode)
        or _file_identity(target_stat) != identity
    ):
        raise OSError("target identity changed during write")


def _write_target_windows(target: Path, text: str) -> _SafeWriteResult:
    descriptors = _guard_windows_target_parent(target)
    identity: tuple[int, int] | None = None
    try:
        parent_identity = _file_identity(os.fstat(descriptors[-1]))
        with target.open("x", encoding="utf-8", newline="\n") as handle:
            created_stat = os.fstat(handle.fileno())
            if _is_reparse(created_stat) or not stat.S_ISREG(
                created_stat.st_mode
            ):
                raise OSError("exclusive target is not a regular file")
            identity = _file_identity(created_stat)
            _validate_created_path(target, identity)
            handle.write(text)
        _validate_created_path(target, identity)
        return _SafeWriteResult(identity, parent_identity)
    except Exception:
        _remove_own_partial(target, identity)
        raise
    finally:
        _close_descriptors(descriptors)


def _create_directory_windows(target: Path) -> tuple[int, int]:
    descriptors = _guard_windows_target_parent(target)
    identity: tuple[int, int] | None = None
    try:
        target.mkdir()
        created_stat = os.lstat(target)
        if _is_reparse(created_stat) or not stat.S_ISDIR(created_stat.st_mode):
            raise OSError("exclusive target is not a regular directory")
        identity = _file_identity(created_stat)
        target_descriptor = _guard_windows_directory(target)
        descriptors.append(target_descriptor)
        if _file_identity(os.fstat(target_descriptor)) != identity:
            raise OSError("target directory changed during guard")
    except Exception:
        _close_descriptors(descriptors)
        # CreateDirectory/mkdir does not return an ownership-proving handle.
        # A path observed afterward may be an attacker replacement, so rollback
        # deliberately leaves unproven directories in place.
        raise
    _close_descriptors(descriptors)
    return identity


def _open_posix_directory_root(root: Path) -> int:
    if not (
        os.open in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    ):
        raise OSError("safe target filesystem backend is unavailable")
    descriptor = os.open(
        root,
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0),
    )
    root_stat = os.fstat(descriptor)
    if _is_reparse(root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        os.close(descriptor)
        raise OSError("target root is unsafe")
    return descriptor


def _guard_posix_target_parent(target: Path) -> tuple[int, list[int]]:
    descriptors: list[int] = []
    try:
        root_descriptor = _open_posix_directory_root(Path(target.anchor))
        descriptors.append(root_descriptor)
        current_descriptor = root_descriptor
        for part in target.parent.parts[1:]:
            try:
                part_stat = os.stat(
                    part,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                try:
                    os.mkdir(part, dir_fd=current_descriptor)
                except FileExistsError:
                    pass
                part_stat = os.stat(
                    part,
                    dir_fd=current_descriptor,
                    follow_symlinks=False,
                )
            if _is_reparse(part_stat) or not stat.S_ISDIR(part_stat.st_mode):
                raise OSError("unsafe target directory component")
            child_descriptor = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=current_descriptor,
            )
            child_stat = os.fstat(child_descriptor)
            if _file_identity(child_stat) != _file_identity(part_stat):
                os.close(child_descriptor)
                raise OSError("target directory changed during guard")
            descriptors.append(child_descriptor)
            current_descriptor = child_descriptor
    except Exception:
        _close_descriptors(descriptors)
        raise
    return current_descriptor, descriptors


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("target write made no progress")
        view = view[written:]


def _write_target_posix(target: Path, text: str) -> _SafeWriteResult:
    parent_descriptor, descriptors = _guard_posix_target_parent(target)
    file_descriptor: int | None = None
    identity: tuple[int, int] | None = None
    try:
        parent_identity = _file_identity(os.fstat(parent_descriptor))
        file_descriptor = os.open(
            target.name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            0o666,
            dir_fd=parent_descriptor,
        )
        created_stat = os.fstat(file_descriptor)
        if _is_reparse(created_stat) or not stat.S_ISREG(created_stat.st_mode):
            raise OSError("exclusive target is not a regular file")
        identity = _file_identity(created_stat)
        _write_all(file_descriptor, text.encode("utf-8"))
        final_handle_stat = os.fstat(file_descriptor)
        if (
            _is_reparse(final_handle_stat)
            or not stat.S_ISREG(final_handle_stat.st_mode)
            or _file_identity(final_handle_stat) != identity
        ):
            raise OSError("target identity changed during write")
        _validate_created_path(target, identity)
        return _SafeWriteResult(identity, parent_identity)
    except Exception:
        _remove_posix_partial(parent_descriptor, target.name, identity)
        raise
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        _close_descriptors(descriptors)


def _create_directory_posix(target: Path) -> tuple[int, int]:
    parent_descriptor, descriptors = _guard_posix_target_parent(target)
    child_descriptor: int | None = None
    identity: tuple[int, int] | None = None
    try:
        os.mkdir(target.name, 0o777, dir_fd=parent_descriptor)
        created_stat = os.stat(
            target.name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _is_reparse(created_stat) or not stat.S_ISDIR(created_stat.st_mode):
            raise OSError("exclusive target is not a regular directory")
        identity = _file_identity(created_stat)
        child_descriptor = os.open(
            target.name,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        if _file_identity(os.fstat(child_descriptor)) != identity:
            raise OSError("target directory changed during guard")
    except Exception:
        if child_descriptor is not None:
            os.close(child_descriptor)
        _close_descriptors(descriptors)
        # mkdir does not return an ownership-proving descriptor. Never unlink
        # a directory merely because its post-mkdir path identity was observed.
        raise
    if child_descriptor is not None:
        os.close(child_descriptor)
    _close_descriptors(descriptors)
    return identity


def _write_target_safely(target: Path, text: str) -> _SafeWriteResult:
    if os.name == "nt":
        return _write_target_windows(target, text)
    return _write_target_posix(target, text)


def _create_directory_safely(target: Path) -> tuple[int, int]:
    if os.name == "nt":
        return _create_directory_windows(target)
    return _create_directory_posix(target)


def build_shim(source: Path, target: Path) -> None:
    source, snapshot, contract = _load_source(source)
    target = _validate_target(source, contract.name, target)
    text = _shim_text(contract.name, source, snapshot.sha256)
    try:
        _write_target_safely(target, text)
    except FileExistsError:
        raise RegistrationError("target already exists") from None
    except RegistrationError:
        raise
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise RegistrationError("could not build shim") from None


def _load_shim(shim: Path) -> tuple[Path, SkillContract]:
    shim = _absolute_path(shim, "invalid shim metadata")
    if (
        shim.name != "SKILL.md"
        or not _existing_components_are_safe(shim)
    ):
        raise RegistrationError("invalid shim metadata")
    try:
        snapshot = load_text_snapshot(
            Path(shim.anchor),
            shim,
            "registration shim",
        )
        frontmatter, body = parse_markdown_text(snapshot.content, shim)
        contract = validate_skill(shim, frontmatter, body)
    except (ContractError, FrontmatterError):
        raise RegistrationError("invalid shim metadata") from None
    if (
        set(frontmatter) != _FRONTMATTER_FIELDS
        or contract.description != _description(contract.name)
        or contract.body != _BODY.rstrip("\n")
        or set(contract.metadata) != _METADATA_FIELDS
    ):
        raise RegistrationError("invalid shim metadata")
    return shim, contract


def _source_from_metadata(contract: SkillContract) -> tuple[Path, str]:
    source_text = contract.metadata.get("source-path")
    digest = contract.metadata.get("source-fingerprint")
    if (
        not isinstance(source_text, str)
        or not isinstance(digest, str)
        or not _FINGERPRINT.fullmatch(digest)
        or "\\" in source_text
    ):
        raise RegistrationError("invalid shim metadata")
    try:
        source = Path(source_text)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise RegistrationError("invalid shim metadata") from None
    _reject_unsafe_path_text(source, "invalid shim metadata")
    if (
        not source.is_absolute()
        or source.absolute().as_posix() != source_text
        or source.name != "SKILL.md"
        or source.parent.name != contract.name
    ):
        raise RegistrationError("invalid shim metadata")
    return source, digest


def verify_shim(shim: Path) -> None:
    _, contract = _load_shim(shim)
    source, expected_digest = _source_from_metadata(contract)
    try:
        _, snapshot, source_contract = _load_source(source)
    except RegistrationError:
        raise RegistrationError(_STALE) from None
    if (
        source_contract.name != contract.name
        or not hmac.compare_digest(snapshot.sha256, expected_digest)
    ):
        raise RegistrationError(_STALE)
