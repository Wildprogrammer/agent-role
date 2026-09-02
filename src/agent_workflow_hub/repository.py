from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from .catalog import (
    ConfigTemplateDescriptor,
    RepositoryCatalog,
    WorkflowDescriptor,
)
from .contracts import (
    CapabilityContract,
    ConfigRequirement,
    ContractError,
    SkillContract,
    validate_capability,
    validate_skill,
    workflow_config_requirements,
    workflow_config_templates,
    workflow_entrypoints,
)
from .frontmatter import FrontmatterError, parse_markdown, parse_markdown_text  # noqa: F401
from .support import PROJECT_HOSTS

REQUIRED_HEADINGS = (
    "用途与触发条件",
    "非目标",
    "输入",
    "输出与命名规则",
    "依赖和运行前检查",
    "系统修改与权限影响",
    "执行步骤",
    "人工确认门",
    "失败恢复",
    "重跑、幂等与覆盖策略",
    "验收标准",
    "清理方式",
)
REQUIRED_CAPABILITY_HEADINGS = (
    "能力用途和非目标",
    "官方获取与文档",
    "系统、架构、运行时和硬件支持",
    "五种宿主兼容矩阵",
    "只读检测",
    "各系统安装",
    "调用示例和成功判据",
    "权限、网络、数据和遥测",
    "卸载或回滚",
    "已知限制",
    "替代能力",
)
REQUIRED_WORKFLOW_METADATA = (
    "spec-version",
    "workflow-version",
    "execution-modes",
)
SUPPORTED_SPEC_VERSION = "1.0"
SEMANTIC_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
ATX_LEVEL_TWO = re.compile(r"^ {0,3}##(?:[ \t]+|$)(.*)$")
HTML_COMMENT_OPEN = re.compile(r"^ {0,3}<!--")
HTML_PROCESSING_INSTRUCTION_OPEN = re.compile(r"^ {0,3}<\?")
HTML_DECLARATION_OPEN = re.compile(r"^ {0,3}<![A-Z]")
HTML_CDATA_OPEN = re.compile(r"^ {0,3}<!\[CDATA\[")
HTML_MARKER_BLOCKS = (
    (HTML_COMMENT_OPEN, re.compile(r"-->")),
    (HTML_PROCESSING_INSTRUCTION_OPEN, re.compile(r"\?>")),
    (HTML_DECLARATION_OPEN, re.compile(r">")),
    (HTML_CDATA_OPEN, re.compile(r"\]\]>")),
)
RAW_HTML_OPEN = re.compile(
    r"^ {0,3}<(?P<tag>script|pre|style|textarea)(?=[\s>]|$)",
    re.IGNORECASE,
)
HTML_TAG_PREFIX = re.compile(
    r"^ {0,3}</?(?P<tag>[A-Za-z][A-Za-z0-9-]*)(?=[\s/>]|$)",
)
GENERIC_HTML_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "base",
        "basefont",
        "blockquote",
        "body",
        "caption",
        "center",
        "col",
        "colgroup",
        "dd",
        "details",
        "dialog",
        "dir",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "frame",
        "frameset",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "head",
        "header",
        "hgroup",
        "hr",
        "html",
        "iframe",
        "legend",
        "li",
        "link",
        "main",
        "menu",
        "menuitem",
        "nav",
        "noframes",
        "ol",
        "optgroup",
        "option",
        "p",
        "param",
        "search",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "title",
        "tr",
        "track",
        "ul",
    }
)
HTML_ATTRIBUTE = (
    r"[A-Za-z_:][A-Za-z0-9_.:-]*"
    r"(?:[ \t]*=[ \t]*(?:[^ \"'=<>`]+|'[^']*'|\"[^\"]*\"))?"
)
GENERIC_HTML_BLOCK = re.compile(
    rf"^ {{0,3}}(?:"
    rf"<[A-Za-z][A-Za-z0-9-]*(?:[ \t]+{HTML_ATTRIBUTE})*[ \t]*/?>"
    rf"|</[A-Za-z][A-Za-z0-9-]*[ \t]*>"
    rf")[ \t]*$"
)
WINDOWS_INVALID_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "clock$",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com\u00b9",
        "com\u00b2",
        "com\u00b3",
        "lpt\u00b9",
        "lpt\u00b2",
        "lpt\u00b3",
    }
)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: Path
    message: str


@dataclass(frozen=True)
class TextSnapshot:
    path: Path
    sha256: str
    content: str
    device: int
    inode: int
    size: int
    mtime_ns: int


RoleSnapshot = TextSnapshot


@dataclass(frozen=True)
class _WorkflowMetadataSnapshot:
    supported_hosts: frozenset[str]
    config_templates: Mapping[str, str]
    config_requirements: Mapping[str, ConfigRequirement]
    template_snapshots: Mapping[str, TextSnapshot]
    entrypoints: Mapping[str, str]


@dataclass(frozen=True)
class LoadedRepository:
    issues: tuple[ValidationIssue, ...]
    catalog: RepositoryCatalog


def _headings(body: str) -> set[str]:
    headings: set[str] = set()
    fence_marker: str | None = None
    fence_length = 0
    html_end_pattern: re.Pattern[str] | None = None
    html_until_blank = False

    for line in body.splitlines():
        if fence_marker is not None:
            closing_fence = re.fullmatch(
                rf" {{0,3}}{re.escape(fence_marker)}"
                rf"{{{fence_length},}}[ \t]*",
                line,
            )
            if closing_fence:
                fence_marker = None
                fence_length = 0
            continue

        if html_end_pattern is not None:
            if html_end_pattern.search(line):
                html_end_pattern = None
            continue

        if html_until_blank:
            if not line.strip():
                html_until_blank = False
            continue

        opening_fence = FENCE_OPEN.match(line)
        if opening_fence:
            markers = opening_fence.group(1)
            fence_marker = markers[0]
            fence_length = len(markers)
            continue

        raw_html_opening = RAW_HTML_OPEN.match(line)
        if raw_html_opening:
            tag = raw_html_opening.group("tag").casefold()
            closing_pattern = re.compile(
                rf"</{re.escape(tag)}[ \t]*>",
                re.IGNORECASE,
            )
            if not closing_pattern.search(line[raw_html_opening.end() :]):
                html_end_pattern = closing_pattern
            continue

        matched_marker_block = False
        for opening_pattern, closing_pattern in HTML_MARKER_BLOCKS:
            opening = opening_pattern.match(line)
            if opening is None:
                continue
            if not closing_pattern.search(line[opening.end() :]):
                html_end_pattern = closing_pattern
            matched_marker_block = True
            break
        if matched_marker_block:
            continue

        html_tag = HTML_TAG_PREFIX.match(line)
        if html_tag:
            tag = html_tag.group("tag").casefold()
            if tag in GENERIC_HTML_BLOCK_TAGS:
                html_until_blank = True
                continue

        if GENERIC_HTML_BLOCK.fullmatch(line):
            html_until_blank = True
            continue

        heading_match = ATX_LEVEL_TWO.match(line)
        if heading_match:
            heading = re.sub(
                r"[ \t]+#+[ \t]*$",
                "",
                heading_match.group(1),
            ).strip()
            headings.add(heading)

    return headings


def _role_reference_parts(reference: str) -> tuple[str, str]:
    segments = reference.split("/")
    portable_path = PurePosixPath(reference)
    filename = segments[-1] if segments else ""
    reserved_basename = filename.split(".", 1)[0].casefold()
    if (
        "\\" in reference
        or portable_path.is_absolute()
        or len(portable_path.parts) != 2
        or len(segments) != 2
        or segments[0] != "roles"
        or any(segment in {"", ".", ".."} for segment in segments)
        or not filename.endswith(".md")
        or not filename.removesuffix(".md")
        or filename.endswith((".", " "))
        or any(
            character in WINDOWS_INVALID_FILENAME_CHARACTERS
            or ord(character) < 32
            for character in filename
        )
        or reserved_basename in WINDOWS_RESERVED_BASENAMES
    ):
        raise ContractError(
            f"metadata.roles reference {reference!r} must be a portable "
            "relative POSIX path shaped roles/<filename>.md using a "
            "Windows-portable basename"
        )
    return segments[0], segments[1]


def _stat_has_reparse_point(file_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(file_attributes & reparse_flag)


def _stable_file_state(file_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_size,
        file_stat.st_mtime_ns,
    )


def _directory_identity(directory_stat: os.stat_result) -> tuple[int, int]:
    return directory_stat.st_dev, directory_stat.st_ino


def load_text_snapshot(
    anchor: Path,
    candidate: Path,
    context: str,
    *,
    max_bytes: int | None = None,
) -> TextSnapshot:
    if max_bytes is not None and (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < 1
    ):
        raise ValueError("max_bytes must be a positive integer")
    try:
        with _GuardedRepository(anchor) as guarded:
            if max_bytes is not None:
                return _load_text_snapshot_guarded(
                    guarded,
                    candidate,
                    context,
                    max_bytes=max_bytes,
                )
            return _load_text_snapshot_guarded(
                guarded,
                candidate,
                context,
            )
    except _SafeFilesystemError:
        raise ContractError(
            f"{context} could not be opened safely"
        ) from None


def _load_text_snapshot_guarded(
    guarded: _GuardedRepository,
    candidate: Path,
    context: str,
    expected_state: tuple[int, int, int, int] | None = None,
    *,
    max_bytes: int | None = None,
) -> TextSnapshot:
    try:
        if max_bytes is not None:
            role_bytes, handle_after = guarded.read_file(
                candidate,
                expected_state=expected_state,
                max_bytes=max_bytes,
            )
        else:
            role_bytes, handle_after = guarded.read_file(
                candidate,
                expected_state=expected_state,
            )
    except _SafeFilesystemError:
        raise ContractError(
            f"{context} could not be opened safely"
        ) from None
    try:
        content = role_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError(
            f"{context} must contain strict UTF-8"
        ) from exc
    return TextSnapshot(
        path=candidate.absolute(),
        sha256=hashlib.sha256(role_bytes).hexdigest(),
        content=content,
        device=handle_after.st_dev,
        inode=handle_after.st_ino,
        size=handle_after.st_size,
        mtime_ns=handle_after.st_mtime_ns,
    )


def load_role_snapshot(
    workflow_skill_path: Path,
    reference: str,
) -> RoleSnapshot:
    role_directory, filename = _role_reference_parts(reference)
    workflow_directory = workflow_skill_path.parent
    candidate = workflow_directory / role_directory / filename
    return load_text_snapshot(
        workflow_directory,
        candidate,
        f"metadata.roles reference {reference!r}",
    )


def workflow_supported_hosts(
    metadata: Mapping[str, str],
    project_hosts: frozenset[str],
) -> frozenset[str]:
    if "supported-hosts" not in metadata:
        return project_hosts

    try:
        decoded_hosts = json.loads(metadata["supported-hosts"])
    except (json.JSONDecodeError, ValueError) as exc:
        raise ContractError(
            "metadata.supported-hosts must be valid JSON containing a non-empty "
            "subset of PROJECT_HOSTS"
        ) from exc
    if (
        not isinstance(decoded_hosts, list)
        or not decoded_hosts
        or not all(isinstance(host, str) for host in decoded_hosts)
        or len(decoded_hosts) != len(set(decoded_hosts))
        or not set(decoded_hosts) <= project_hosts
    ):
        raise ContractError(
            "metadata.supported-hosts must be a non-empty subset of the project "
            "supported hosts"
        )
    return frozenset(decoded_hosts)


def _project_supported_hosts_from_text(
    root: Path,
    root_skill: Path,
    text: str,
) -> frozenset[str]:
    frontmatter, body = parse_markdown_text(text, root_skill)
    synthetic_path = root.parent / "agent-workflow-hub" / "SKILL.md"
    contract = validate_skill(synthetic_path, frontmatter, body)
    if contract.metadata.get("spec-version") != SUPPORTED_SPEC_VERSION:
        raise ContractError(
            "metadata.spec-version is required for the root Skill and must "
            f"equal supported version {SUPPORTED_SPEC_VERSION}"
        )
    if "supported-hosts" not in contract.metadata:
        raise ContractError(
            "metadata.supported-hosts is required for the root Skill"
        )
    return workflow_supported_hosts(contract.metadata, PROJECT_HOSTS)


def project_supported_hosts(
    root: Path,
    guarded: _GuardedRepository | None = None,
) -> frozenset[str]:
    root_skill = root / "SKILL.md"
    if guarded is not None:
        try:
            if "SKILL.md" not in guarded.listdir(root):
                return PROJECT_HOSTS
            root_skill_stat = guarded.lstat(root_skill)
        except _SafeFilesystemError as exc:
            raise ContractError(
                "root Skill could not be inspected safely"
            ) from exc
    else:
        try:
            root_skill_stat = os.lstat(root_skill)
        except FileNotFoundError:
            return PROJECT_HOSTS
        except OSError as exc:
            raise ContractError(
                f"root Skill could not be inspected: {exc}"
            ) from exc
    if not stat.S_ISREG(root_skill_stat.st_mode):
        raise ContractError("root Skill must be a regular file")
    snapshot = (
        _load_text_snapshot_guarded(guarded, root_skill, "root Skill")
        if guarded is not None
        else load_text_snapshot(root, root_skill, "root Skill")
    )
    return _project_supported_hosts_from_text(
        root,
        root_skill,
        snapshot.content,
    )


def _validate_workflow_metadata(
    skill_path: Path,
    metadata: Mapping[str, str],
    project_hosts: frozenset[str],
    guarded: _GuardedRepository | None = None,
) -> _WorkflowMetadataSnapshot:
    for field in REQUIRED_WORKFLOW_METADATA:
        if field not in metadata:
            raise ContractError(f"metadata.{field} is required for workflows")
    if metadata["spec-version"] != SUPPORTED_SPEC_VERSION:
        raise ContractError(
            "metadata.spec-version must equal supported version "
            f"{SUPPORTED_SPEC_VERSION}"
        )
    if not SEMANTIC_VERSION.fullmatch(metadata["workflow-version"]):
        raise ContractError(
            "metadata.workflow-version must be an exact MAJOR.MINOR.PATCH version"
        )

    config_templates = workflow_config_templates(metadata)
    config_requirements = workflow_config_requirements(metadata)
    if set(config_templates) != set(config_requirements):
        raise ContractError(
            "metadata.config-templates and metadata.config-requirements "
            "labels must match exactly"
        )
    entrypoints = workflow_entrypoints(metadata)

    normalized_references: set[tuple[str, ...]] = set()
    template_identities: set[tuple[int, int]] = set()
    template_snapshots: dict[str, TextSnapshot] = {}
    for label, reference in config_templates.items():
        segments = reference.split("/")
        if (
            "\\" in reference
            or reference.startswith("/")
            or re.match(r"^[A-Za-z]:/", reference) is not None
            or any(segment in {"", ".", ".."} for segment in segments)
            or any(
                segment.endswith((".", " "))
                or any(
                    character in WINDOWS_INVALID_FILENAME_CHARACTERS
                    or ord(character) < 32
                    for character in segment
                )
                or segment.split(".", 1)[0].casefold() in WINDOWS_RESERVED_BASENAMES
                for segment in segments
            )
        ):
            raise ContractError(
                "metadata.config-templates reference "
                f"{reference!r} for label {label!r} must be a portable "
                "relative POSIX path without aliases"
            )
        normalized = tuple(segments)
        if normalized in normalized_references:
            raise ContractError(
                "metadata.config-templates contains a duplicate resolved "
                f"reference: {reference!r}"
            )
        normalized_references.add(normalized)
        candidate = skill_path.parent.joinpath(*segments)
        context = (
            f"metadata.config-templates reference {reference!r} for label {label!r}"
        )
        snapshot = (
            load_text_snapshot(skill_path.parent, candidate, context)
            if guarded is None
            else _load_text_snapshot_guarded(guarded, candidate, context)
        )
        identity = (snapshot.device, snapshot.inode)
        if identity != (0, 0):
            if identity in template_identities:
                raise ContractError(
                    "metadata.config-templates contains a duplicate resolved "
                    f"reference: {reference!r}"
                )
            template_identities.add(identity)
        template_snapshots[label] = snapshot

    if "roles" in metadata:
        roles = json.loads(metadata["roles"])
        if not roles:
            raise ContractError(
                "metadata.roles must be omitted or contain a non-empty list"
            )
        for reference in roles:
            if guarded is None:
                load_role_snapshot(skill_path, reference)
            else:
                role_directory, filename = _role_reference_parts(reference)
                _load_text_snapshot_guarded(
                    guarded,
                    skill_path.parent / role_directory / filename,
                    f"metadata.roles reference {reference!r}",
                )

    return _WorkflowMetadataSnapshot(
        supported_hosts=workflow_supported_hosts(metadata, project_hosts),
        config_templates=config_templates,
        config_requirements=config_requirements,
        template_snapshots=MappingProxyType(template_snapshots),
        entrypoints=entrypoints,
    )


def _discovery_issue(
    code: str,
    path: Path,
    message: str,
) -> ValidationIssue:
    return ValidationIssue(code, path, message)


def _changed_directory_issue(
    directory: Path,
    directory_stat: os.stat_result | None,
) -> ValidationIssue:
    if (
        directory_stat is not None
        and _stat_has_reparse_point(directory_stat)
    ):
        return _discovery_issue(
            "unsafe-contract-path",
            directory,
            "Contract discovery does not allow symlinks, junctions, "
            "or reparse points",
        )
    return _discovery_issue(
        "contract-discovery-error",
        directory,
        "Contract directory changed identity during discovery",
    )


def _open_windows_directory_guard(
    directory: Path,
    expected_identity: tuple[int, int],
) -> tuple[int | None, ValidationIssue | None]:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    # List access makes the omitted delete sharing deny rename/replacement.
    file_list_directory = 0x0001
    file_read_attributes = 0x0080
    file_share_read = 0x0001
    file_share_write = 0x0002
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_flag_backup_semantics = 0x02000000

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    raw_handle = create_file(
        str(directory),
        file_list_directory | file_read_attributes,
        file_share_read | file_share_write,
        None,
        open_existing,
        file_flag_open_reparse_point | file_flag_backup_semantics,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if raw_handle == invalid_handle:
        return None, _discovery_issue(
            "contract-discovery-error",
            directory,
            "Could not guard contract directory for enumeration",
        )

    file_descriptor: int | None = None
    try:
        file_descriptor = msvcrt.open_osfhandle(
            int(raw_handle),
            os.O_RDONLY,
        )
        guard_stat = os.fstat(file_descriptor)
        if (
            _stat_has_reparse_point(guard_stat)
            or not stat.S_ISDIR(guard_stat.st_mode)
            or _directory_identity(guard_stat) != expected_identity
        ):
            issue = _changed_directory_issue(directory, guard_stat)
            os.close(file_descriptor)
            return None, issue
    except (OSError, RuntimeError, ValueError):
        if file_descriptor is None:
            close_handle(raw_handle)
        else:
            os.close(file_descriptor)
        return None, _discovery_issue(
            "contract-discovery-error",
            directory,
            "Could not validate guarded contract directory",
        )
    return file_descriptor, None


class _SafeFilesystemError(Exception):
    pass


def _open_windows_file_guard(
    path: Path,
    expected_state: tuple[int, int, int, int],
) -> int:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(path),
        0x80000000 | 0x0080,
        0x0001 | 0x0002,
        None,
        3,
        0x00200000,
        None,
    )
    if raw_handle == wintypes.HANDLE(-1).value:
        raise _SafeFilesystemError("could not open file safely")
    descriptor: int | None = None
    try:
        descriptor = msvcrt.open_osfhandle(
            int(raw_handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        file_stat = os.fstat(descriptor)
        if (
            _stat_has_reparse_point(file_stat)
            or not stat.S_ISREG(file_stat.st_mode)
            or _stable_file_state(file_stat) != expected_state
        ):
            raise _SafeFilesystemError("file identity changed before read")
        return descriptor
    except Exception as exc:
        if descriptor is None:
            close_handle(raw_handle)
        else:
            os.close(descriptor)
        if isinstance(exc, _SafeFilesystemError):
            raise
        raise _SafeFilesystemError("could not validate opened file") from exc


class _GuardedRepository:
    def __init__(self, root: Path):
        self.root = root
        self.root_descriptor: int | None = None

    def __enter__(self) -> _GuardedRepository:
        try:
            root_stat = os.lstat(self.root)
        except (OSError, RuntimeError) as exc:
            raise _SafeFilesystemError("repository root is unavailable") from exc
        if (
            _stat_has_reparse_point(root_stat)
            or not stat.S_ISDIR(root_stat.st_mode)
        ):
            raise _SafeFilesystemError(
                "repository root must be a non-reparse directory"
            )
        expected = _stable_file_state(root_stat)
        if os.name == "nt":
            descriptor, issue = _open_windows_directory_guard(
                self.root,
                _directory_identity(root_stat),
            )
            if issue is not None or descriptor is None:
                raise _SafeFilesystemError(
                    "repository root could not be guarded"
                )
            self.root_descriptor = descriptor
            return self
        if not (
            os.scandir in os.supports_fd
            and os.open in os.supports_dir_fd
            and hasattr(os, "O_DIRECTORY")
            and hasattr(os, "O_NOFOLLOW")
        ):
            raise _SafeFilesystemError(
                "safe repository filesystem backend is unavailable"
            )
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.root,
                os.O_RDONLY
                | os.O_DIRECTORY
                | os.O_NOFOLLOW
                | getattr(os, "O_CLOEXEC", 0),
            )
            opened_stat = os.fstat(descriptor)
        except (OSError, RuntimeError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise _SafeFilesystemError(
                "repository root could not be guarded"
            ) from exc
        if (
            _stat_has_reparse_point(opened_stat)
            or not stat.S_ISDIR(opened_stat.st_mode)
            or _stable_file_state(opened_stat) != expected
        ):
            os.close(descriptor)
            raise _SafeFilesystemError(
                "repository root changed before it was guarded"
            )
        self.root_descriptor = descriptor
        return self

    def __exit__(self, *_args) -> None:
        if self.root_descriptor is not None:
            os.close(self.root_descriptor)
            self.root_descriptor = None

    def relative_parts(self, path: Path) -> tuple[str, ...]:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError as exc:
            raise _SafeFilesystemError(
                "path is outside repository root"
            ) from exc
        if any(part in ("", ".", "..") for part in parts):
            raise _SafeFilesystemError("path is not a safe relative path")
        return parts

    @contextmanager
    def _open_directory(self, parts: tuple[str, ...]):
        if self.root_descriptor is None:
            raise _SafeFilesystemError("repository guard is closed")
        descriptors: list[int] = []
        current_descriptor = self.root_descriptor
        current_path = self.root
        try:
            for part in parts:
                current_path /= part
                if os.name == "nt":
                    try:
                        path_stat = os.lstat(current_path)
                    except (OSError, RuntimeError) as exc:
                        raise _SafeFilesystemError(
                            "could not inspect directory component"
                        ) from exc
                    descriptor, issue = _open_windows_directory_guard(
                        current_path,
                        _directory_identity(path_stat),
                    )
                    if issue is not None or descriptor is None:
                        raise _SafeFilesystemError(
                            "could not guard directory component"
                        )
                else:
                    descriptor = None
                    try:
                        path_stat = os.stat(
                            part,
                            dir_fd=current_descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            _stat_has_reparse_point(path_stat)
                            or not stat.S_ISDIR(path_stat.st_mode)
                        ):
                            raise _SafeFilesystemError(
                                "unsafe directory component"
                            )
                        descriptor = os.open(
                            part,
                            os.O_RDONLY
                            | os.O_DIRECTORY
                            | os.O_NOFOLLOW
                            | getattr(os, "O_CLOEXEC", 0),
                            dir_fd=current_descriptor,
                        )
                        if (
                            _stable_file_state(os.fstat(descriptor))
                            != _stable_file_state(path_stat)
                        ):
                            os.close(descriptor)
                            raise _SafeFilesystemError(
                                "directory component changed"
                            )
                    except (OSError, RuntimeError) as exc:
                        if descriptor is not None:
                            os.close(descriptor)
                        raise _SafeFilesystemError(
                            "could not guard directory component"
                        ) from exc
                descriptors.append(descriptor)
                current_descriptor = descriptor
            yield current_descriptor, current_path
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def listdir(self, directory: Path) -> list[str]:
        parts = self.relative_parts(directory)
        with self._open_directory(parts) as (descriptor, guarded_path):
            try:
                scan_target = guarded_path if os.name == "nt" else descriptor
                with os.scandir(scan_target) as entries:
                    return sorted(
                        (entry.name for entry in entries),
                        key=lambda name: (name.casefold(), name),
                    )
            except (OSError, RuntimeError) as exc:
                raise _SafeFilesystemError(
                    "could not enumerate guarded directory"
                ) from exc

    def lstat(self, path: Path) -> os.stat_result:
        parts = self.relative_parts(path)
        if not parts:
            assert self.root_descriptor is not None
            return os.fstat(self.root_descriptor)
        with self._open_directory(parts[:-1]) as (descriptor, _):
            try:
                if os.name == "nt":
                    return os.lstat(path)
                return os.stat(
                    parts[-1],
                    dir_fd=descriptor,
                    follow_symlinks=False,
                )
            except (OSError, RuntimeError) as exc:
                raise _SafeFilesystemError(
                    "could not inspect guarded path"
                ) from exc

    def posix_link_can_hide_branch(
        self,
        path: Path,
        *,
        is_named_candidate: bool,
    ) -> bool:
        if is_named_candidate:
            return True
        parts = self.relative_parts(path)
        if not parts:
            return True
        with self._open_directory(parts[:-1]) as (parent_descriptor, _):
            try:
                payload = PurePosixPath(
                    os.readlink(parts[-1], dir_fd=parent_descriptor)
                )
            except (OSError, RuntimeError, ValueError):
                return True
            if payload.is_absolute():
                return True
            normalized = list(parts[:-1])
            for part in payload.parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    if not normalized:
                        return True
                    normalized.pop()
                else:
                    normalized.append(part)
            try:
                target_stat = self.lstat(
                    self.root.joinpath(*normalized)
                )
            except _SafeFilesystemError:
                return True
            return (
                stat.S_ISDIR(target_stat.st_mode)
                or _stat_has_reparse_point(target_stat)
            )

    def read_file(
        self,
        path: Path,
        *,
        expected_state: tuple[int, int, int, int] | None = None,
        max_bytes: int | None = None,
    ) -> tuple[bytes, os.stat_result]:
        parts = self.relative_parts(path)
        if not parts:
            raise _SafeFilesystemError("repository root is not a file")
        with self._open_directory(parts[:-1]) as (parent_descriptor, _):
            try:
                if os.name == "nt":
                    pre_stat = os.lstat(path)
                else:
                    pre_stat = os.stat(
                        parts[-1],
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
            except (OSError, RuntimeError) as exc:
                raise _SafeFilesystemError("could not inspect file") from exc
            pre_state = _stable_file_state(pre_stat)
            if (
                _stat_has_reparse_point(pre_stat)
                or not stat.S_ISREG(pre_stat.st_mode)
                or (
                    expected_state is not None
                    and pre_state != expected_state
                )
            ):
                raise _SafeFilesystemError("file is unsafe or changed")
            if os.name == "nt":
                descriptor = _open_windows_file_guard(path, pre_state)
            else:
                try:
                    descriptor = os.open(
                        parts[-1],
                        os.O_RDONLY
                        | os.O_NOFOLLOW
                        | getattr(os, "O_CLOEXEC", 0),
                        dir_fd=parent_descriptor,
                    )
                except (OSError, RuntimeError) as exc:
                    raise _SafeFilesystemError(
                        "could not open file safely"
                    ) from exc
            try:
                before = os.fstat(descriptor)
                if (
                    _stable_file_state(before) != pre_state
                    or _stat_has_reparse_point(before)
                    or not stat.S_ISREG(before.st_mode)
                    or (
                        max_bytes is not None
                        and before.st_size > max_bytes
                    )
                ):
                    raise _SafeFilesystemError(
                        "file identity changed before read"
                    )
                chunks: list[bytes] = []
                total = 0
                while True:
                    read_size = 1024 * 1024
                    if max_bytes is not None:
                        read_size = min(
                            read_size,
                            max_bytes + 1 - total,
                        )
                    chunk = os.read(descriptor, read_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if max_bytes is not None and total > max_bytes:
                        raise _SafeFilesystemError("file exceeds byte limit")
                after = os.fstat(descriptor)
                if _stable_file_state(after) != _stable_file_state(before):
                    raise _SafeFilesystemError("file changed while read")
                return b"".join(chunks), after
            finally:
                os.close(descriptor)


def _reparse_path_can_hide_branch(
    path_stat: os.stat_result,
    *,
    is_named_candidate: bool,
) -> bool:
    if not _stat_has_reparse_point(path_stat):
        return False
    if is_named_candidate:
        return True

    file_attributes = getattr(path_stat, "st_file_attributes", None)
    directory_attribute = getattr(
        stat,
        "FILE_ATTRIBUTE_DIRECTORY",
        0x10,
    )
    if (
        stat.S_ISDIR(path_stat.st_mode)
        or bool((file_attributes or 0) & directory_attribute)
    ):
        return True
    if (
        not stat.S_ISLNK(path_stat.st_mode)
        or file_attributes is None
    ):
        return True
    return False


def _walk_contract_tree(
    root: Path,
    base: Path,
    filename: str,
    guarded: _GuardedRepository,
) -> tuple[list[Path], list[ValidationIssue]]:
    candidates: list[Path] = []
    issues: list[ValidationIssue] = []
    pending = [base]

    while pending:
        directory = pending.pop()
        try:
            children = guarded.listdir(directory)
            issue = None
        except _SafeFilesystemError:
            children = None
            issue = _discovery_issue(
                "contract-discovery-error",
                directory,
                "Could not enumerate contract directory",
            )
        if issue is not None:
            issues.append(issue)
            continue
        assert children is not None

        child_directories: list[
            tuple[Path, tuple[int, int, int, int]]
        ] = []
        for name in children:
            path = directory / name
            try:
                path_stat = guarded.lstat(path)
            except _SafeFilesystemError:
                issues.append(
                    _discovery_issue(
                        "contract-discovery-error",
                        path,
                        "Could not inspect contract path",
                    )
                )
                continue

            is_named_candidate = (
                path.name.casefold() == filename.casefold()
            )
            unsafe_reparse = (
                guarded.posix_link_can_hide_branch(
                    path,
                    is_named_candidate=is_named_candidate,
                )
                if (
                    os.name != "nt"
                    and stat.S_ISLNK(path_stat.st_mode)
                )
                else _reparse_path_can_hide_branch(
                    path_stat,
                    is_named_candidate=is_named_candidate,
                )
            )
            if unsafe_reparse:
                issues.append(
                    _discovery_issue(
                        "unsafe-contract-path",
                        path,
                        "Contract discovery does not allow symlinks, "
                        "junctions, or reparse points",
                    )
                )
                continue
            if _stat_has_reparse_point(path_stat):
                continue

            if stat.S_ISDIR(path_stat.st_mode):
                child_directories.append(
                    (path, _stable_file_state(path_stat))
                )
                continue

            if (
                not stat.S_ISREG(path_stat.st_mode)
                or not is_named_candidate
            ):
                continue

            candidates.append(path)

        pending.extend(
            path for path, _state in reversed(child_directories)
        )

    return candidates, issues


def _candidate_paths(
    root: Path,
    root_children: tuple[Path, ...],
    directory: str,
    filename: str,
    guarded: _GuardedRepository,
) -> tuple[list[Path], list[ValidationIssue]]:
    candidates: list[Path] = []
    issues: list[ValidationIssue] = []
    matching_roots = sorted(
        (
            path
            for path in root_children
            if path.name.casefold() == directory.casefold()
        ),
        key=lambda path: (path.name.casefold(), path.name),
    )

    for base in matching_roots:
        try:
            base_stat = guarded.lstat(base)
        except _SafeFilesystemError:
            issues.append(
                _discovery_issue(
                    "contract-discovery-error",
                    base,
                    "Could not inspect matching contract root",
                )
            )
            continue
        if _stat_has_reparse_point(base_stat):
            issues.append(
                _discovery_issue(
                    "unsafe-contract-path",
                    base,
                    "Contract discovery does not allow symlinks, junctions, "
                    "or reparse points",
                )
            )
            continue
        if not stat.S_ISDIR(base_stat.st_mode):
            continue

        discovered, branch_issues = _walk_contract_tree(
            root,
            base,
            filename,
            guarded,
        )
        candidates.extend(discovered)
        issues.extend(branch_issues)

    candidates.sort(key=lambda path: path.relative_to(root).as_posix())
    issues.sort(
        key=lambda issue: (
            issue.path.relative_to(root).as_posix(),
            issue.code,
            issue.message,
        )
    )
    return candidates, issues


def _is_canonical_capability(root: Path, path: Path) -> bool:
    parts = path.relative_to(root).parts
    return (
        len(parts) == 4
        and parts[0] == "capabilities"
        and parts[3] == "CAPABILITY.md"
    )


def _is_canonical_workflow(root: Path, path: Path) -> bool:
    parts = path.relative_to(root).parts
    return (
        len(parts) == 3
        and parts[0] == "workflows"
        and parts[2] == "SKILL.md"
    )


def _issue_sort_key(
    root: Path,
    issue: ValidationIssue,
) -> tuple[str, str, str]:
    try:
        relative_path = issue.path.relative_to(root).as_posix()
    except ValueError:
        relative_path = issue.path.as_posix()
    return relative_path, issue.code, issue.message


def _validate_project_marker(
    root: Path,
    relative_path: PurePosixPath,
    *,
    expect_directory: bool,
    guarded: _GuardedRepository,
) -> ValidationIssue | None:
    candidate = root.joinpath(*relative_path.parts)
    current = root

    for index, part in enumerate(relative_path.parts):
        try:
            exact_names = set(guarded.listdir(current))
        except _SafeFilesystemError:
            return ValidationIssue(
                "invalid-root",
                candidate,
                f"Could not inspect required Hub marker: {relative_path}",
            )
        if part not in exact_names:
            return ValidationIssue(
                "invalid-root",
                candidate,
                f"Required Hub marker is missing: {relative_path}",
            )

        current /= part
        try:
            component_stat = guarded.lstat(current)
        except _SafeFilesystemError:
            return ValidationIssue(
                "invalid-root",
                candidate,
                f"Could not inspect required Hub marker: {relative_path}",
            )
        if _stat_has_reparse_point(component_stat):
            return ValidationIssue(
                "invalid-root",
                current,
                f"Required Hub marker is a symlink, junction, or reparse "
                f"point: {relative_path}",
            )

        is_final = index == len(relative_path.parts) - 1
        requires_directory = not is_final or expect_directory
        expected_type = (
            stat.S_ISDIR(component_stat.st_mode)
            if requires_directory
            else stat.S_ISREG(component_stat.st_mode)
        )
        if not expected_type:
            kind = "directory" if requires_directory else "regular file"
            return ValidationIssue(
                "invalid-root",
                current,
                f"Required Hub marker must be a {kind}: {relative_path}",
            )

    return None


def _project_marker_issues(
    root: Path,
    guarded: _GuardedRepository,
) -> list[ValidationIssue]:
    marker_specs = (
        (PurePosixPath("SKILL.md"), False),
        (PurePosixPath("pyproject.toml"), False),
        (PurePosixPath("src/agent_workflow_hub"), True),
    )
    issues = [
        issue
        for relative_path, expect_directory in marker_specs
        if (
            issue := _validate_project_marker(
                root,
                relative_path,
                expect_directory=expect_directory,
                guarded=guarded,
            )
        )
        is not None
    ]
    return sorted(issues, key=lambda issue: _issue_sort_key(root, issue))


def _build_workflow_descriptor(
    skill: SkillContract,
    metadata: _WorkflowMetadataSnapshot,
) -> WorkflowDescriptor:
    required_capabilities = tuple(
        json.loads(skill.metadata.get("required-capabilities", "[]"))
    )
    raw_capability_slots = json.loads(
        skill.metadata.get("capability-slots", "{}")
    )
    capability_slots = MappingProxyType(
        {
            slot: tuple(raw_capability_slots[slot])
            for slot in sorted(raw_capability_slots)
        }
    )
    config_templates = tuple(
        ConfigTemplateDescriptor(
            label=label,
            relative_path=metadata.config_templates[label],
            output_name=PurePosixPath(
                metadata.config_templates[label]
            ).name.removesuffix(".example"),
            scope=metadata.config_requirements[label].scope,
            required=metadata.config_requirements[label].required,
            content=metadata.template_snapshots[label].content,
            sha256=metadata.template_snapshots[label].sha256,
        )
        for label in sorted(metadata.config_templates)
    )
    return WorkflowDescriptor(
        name=skill.name,
        description=skill.description,
        required_capabilities=required_capabilities,
        capability_slots=capability_slots,
        supported_hosts=tuple(sorted(metadata.supported_hosts)),
        config_templates=config_templates,
        entrypoints=MappingProxyType(dict(sorted(metadata.entrypoints.items()))),
    )


def _build_repository_catalog(
    root: Path,
    workflows: list[WorkflowDescriptor],
    capabilities: Mapping[str, CapabilityContract],
) -> RepositoryCatalog:
    canonical_root = Path(os.path.abspath(root))
    return RepositoryCatalog(
        root=canonical_root,
        workflows=tuple(sorted(workflows, key=lambda workflow: workflow.name)),
        capabilities=MappingProxyType(dict(sorted(capabilities.items()))),
    )


def _loaded_with_issues(
    root: Path,
    issues: list[ValidationIssue],
) -> LoadedRepository:
    return LoadedRepository(
        issues=tuple(sorted(issues, key=lambda issue: _issue_sort_key(root, issue))),
        catalog=_build_repository_catalog(root, [], {}),
    )


def validate_repository(
    root: Path,
    *,
    require_project_markers: bool = False,
) -> list[ValidationIssue]:
    """Validate contracts under *root*.

    The default marker-free mode exists for isolated structural fixtures and
    library tests; it does not establish that *root* is a runnable Hub.
    """
    try:
        with _GuardedRepository(root) as guarded:
            loaded = _load_guarded_repository(
                root,
                guarded,
                require_project_markers=require_project_markers,
            )
            return list(loaded.issues)
    except _SafeFilesystemError:
        return [
            ValidationIssue(
                "invalid-root",
                root,
                "Repository root could not be guarded safely",
            )
        ]


def _load_guarded_repository(
    root: Path,
    guarded: _GuardedRepository,
    *,
    require_project_markers: bool,
) -> LoadedRepository:
    try:
        root_children = tuple(root / name for name in guarded.listdir(root))
    except _SafeFilesystemError:
        return _loaded_with_issues(
            root,
            [
                ValidationIssue(
                    "invalid-root",
                    root,
                    "Could not enumerate repository root",
                )
            ],
        )

    if require_project_markers:
        marker_issues = _project_marker_issues(
            root,
            guarded,
        )
        if marker_issues:
            return _loaded_with_issues(root, marker_issues)

    try:
        project_hosts = project_supported_hosts(root, guarded)
    except (ContractError, FrontmatterError, OSError, RuntimeError) as exc:
        return _loaded_with_issues(
            root,
            [
                ValidationIssue(
                    "invalid-root-skill",
                    root / "SKILL.md",
                    str(exc),
                )
            ],
        )

    issues: list[ValidationIssue] = []
    capabilities: dict[str, CapabilityContract] = {}

    capability_paths, capability_discovery_issues = _candidate_paths(
        root,
        root_children,
        "capabilities",
        "CAPABILITY.md",
        guarded,
    )
    issues.extend(capability_discovery_issues)
    for path in capability_paths:
        if not _is_canonical_capability(root, path):
            issues.append(
                ValidationIssue(
                    "noncanonical-capability",
                    path,
                    "Capability path must be "
                    "capabilities/<type>/<slug>/CAPABILITY.md",
                )
            )
            continue
        try:
            snapshot = _load_text_snapshot_guarded(
                guarded,
                path,
                "capability contract",
                _stable_file_state(guarded.lstat(path)),
            )
            capability = validate_capability(
                path,
                *parse_markdown_text(snapshot.content, path),
            )
        except (
            ContractError,
            FrontmatterError,
            OSError,
            RuntimeError,
            _SafeFilesystemError,
        ) as exc:
            issues.append(ValidationIssue("invalid-capability", path, str(exc)))
        else:
            if capability.id in capabilities:
                issues.append(
                    ValidationIssue(
                        "duplicate-capability",
                        path,
                        "Capability id is declared more than once: "
                        f"{capability.id}",
                    )
                )
                continue
            capabilities[capability.id] = capability
            present_headings = _headings(capability.body)
            for heading in sorted(REQUIRED_CAPABILITY_HEADINGS):
                if heading not in present_headings:
                    issues.append(
                        ValidationIssue(
                            "missing-capability-heading",
                            path,
                            "Missing required capability heading: "
                            f"{heading}",
                        )
                    )

    workflow_paths, workflow_discovery_issues = _candidate_paths(
        root,
        root_children,
        "workflows",
        "SKILL.md",
        guarded,
    )
    issues.extend(workflow_discovery_issues)
    workflows: list[WorkflowDescriptor] = []
    workflow_names: set[str] = set()
    for path in workflow_paths:
        if not _is_canonical_workflow(root, path):
            issues.append(
                ValidationIssue(
                    "noncanonical-skill",
                    path,
                    "Workflow path must be workflows/<slug>/SKILL.md",
                )
            )
            continue
        try:
            snapshot = _load_text_snapshot_guarded(
                guarded,
                path,
                "workflow contract",
                _stable_file_state(guarded.lstat(path)),
            )
            skill = validate_skill(
                path,
                *parse_markdown_text(snapshot.content, path),
            )
            workflow_metadata = _validate_workflow_metadata(
                path,
                skill.metadata,
                project_hosts,
                guarded,
            )
        except (
            ContractError,
            FrontmatterError,
            OSError,
            RuntimeError,
            _SafeFilesystemError,
        ) as exc:
            issues.append(ValidationIssue("invalid-skill", path, str(exc)))
            continue

        if skill.name in workflow_names:
            issues.append(
                ValidationIssue(
                    "duplicate-workflow",
                    path,
                    f"Workflow name is declared more than once: {skill.name}",
                )
            )
            continue
        workflow_names.add(skill.name)
        workflows.append(
            _build_workflow_descriptor(skill, workflow_metadata)
        )

        present_headings = _headings(skill.body)
        for heading in sorted(REQUIRED_HEADINGS):
            if heading not in present_headings:
                issues.append(
                    ValidationIssue(
                        "missing-heading",
                        path,
                        f"Missing required heading: {heading}",
                    )
                )

        required_capabilities = json.loads(
            skill.metadata.get("required-capabilities", "[]")
        )
        for capability_id in required_capabilities:
            if capability_id not in capabilities:
                issues.append(
                    ValidationIssue(
                        "missing-capability",
                        path,
                        f"Required capability not found: {capability_id}",
                    )
                )

        capability_slots = json.loads(skill.metadata.get("capability-slots", "{}"))
        for slot in sorted(capability_slots):
            for capability_id in sorted(capability_slots[slot]):
                if capability_id not in capabilities:
                    issues.append(
                        ValidationIssue(
                            "missing-capability",
                            path,
                            "Capability slot "
                            f"{slot} references missing capability: {capability_id}",
                        )
                    )

    if issues:
        return _loaded_with_issues(root, issues)
    return LoadedRepository(
        issues=(),
        catalog=_build_repository_catalog(root, workflows, capabilities),
    )
