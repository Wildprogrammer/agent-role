from __future__ import annotations

import hashlib
import hmac
import ntpath
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .catalog import ConfigTemplateDescriptor, RepositoryCatalog
from .registration import (
    _SafeWriteResult,
    _create_directory_safely,
    _existing_components_are_safe,
    _file_identity,
    _is_reparse,
    _remove_own_partial,
    _write_target_safely,
)
from .repository import (
    _directory_identity as _repository_directory_identity,
    _open_windows_directory_guard,
)


_POLICY_FILE = re.compile(r"(?m)^(?P<prefix>[ \t]*policy_file[ \t]*=[ \t]*).*$")
_WINDOWS_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


class ConfigInitError(ValueError):
    pass


@dataclass(frozen=True)
class InitializedConfig:
    workflow: str
    target: Path
    files: tuple[Path, ...]


def _same_path(first: Path, second: Path) -> bool:
    return os.path.normcase(os.path.normpath(first)) == os.path.normcase(
        os.path.normpath(second)
    )


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        common = os.path.commonpath((candidate, root))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False
    return _same_path(Path(common), root)


def _windows_final_directory_path(directory: Path) -> str:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    try:
        directory_stat = os.lstat(directory)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise ConfigInitError("invalid configuration target") from None
    if _is_reparse(directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
        raise ConfigInitError("invalid configuration target")
    descriptor, issue = _open_windows_directory_guard(
        directory, _repository_directory_identity(directory_stat)
    )
    if issue is not None or descriptor is None:
        raise ConfigInitError("invalid configuration target")
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = (
            wintypes.HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        get_final_path.restype = wintypes.DWORD
        handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
        required = get_final_path(handle, None, 0, 0)
        if required == 0:
            raise ConfigInitError("invalid configuration target")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = get_final_path(handle, buffer, len(buffer), 0)
        if written == 0 or written >= len(buffer):
            raise ConfigInitError("invalid configuration target")
        return ntpath.normcase(ntpath.normpath(buffer.value))
    finally:
        os.close(descriptor)


def _nearest_existing_parent(path: Path) -> Path:
    current = path.parent
    while True:
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                raise ConfigInitError("invalid configuration target")
            current = parent
            continue
        except (OSError, RuntimeError, UnicodeError, ValueError):
            raise ConfigInitError("invalid configuration target") from None
        if _is_reparse(current_stat) or not stat.S_ISDIR(current_stat.st_mode):
            raise ConfigInitError("invalid configuration target")
        return current


def _windows_canonical_within(candidate: str, root: str) -> bool:
    try:
        common = ntpath.commonpath((candidate, root))
    except (OSError, RuntimeError, UnicodeError, ValueError):
        return False
    return ntpath.normcase(ntpath.normpath(common)) == ntpath.normcase(
        ntpath.normpath(root)
    )


def _is_proven_repository_external(path: Path, root: Path) -> bool:
    if _is_within(path, root):
        return False
    if os.name != "nt":
        return True
    root_final = _windows_final_directory_path(root)
    existing_parent = _nearest_existing_parent(path)
    parent_final = _windows_final_directory_path(existing_parent)
    return not _windows_canonical_within(parent_final, root_final)


def _safe_absolute_target(target: Path) -> Path:
    if not isinstance(target, Path) or not target.is_absolute():
        raise ConfigInitError("invalid configuration target")
    try:
        raw = os.fspath(target)
        raw.encode("utf-8", errors="strict")
        absolute = Path(os.path.abspath(target))
        absolute_text = os.fspath(absolute)
        absolute_text.encode("utf-8", errors="strict")
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        raise ConfigInitError("invalid configuration target") from None
    if not raw or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in absolute_text
    ):
        raise ConfigInitError("invalid configuration target")
    if _unsafe_windows_target_text(raw) or _unsafe_windows_target_text(
        absolute_text
    ):
        raise ConfigInitError("invalid configuration target")
    return absolute


def _unsafe_windows_target_text(value: str) -> bool:
    normalized = value.replace("/", "\\")
    folded = normalized.casefold()
    if folded.startswith(
        (
            "\\\\?\\",
            "\\\\.\\",
            "\\??\\",
            "\\\\??\\",
        )
    ):
        return True
    looks_windows = (
        os.name == "nt"
        or bool(re.match(r"^[A-Za-z]:\\", normalized))
        or "\\" in value
    )
    if not looks_windows:
        return False
    _, tail = ntpath.splitdrive(normalized)
    return any(":" in component for component in tail.split("\\"))


def _safe_output_name(template: ConfigTemplateDescriptor) -> str:
    name = template.output_name.removesuffix(".example")
    if (
        not name
        or name in {".", ".."}
        or name[-1] in {" ", "."}
        or any(character in name for character in '<>:"/\\|?*')
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in name
        )
        or name.split(".", 1)[0].casefold() in _WINDOWS_RESERVED
    ):
        raise ConfigInitError("invalid configuration template descriptor")
    return name


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        directory_stat = os.lstat(path)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise ConfigInitError("invalid configuration target") from None
    if _is_reparse(directory_stat) or not stat.S_ISDIR(directory_stat.st_mode):
        raise ConfigInitError("invalid configuration target")
    return _file_identity(directory_stat)


def _require_directory_identity(
    path: Path, expected: tuple[int, int]
) -> None:
    try:
        current_stat = os.lstat(path)
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise ConfigInitError(
            "configuration target changed during initialization"
        ) from None
    if (
        _is_reparse(current_stat)
        or not stat.S_ISDIR(current_stat.st_mode)
        or _file_identity(current_stat) != expected
    ):
        raise ConfigInitError(
            "configuration target changed during initialization"
        )


def _validated_templates(
    templates: tuple[ConfigTemplateDescriptor, ...],
) -> tuple[tuple[ConfigTemplateDescriptor, str, str], ...]:
    validated: list[tuple[ConfigTemplateDescriptor, str, str]] = []
    names: set[str] = set()
    for template in templates:
        name = _safe_output_name(template)
        platform_name = os.path.normcase(name)
        if platform_name in names:
            raise ConfigInitError("duplicate configuration output name")
        names.add(platform_name)
        digest = hashlib.sha256(template.content.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(digest, template.sha256):
            raise ConfigInitError("invalid configuration template descriptor")
        validated.append((template, name, template.content))
    return tuple(validated)


def _jenkins_content(
    workflow: str,
    templates: tuple[tuple[ConfigTemplateDescriptor, str, str], ...],
) -> tuple[tuple[ConfigTemplateDescriptor, str, str], ...]:
    if workflow != "jenkins-operations":
        return templates
    policy_names = [
        name for template, name, _ in templates if template.label == "policy"
    ]
    if len(policy_names) != 1:
        raise ConfigInitError("invalid configuration template descriptor")
    policy_name = policy_names[0]
    adjusted: list[tuple[ConfigTemplateDescriptor, str, str]] = []
    for template, name, content in templates:
        if template.label == "main":
            content, replacements = _POLICY_FILE.subn(
                rf"\g<prefix>{policy_name}", content
            )
            if replacements != 1:
                raise ConfigInitError("invalid configuration template descriptor")
        adjusted.append((template, name, content))
    return tuple(adjusted)


def _preflight_absent(paths: tuple[Path, ...]) -> None:
    for path in paths:
        try:
            path_stat = os.lstat(path)
        except FileNotFoundError:
            continue
        except (OSError, RuntimeError, UnicodeError, ValueError):
            raise ConfigInitError("invalid configuration target") from None
        if _is_reparse(path_stat):
            raise ConfigInitError("invalid configuration target")
        raise ConfigInitError("configuration target already exists")


def initialize_config(
    catalog: RepositoryCatalog, workflow: str, target: Path
) -> InitializedConfig:
    descriptor = next(
        (item for item in catalog.workflows if item.name == workflow), None
    )
    if descriptor is None:
        raise ConfigInitError("unknown workflow")
    if not descriptor.config_templates:
        raise ConfigInitError("workflow has no configuration templates")

    target = _safe_absolute_target(target)
    root = _safe_absolute_target(catalog.root)
    filesystem_root = Path(target.anchor)
    try:
        home = Path.home().absolute()
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise ConfigInitError("invalid configuration target") from None
    if (
        _same_path(target, filesystem_root)
        or _same_path(target, home)
        or _same_path(target, root)
        or not _existing_components_are_safe(target)
    ):
        raise ConfigInitError("invalid configuration target")

    templates = _jenkins_content(
        workflow, _validated_templates(descriptor.config_templates)
    )
    try:
        target_stat = os.lstat(target)
    except FileNotFoundError:
        target_stat = None
    except (OSError, RuntimeError, UnicodeError, ValueError):
        raise ConfigInitError("invalid configuration target") from None

    existing_directory = False
    package_directory = False
    package_identity: tuple[int, int] | None = None
    if target_stat is not None:
        if _is_reparse(target_stat):
            raise ConfigInitError("invalid configuration target")
        if not stat.S_ISDIR(target_stat.st_mode):
            raise ConfigInitError("configuration target already exists")
        existing_directory = True
        package_directory = True
        package_identity = _directory_identity(target)
        files = tuple(target / name for _, name, _ in templates)
    elif len(templates) == 1:
        files = (target,)
    else:
        package_directory = True
        files = tuple(target / name for _, name, _ in templates)

    if any(
        template.scope == "repository-external"
        and not _is_proven_repository_external(path, root)
        for (template, _, _), path in zip(templates, files, strict=True)
    ):
        raise ConfigInitError("configuration target must be repository-external")
    _preflight_absent(files)

    created: list[tuple[Path, tuple[int, int]]] = []
    try:
        if package_directory and not existing_directory:
            package_identity = _create_directory_safely(target)
        for (_, _, content), path in zip(templates, files, strict=True):
            if package_identity is not None:
                _require_directory_identity(target, package_identity)
            result: _SafeWriteResult = _write_target_safely(path, content)
            created.append((path, result.file_identity))
            if package_directory and package_identity is None:
                package_identity = result.parent_identity
                _require_directory_identity(target, package_identity)
            elif (
                package_identity is not None
                and result.parent_identity != package_identity
            ):
                raise ConfigInitError(
                    "configuration target changed during initialization"
                )
            elif package_identity is not None:
                _require_directory_identity(target, package_identity)
        if package_identity is not None:
            _require_directory_identity(target, package_identity)
    except ConfigInitError:
        for path, identity in reversed(created):
            _remove_own_partial(path, identity)
        # mkdir yields no stable ownership handle. Keep all directory paths;
        # deleting a post-mkdir identity could remove another actor's swap-in.
        raise
    except FileExistsError:
        for path, identity in reversed(created):
            _remove_own_partial(path, identity)
        raise ConfigInitError("configuration target already exists") from None
    except (OSError, RuntimeError, UnicodeError, ValueError):
        for path, identity in reversed(created):
            _remove_own_partial(path, identity)
        raise ConfigInitError("could not initialize configuration") from None

    return InitializedConfig(workflow, target, files)
