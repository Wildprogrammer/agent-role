from __future__ import annotations

import configparser
import ctypes
import json
import ntpath
import os
import platform
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol
from urllib.parse import urlparse

import httpx
import yaml

from .adapters import AdapterError, validate_adapter
from .catalog import (
    ConfigTemplateDescriptor,
    RepositoryCatalog,
    WorkflowDescriptor,
)
from .contracts import CapabilityContract, ContractError
from .frontmatter import FrontmatterError, parse_markdown
from .jenkins_mcp import config as jenkins_config
from .jenkins_mcp.policy import parse_policy as parse_jenkins_policy
from .mysql_mcp import config as mysql_config
from .mysql_mcp.policy import parse_policy as parse_mysql_policy
from .repository import load_text_snapshot
from .ssh_operations import config as ssh_config
from .support import PROJECT_HOSTS, host_compatibility


@dataclass(frozen=True)
class DoctorReport:
    host: str
    status: str
    details: tuple[str, ...]


@dataclass(frozen=True)
class CapabilityReadiness:
    capability_id: str
    required: bool
    status: str
    details: tuple[str, ...]


@dataclass(frozen=True)
class ConfigReadiness:
    label: str
    required: bool
    scope: str
    status: str
    path: Path | None


@dataclass(frozen=True)
class WorkflowDoctorReport:
    workflow: str
    status: str
    configs: tuple[ConfigReadiness, ...]
    capabilities: tuple[CapabilityReadiness, ...]
    host_compatibility: str


@dataclass(frozen=True)
class DetectorContext:
    root: Path
    config_paths: Mapping[str, Path]
    selected_capabilities: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(
            self,
            "config_paths",
            MappingProxyType(
                {
                    str(label): Path(path)
                    for label, path in self.config_paths.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "selected_capabilities",
            MappingProxyType(dict(self.selected_capabilities)),
        )


@dataclass(frozen=True)
class CandidateSearchResult:
    candidates: tuple[Path, ...]
    status: str
    details: tuple[str, ...]


@dataclass(frozen=True)
class DetectorContract:
    detector_type: str
    executable_names: tuple[str, ...]
    import_name: str | None
    distribution_name: str | None
    version_argv: tuple[str, ...]
    version_parser: str
    config_label: str | None
    read_only_behavior: str
    product_aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.detector_type not in {
            "executable",
            "python-import",
            "mcp-config",
            "external-service",
        }:
            raise ValueError("unsupported detector type")
        if self.version_parser not in {"none", "presence", "semantic"}:
            raise ValueError("unsupported version parser")
        if not isinstance(self.read_only_behavior, str) or not self.read_only_behavior:
            raise ValueError("read-only detector behavior is required")
        if (
            not isinstance(self.executable_names, tuple)
            or not all(
                isinstance(name, str)
                and bool(name.strip())
                and ntpath.basename(name) == name
                and posixpath.basename(name) == name
                for name in self.executable_names
            )
            or not isinstance(self.version_argv, tuple)
            or not all(isinstance(token, str) for token in self.version_argv)
            or not isinstance(self.product_aliases, tuple)
            or not all(
                isinstance(alias, str) and bool(alias.strip())
                for alias in self.product_aliases
            )
            or len(set(self.product_aliases)) != len(self.product_aliases)
        ):
            raise ValueError("detector metadata must use closed nonblank tuples")

        executable_probe = (
            len(self.version_argv) == 2
            and self.version_argv[0] == "{executable}"
            and self.version_argv[1] in {"--version", "-version", "--help"}
        )
        if self.detector_type == "executable":
            if not self.executable_names:
                raise ValueError("executable detectors require executable names")
            if self.import_name is not None or self.distribution_name is not None:
                raise ValueError("executable detectors cannot import Python packages")
            if self.config_label is not None:
                raise ValueError("executable detectors cannot require service config")
            if self.version_argv:
                if not executable_probe or self.version_parser != "semantic":
                    raise ValueError("executable version probe grammar is not allowed")
                if not self.product_aliases:
                    raise ValueError("semantic executable probes require product aliases")
            elif self.version_parser != "presence":
                raise ValueError("presence executable probes must not run argv")
            return

        python_probe = (
            "{python}",
            "-I",
            "-c",
            "{import-metadata}",
        )
        if self.detector_type == "python-import":
            if self.executable_names:
                raise ValueError("python import detectors cannot name executables")
            if (
                not isinstance(self.import_name, str)
                or not self.import_name.strip()
                or not isinstance(self.distribution_name, str)
                or not self.distribution_name.strip()
                or self.version_argv != python_probe
                or self.version_parser != "semantic"
                or self.config_label is not None
                or not self.product_aliases
            ):
                raise ValueError("python import probe grammar is not allowed")
            return

        if self.version_argv:
            raise ValueError("service and MCP detectors cannot run version argv")
        if (
            self.version_parser != "none"
            or self.import_name is not None
            or self.distribution_name is not None
            or self.product_aliases
        ):
            raise ValueError("service and MCP detector metadata is not allowed")


class WorkflowDoctorError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    output: str


RUNNER_SPAWN_ERROR = -1001
RUNNER_TIMEOUT = -1002
RUNNER_OUTPUT_LIMIT = -1003


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        output_limit: int,
    ) -> ProcessResult: ...


class Filesystem(Protocol):
    def is_dir(self, path: Path) -> bool: ...

    def is_file(self, path: Path) -> bool: ...

    def iterdir(self, path: Path): ...

    def directory_identity(self, path: Path) -> Hashable | None: ...


class SubprocessRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        output_limit: int,
    ) -> ProcessResult:
        if not isinstance(argv, (list, tuple)) or not argv:
            raise ValueError("detector argv must be a non-empty list or tuple")
        if timeout <= 0 or output_limit <= 0:
            raise ValueError("detector timeout and output limit must be positive")
        popen_options: dict[str, object] = {
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            popen_options["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
            )
        else:
            popen_options["start_new_session"] = True
        try:
            process = subprocess.Popen(list(argv), **popen_options)
        except OSError:
            return ProcessResult(RUNNER_SPAWN_ERROR, "")

        process_group = process.pid if os.name != "nt" else None
        windows_job = None
        if os.name == "nt":
            try:
                windows_job = _create_windows_kill_job(process)
            except (AttributeError, OSError, RuntimeError, ValueError):
                windows_job = None
            if windows_job is None:
                try:
                    process.kill()
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if process.stdout is not None:
                    process.stdout.close()
                return ProcessResult(RUNNER_SPAWN_ERROR, "")

        output = bytearray()
        output_limited = threading.Event()

        def read_output() -> None:
            stream = process.stdout
            if stream is None:
                return
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        return
                    remaining = max(0, output_limit - len(output))
                    if remaining:
                        output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        output_limited.set()
                        return
            except OSError:
                return

        reader = threading.Thread(
            target=read_output,
            name="workflow-doctor-output-reader",
            daemon=False,
        )
        reader.start()
        deadline = time.monotonic() + timeout
        returncode: int | None = None
        while returncode is None:
            if output_limited.is_set():
                returncode = RUNNER_OUTPUT_LIMIT
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                returncode = RUNNER_TIMEOUT
                break
            polled = process.poll()
            if polled is not None and not reader.is_alive():
                returncode = polled
                break
            time.sleep(min(0.01, remaining))

        _terminate_and_reap(
            process,
            process_group=process_group,
            windows_job=windows_job,
        )
        reader.join()
        if process.stdout is not None:
            process.stdout.close()
        return ProcessResult(
            returncode,
            bytes(output).decode("utf-8", errors="ignore"),
        )


def _terminate_and_reap(
    process: subprocess.Popen[bytes],
    *,
    process_group: int | None,
    windows_job: int | None,
) -> None:
    if os.name == "nt":
        if windows_job is not None:
            _close_windows_job(windows_job, terminate=True)
        elif process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    elif process_group is not None:
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass
    if os.name != "nt" and process_group is not None:
        try:
            os.killpg(process_group, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        pass


def _create_windows_kill_job(process: subprocess.Popen[bytes]) -> int | None:
    if os.name != "nt":
        return None
    from ctypes import wintypes

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.CreateToolhelp32Snapshot.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.OpenThread.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    information = _ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(information),
        ctypes.sizeof(information),
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, wintypes.HANDLE(int(process._handle))
    )
    if not assigned or not _resume_windows_process(kernel32, process.pid, _ThreadEntry32):
        _close_windows_job(int(job), terminate=True)
        return None
    return int(job)


def _resume_windows_process(kernel32, pid: int, entry_type) -> bool:
    from ctypes import wintypes

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if not snapshot or int(snapshot) == invalid_handle:
        return False
    try:
        entry = entry_type()
        entry.dwSize = ctypes.sizeof(entry)
        found = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(
                    0x0002, False, entry.th32ThreadID
                )
                if not thread:
                    return False
                try:
                    return kernel32.ResumeThread(thread) != 0xFFFFFFFF
                finally:
                    kernel32.CloseHandle(thread)
            found = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
        return False
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(snapshot))


def _close_windows_job(job: int, *, terminate: bool) -> None:
    if os.name != "nt":
        return
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.TerminateJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.UINT,
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = wintypes.HANDLE(job)
    if terminate:
        kernel32.TerminateJobObject(handle, 1)
    kernel32.CloseHandle(handle)


class LocalFilesystem:
    def is_dir(self, path: Path) -> bool:
        return self.directory_identity(path) is not None

    def is_file(self, path: Path) -> bool:
        try:
            metadata = os.lstat(path)
        except OSError:
            return False
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(metadata.st_mode) or attributes & reparse_flag:
            return False
        return stat.S_ISREG(metadata.st_mode)

    def iterdir(self, path: Path):
        return path.iterdir()

    def directory_identity(self, path: Path) -> Hashable | None:
        try:
            metadata = os.lstat(path)
        except OSError:
            return None
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or attributes & reparse_flag
            or not stat.S_ISDIR(metadata.st_mode)
        ):
            return None
        device = int(getattr(metadata, "st_dev", 0))
        inode = int(getattr(metadata, "st_ino", 0))
        if device and inode:
            return ("inode", device, inode)
        return (
            "path",
            os.path.normcase(os.path.abspath(os.fspath(path))),
        )


@dataclass(frozen=True)
class _ConfigSnapshot:
    path: Path
    text: str = field(repr=False)
    parsed: object = field(repr=False)


@dataclass(frozen=True)
class _JenkinsSnapshotConfig:
    name: str
    url: str
    environment: str
    allow_insecure_http: bool
    require_crumb: bool
    ca_bundle: Path | None
    confirm_writes: bool
    controller: object = field(repr=False)


@dataclass(frozen=True)
class _DetectorRuntime:
    runner: Runner
    filesystem: Filesystem
    which: Callable[[str], str | None]
    environ: Mapping[str, str]
    system: str
    clock: Callable[[], float]
    config_snapshots: Mapping[str, _ConfigSnapshot]
    http_transport: httpx.BaseTransport | None


_MAX_ROOTS = 64
_MAX_DEPTH = 4
_MAX_ENTRIES = 4096
_MAX_CANDIDATES = 32
_MAX_SECONDS = 2.0
_PROBE_TIMEOUT = 5.0
_OUTPUT_LIMIT = 4096
_MAX_CONFIG_BYTES = 1024 * 1024
_MAX_JENKINS_RESPONSE_BYTES = 64 * 1024
_JENKINS_PROBE_TOTAL_SECONDS = 5.0
_JENKINS_READ_TIMEOUT_SECONDS = 1.0
_VERSION = re.compile(r"(?<!\d)(\d+)\.(\d+)\.(\d+)(?!\d)")
_JENKINS_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
_PYTHON_MODULE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*"
)
_DISTRIBUTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_ENVIRONMENT_KEY = re.compile(r"[A-Z_][A-Z0-9_]*")
_CAPABILITY_MARKER = re.compile(r"capability=(?:model|python)\.[a-z0-9-]+")


@dataclass(frozen=True)
class _PythonImportProbeContract:
    modules: tuple[str, ...]
    distribution: str
    environment: tuple[tuple[str, str], ...]
    marker: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.modules, tuple)
            or not self.modules
            or len(set(self.modules)) != len(self.modules)
            or not all(
                isinstance(module, str)
                and _PYTHON_MODULE.fullmatch(module) is not None
                for module in self.modules
            )
            or not isinstance(self.distribution, str)
            or _DISTRIBUTION.fullmatch(self.distribution) is None
            or not isinstance(self.environment, tuple)
            or not isinstance(self.marker, str)
            or _CAPABILITY_MARKER.fullmatch(self.marker) is None
        ):
            raise ValueError("invalid Python import probe contract")
        keys: set[str] = set()
        for item in self.environment:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or _ENVIRONMENT_KEY.fullmatch(item[0]) is None
                or item[0] in keys
                or not isinstance(item[1], str)
                or not item[1]
                or len(item[1]) > 256
                or not item[1].isprintable()
            ):
                raise ValueError("invalid Python import probe environment")
            keys.add(item[0])


def _python_probe_source(contract: _PythonImportProbeContract) -> str:
    statements: list[str] = []
    if contract.environment:
        statements.append("import os")
        statements.extend(
            f"os.environ[{key!r}]={value!r}"
            for key, value in contract.environment
        )
    statements.extend(f"import {module}" for module in contract.modules)
    statements.append("from importlib.metadata import version")
    statements.append(
        f"print({contract.marker + ' version='!r} + "
        f"version({contract.distribution!r}))"
    )
    return "; ".join(statements)


PYTHON_IMPORT_PROBES: Mapping[
    str, _PythonImportProbeContract
] = MappingProxyType(
    {
        "model.funasr": _PythonImportProbeContract(
            ("funasr", "torch", "torchaudio"),
            "funasr",
            (),
            "capability=model.funasr",
        ),
        "model.paddleocr": _PythonImportProbeContract(
            ("paddle", "paddleocr"),
            "paddleocr",
            (("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True"),),
            "capability=model.paddleocr",
        ),
        "model.voxcpm": _PythonImportProbeContract(
            ("torch", "voxcpm"),
            "voxcpm",
            (),
            "capability=model.voxcpm",
        ),
        "python.asyncssh": _PythonImportProbeContract(
            ("asyncssh",),
            "asyncssh",
            (),
            "capability=python.asyncssh",
        ),
        "python.lancedb": _PythonImportProbeContract(
            ("lancedb",),
            "lancedb",
            (),
            "capability=python.lancedb",
        ),
        "python.pillow": _PythonImportProbeContract(
            ("PIL",),
            "Pillow",
            (),
            "capability=python.pillow",
        ),
    }
)


def _environment_details(path: Path) -> tuple[str, ...]:
    return (
        f"system={platform.system()} {platform.release()}",
        f"architecture={platform.machine()}",
        f"python={platform.python_version()} ({sys.executable})",
        f"adapter_path={path.resolve(strict=False)}",
    )


def doctor(root: Path, *, host: str) -> DoctorReport:
    path = root / "adapters" / host / "ADAPTER.md"
    environment = _environment_details(path)
    if not path.is_file():
        return DoctorReport(host, "missing-adapter", environment)

    try:
        contract = validate_adapter(path, *parse_markdown(path))
    except (AdapterError, FrontmatterError, OSError, RuntimeError) as exc:
        return DoctorReport(
            host,
            "invalid-adapter",
            environment + (f"error={exc}",),
        )

    status = "verified" if contract.is_verified else "unverified"
    return DoctorReport(
        host,
        status,
        environment
        + (
            f"docs={contract.official_docs}",
            f"minimum_version={contract.minimum_version}",
            f"skill_discovery={contract.skill_discovery}",
            f"mcp_support={contract.mcp_support}",
            f"subagent_support={contract.subagent_support}",
        ),
    )


def _contract(
    detector_type: str,
    *,
    executables: tuple[str, ...] = (),
    import_name: str | None = None,
    distribution_name: str | None = None,
    version_argv: tuple[str, ...] = (),
    parser: str = "none",
    config_label: str | None = None,
    aliases: tuple[str, ...] = (),
    behavior: str,
) -> DetectorContract:
    return DetectorContract(
        detector_type,
        executables,
        import_name,
        distribution_name,
        version_argv,
        parser,
        config_label,
        behavior,
        aliases,
    )


CAPABILITY_DETECTOR_CONTRACTS: Mapping[str, DetectorContract] = MappingProxyType(
    {
        "app.bambu-studio": _contract(
            "executable",
            executables=("bambu-studio", "bambu-studio.exe", "BambuStudio.exe"),
            version_argv=("{executable}", "--version"),
            parser="semantic",
            aliases=("Bambu Studio", "BambuStudio"),
            behavior="fixed version probe",
        ),
        "app.blender": _contract(
            "executable",
            executables=("blender", "blender.exe"),
            version_argv=("{executable}", "--version"),
            parser="semantic",
            aliases=("Blender",),
            behavior="fixed version probe",
        ),
        "app.jenkins": _contract(
            "external-service",
            config_label="main",
            behavior="fixed read-only controller metadata probe",
        ),
        "app.obsidian": _contract(
            "executable",
            executables=("obsidian", "Obsidian.exe"),
            parser="presence",
            behavior="executable path fact only",
        ),
        "app.orcaslicer": _contract(
            "executable",
            executables=("orcaslicer", "orca-slicer", "OrcaSlicer.exe"),
            version_argv=("{executable}", "--help"),
            parser="semantic",
            aliases=("OrcaSlicer", "Orca Slicer"),
            behavior="fixed help probe",
        ),
        "app.playwright": _contract(
            "executable",
            executables=("playwright", "playwright.exe"),
            version_argv=("{executable}", "--version"),
            parser="semantic",
            aliases=("Playwright", "Version"),
            behavior="fixed version probe",
        ),
        "app.prusaslicer": _contract(
            "executable",
            executables=(
                "prusa-slicer-console",
                "prusa-slicer-console.exe",
            ),
            version_argv=("{executable}", "--help"),
            parser="semantic",
            aliases=("PrusaSlicer", "Prusa Slicer"),
            behavior="fixed help probe",
        ),
        "cli.agent-browser": _contract(
            "executable",
            executables=("agent-browser", "agent-browser.cmd", "agent-browser.exe"),
            version_argv=("{executable}", "--version"),
            parser="semantic",
            aliases=("agent-browser", "Agent Browser"),
            behavior="fixed version probe",
        ),
        "cli.ffmpeg": _contract(
            "executable",
            executables=("ffmpeg", "ffmpeg.exe"),
            version_argv=("{executable}", "-version"),
            parser="semantic",
            aliases=("ffmpeg",),
            behavior="fixed version probe",
        ),
        "cli.playwright-cli": _contract(
            "executable",
            executables=("playwright-cli", "playwright-cli.cmd"),
            version_argv=("{executable}", "--version"),
            parser="semantic",
            aliases=("playwright-cli", "Playwright CLI"),
            behavior="fixed version probe",
        ),
        "cli.tavily-search": _contract(
            "external-service",
            executables=("tavily-search", "tavily-search.exe"),
            behavior="local executable path fact without service request",
        ),
        "cli.tesseract": _contract(
            "executable",
            executables=("tesseract", "tesseract.exe"),
            version_argv=("{executable}", "--version"),
            parser="semantic",
            aliases=("tesseract",),
            behavior="fixed version probe",
        ),
        "cli.umi-ocr": _contract(
            "executable",
            executables=("PaddleOCR-json.exe", "Umi-OCR.exe", "umi-ocr"),
            parser="presence",
            behavior="executable path fact; JSON service is never started",
        ),
        "mcp.blender": _contract(
            "mcp-config",
            executables=("uvx", "uvx.exe"),
            behavior="executable and host registration facts only",
        ),
        "model.funasr": _contract(
            "python-import",
            import_name="funasr",
            distribution_name="funasr",
            version_argv=("{python}", "-I", "-c", "{import-metadata}"),
            parser="semantic",
            aliases=("capability=model.funasr",),
            behavior="isolated import metadata probe",
        ),
        "model.paddleocr": _contract(
            "python-import",
            import_name="paddleocr",
            distribution_name="paddleocr",
            version_argv=("{python}", "-I", "-c", "{import-metadata}"),
            parser="semantic",
            aliases=("capability=model.paddleocr",),
            behavior="isolated import metadata probe",
        ),
        "model.voxcpm": _contract(
            "python-import",
            import_name="voxcpm",
            distribution_name="voxcpm",
            version_argv=("{python}", "-I", "-c", "{import-metadata}"),
            parser="semantic",
            aliases=("capability=model.voxcpm",),
            behavior="isolated import metadata probe",
        ),
        "python.asyncssh": _contract(
            "python-import",
            import_name="asyncssh",
            distribution_name="asyncssh",
            version_argv=("{python}", "-I", "-c", "{import-metadata}"),
            parser="semantic",
            aliases=("capability=python.asyncssh",),
            behavior="isolated import metadata probe",
        ),
        "python.lancedb": _contract(
            "python-import",
            import_name="lancedb",
            distribution_name="lancedb",
            version_argv=("{python}", "-I", "-c", "{import-metadata}"),
            parser="semantic",
            aliases=("capability=python.lancedb",),
            behavior="isolated import metadata probe",
        ),
        "python.pillow": _contract(
            "python-import",
            import_name="PIL",
            distribution_name="Pillow",
            version_argv=("{python}", "-I", "-c", "{import-metadata}"),
            parser="semantic",
            aliases=("capability=python.pillow",),
            behavior="isolated import metadata probe",
        ),
    }
)


def _standard_roots(
    system: str, environ: Mapping[str, str]
) -> tuple[Path, ...]:
    if system.casefold() == "windows":
        return tuple(
            Path(value)
            for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")
            if (value := environ.get(name))
        )
    if system.casefold() == "darwin":
        return (
            Path("/Applications"),
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            Path("/opt"),
        )
    return (Path("/usr/local/bin"), Path("/usr/bin"), Path("/opt"))


def _path_key(path: Path, *, system: str) -> str:
    value = os.fspath(path)
    if system.casefold() == "windows":
        return ntpath.normcase(ntpath.abspath(value))
    if os.name == "nt":
        value = value.replace("\\", "/")
    return posixpath.abspath(value)


def _path_order_key(path: Path, system: str) -> tuple[str, str]:
    value = os.fspath(path)
    primary = _path_key(path, system=system)
    if system.casefold() == "windows":
        secondary = ntpath.abspath(value).replace("/", "\\")
    else:
        if os.name == "nt":
            value = value.replace("\\", "/")
        secondary = posixpath.abspath(value)
    return primary, secondary


def _is_filesystem_root(path: Path, system: str) -> bool:
    value = os.fspath(path)
    if system.casefold() == "windows":
        normalized = ntpath.normpath(value)
        drive, tail = ntpath.splitdrive(normalized)
        if drive.startswith(("\\\\", "//")):
            return tail in {"", "\\", "/"}
        return bool(drive) and tail in {"\\", "/"}
    if os.name == "nt":
        value = value.replace("\\", "/")
    normalized = posixpath.abspath(value)
    return posixpath.dirname(normalized) == normalized


def _directory_identity(
    filesystem: Filesystem,
    path: Path,
    system: str,
) -> Hashable | None:
    identity_reader = getattr(filesystem, "directory_identity", None)
    if callable(identity_reader):
        identity = identity_reader(path)
        if identity is None:
            return None
        try:
            hash(identity)
        except TypeError:
            return None
        return identity
    if not filesystem.is_dir(path):
        return None
    return ("path", _path_key(path, system=system))


def bounded_candidates(
    context: DetectorContext,
    executable_names: Sequence[str],
    *,
    filesystem: Filesystem | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    clock: Callable[[], float] | None = None,
    _roots_only: Sequence[Path] | None = None,
    _collect_all: bool = False,
) -> CandidateSearchResult:
    fs = filesystem or LocalFilesystem()
    which_fn = which or shutil.which
    environment = dict(os.environ) if environ is None else dict(environ)
    system_name = system or platform.system()
    monotonic = clock or time.monotonic
    started = monotonic()
    names = tuple(dict.fromkeys(str(name) for name in executable_names))
    folded_names = {
        name.casefold() if system_name.casefold() == "windows" else name
        for name in names
    }
    candidates: list[Path] = []
    candidate_keys: set[str] = set()
    roots: list[Path] = []
    root_keys: set[str] = set()
    visited_directories: set[Hashable] = set()

    def manual(detail: str) -> CandidateSearchResult:
        return CandidateSearchResult(
            tuple(candidates), "manual_check_required", (detail,)
        )

    def timed_out() -> bool:
        return monotonic() - started >= _MAX_SECONDS

    def add_candidate(path: Path) -> CandidateSearchResult | None:
        if _is_filesystem_root(path, system_name):
            return manual("limit=filesystem-root")
        key = _path_key(path, system=system_name)
        if key in candidate_keys:
            return None
        candidate_keys.add(key)
        candidates.append(Path(path))
        if len(candidates) >= _MAX_CANDIDATES:
            return manual("limit=candidates:32")
        return None

    def add_root(
        path: Path,
    ) -> tuple[CandidateSearchResult | None, bool]:
        if _is_filesystem_root(path, system_name):
            return manual("limit=filesystem-root"), False
        key = _path_key(path, system=system_name)
        if key in root_keys:
            return None, False
        root_keys.add(key)
        roots.append(Path(path))
        if len(roots) >= _MAX_ROOTS:
            return manual("limit=roots:64"), True
        return None, True

    entries_seen = 0

    def scan_root(root: Path) -> CandidateSearchResult | None:
        nonlocal entries_seen
        if timed_out():
            return manual("limit=seconds:2.0")
        root_identity = _directory_identity(fs, root, system_name)
        if timed_out():
            return manual("limit=seconds:2.0")
        if root_identity is None or root_identity in visited_directories:
            return None
        visited_directories.add(root_identity)
        pending: deque[tuple[Path, int]] = deque(((root, 0),))
        while pending:
            if timed_out():
                return manual("limit=seconds:2.0")
            directory, depth = pending.popleft()
            try:
                entries = fs.iterdir(directory)
                if timed_out():
                    return manual("limit=seconds:2.0")
                ordered_entries: list[Path] = []
                for entry in entries:
                    if timed_out():
                        return manual("limit=seconds:2.0")
                    entries_seen += 1
                    if entries_seen >= _MAX_ENTRIES:
                        return manual("limit=entries:4096")
                    ordered_entries.append(Path(entry))
            except OSError:
                continue
            ordered_entries.sort(
                key=lambda item: _path_order_key(item, system_name)
            )
            try:
                for path in ordered_entries:
                    if timed_out():
                        return manual("limit=seconds:2.0")
                    comparable = (
                        path.name.casefold()
                        if system_name.casefold() == "windows"
                        else path.name
                    )
                    path_is_file = fs.is_file(path)
                    if timed_out():
                        return manual("limit=seconds:2.0")
                    if path_is_file and comparable in folded_names:
                        limited = add_candidate(path)
                        if limited is not None:
                            return limited
                        if not _collect_all:
                            return CandidateSearchResult(
                                tuple(candidates), "complete", ()
                            )
                    path_identity = _directory_identity(
                        fs, path, system_name
                    )
                    if timed_out():
                        return manual("limit=seconds:2.0")
                    if (
                        path_identity is not None
                        and path_identity not in visited_directories
                    ):
                        if depth + 1 >= _MAX_DEPTH:
                            return manual("limit=depth:4")
                        visited_directories.add(path_identity)
                        pending.append((path, depth + 1))
            except OSError:
                continue
        return None

    def register_and_scan(root: Path) -> CandidateSearchResult | None:
        limited, added = add_root(root)
        if limited is not None:
            return limited
        if not added:
            return None
        return scan_root(root)

    if _roots_only is not None:
        for root in _roots_only:
            discovered = register_and_scan(Path(root))
            if discovered is not None:
                return discovered
        return CandidateSearchResult(tuple(candidates), "complete", ())

    for configured_path in context.config_paths.values():
        configured = Path(configured_path)
        discovered = register_and_scan(configured.parent)
        if discovered is not None:
            return discovered

    for name in names:
        if timed_out():
            return manual("limit=seconds:2.0")
        found = which_fn(name)
        if timed_out():
            return manual("limit=seconds:2.0")
        if found:
            limited = add_candidate(Path(found))
            if limited is not None:
                return limited
    if candidates:
        return CandidateSearchResult(tuple(candidates), "complete", ())

    separator = ";" if system_name.casefold() == "windows" else ":"
    for value in environment.get("PATH", "").split(separator):
        if value:
            discovered = register_and_scan(Path(value))
            if discovered is not None:
                return discovered

    for root in _standard_roots(system_name, environment):
        discovered = register_and_scan(root)
        if discovered is not None:
            return discovered
    discovered = register_and_scan(
        context.root / "workspace" / "shared" / "runtimes"
    )
    if discovered is not None:
        return discovered
    return CandidateSearchResult(tuple(candidates), "complete", ())


def _minimum_version(capability: CapabilityContract) -> tuple[int, int, int]:
    requirement = capability.version_requirement
    if requirement is None:
        return (0, 0, 0)
    match = _VERSION.search(requirement)
    if match is None:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def _detected_product_version(
    output: str,
    aliases: tuple[str, ...],
) -> tuple[int, int, int] | None:
    if not aliases:
        return None
    products = "|".join(
        re.escape(alias) for alias in sorted(aliases, key=len, reverse=True)
    )
    pattern = re.compile(
        rf"^\s*(?:{products})(?:\s+version)?\s*"
        rf"(?:[/=: -]\s*)?v?(?P<version>\d+\.\d+\.\d+)(?!\d)",
        re.IGNORECASE | re.MULTILINE,
    )
    versions = {
        tuple(int(part) for part in match.group("version").split("."))
        for match in pattern.finditer(output)
    }
    if len(versions) != 1:
        return None
    return next(iter(versions))


def _runtime_or_default(runtime: _DetectorRuntime | None) -> _DetectorRuntime:
    return runtime or _DetectorRuntime(
        SubprocessRunner(),
        LocalFilesystem(),
        shutil.which,
        MappingProxyType(dict(os.environ)),
        platform.system(),
        time.monotonic,
        MappingProxyType({}),
        None,
    )


def _executable_search(
    capability: CapabilityContract,
    context: DetectorContext,
    row: DetectorContract,
    runtime: _DetectorRuntime,
) -> CandidateSearchResult:
    return bounded_candidates(
        context,
        row.executable_names,
        filesystem=runtime.filesystem,
        which=runtime.which,
        environ=runtime.environ,
        system=runtime.system,
        clock=runtime.clock,
    )


def _python_runtime_search(
    context: DetectorContext,
    runtime: _DetectorRuntime,
) -> CandidateSearchResult:
    return bounded_candidates(
        context,
        ("python.exe", "python3", "python"),
        filesystem=runtime.filesystem,
        which=lambda name: None,
        environ={},
        system=runtime.system,
        clock=runtime.clock,
        _roots_only=(
            context.root / "workspace" / "shared" / "runtimes",
        ),
        _collect_all=True,
    )


def _jenkins_version(value: str | None) -> tuple[int, int, int] | None:
    if value is None:
        return None
    match = _JENKINS_VERSION.fullmatch(value.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def _jenkins_readiness(
    capability: CapabilityContract,
    runtime: _DetectorRuntime,
    base_details: tuple[str, ...],
) -> CapabilityReadiness:
    snapshot = runtime.config_snapshots.get("main")
    if snapshot is None or not isinstance(
        snapshot.parsed, _JenkinsSnapshotConfig
    ):
        return CapabilityReadiness(
            capability.id,
            False,
            "needs_config",
            base_details + ("config=main",),
        )
    config = snapshot.parsed
    probe_details = base_details + (
        "probe=read_only",
        "endpoint=controller_metadata",
    )
    if config.ca_bundle is not None:
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            probe_details + ("tls=custom_ca_unverified",),
        )
    controller = snapshot.parsed.controller
    try:
        credentials = jenkins_config.resolve_credentials(
            controller,
            environ=runtime.environ,
        )
    except jenkins_config.ConfigError:
        return CapabilityReadiness(
            capability.id,
            False,
            "not_ready",
            probe_details + ("credentials=missing",),
        )
    auth = (
        None
        if not credentials.username and not credentials.token
        else httpx.BasicAuth(credentials.username, credentials.token)
    )

    endpoint = config.url + "/api/json"
    started = runtime.clock()
    try:
        with httpx.Client(
            transport=runtime.http_transport,
            timeout=httpx.Timeout(
                connect=_PROBE_TIMEOUT,
                read=_JENKINS_READ_TIMEOUT_SECONDS,
                write=_JENKINS_READ_TIMEOUT_SECONDS,
                pool=_JENKINS_READ_TIMEOUT_SECONDS,
            ),
            follow_redirects=False,
            verify=True,
            trust_env=False,
        ) as client:
            with client.stream(
                "GET",
                endpoint,
                params={"tree": "mode"},
                auth=auth,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
            ) as response:
                if (
                    runtime.clock() - started
                    >= _JENKINS_PROBE_TOTAL_SECONDS
                ):
                    return CapabilityReadiness(
                        capability.id,
                        False,
                        "manual_check_required",
                        probe_details + ("probe=timeout",),
                    )
                if response.status_code in {401, 403}:
                    return CapabilityReadiness(
                        capability.id,
                        False,
                        "not_ready",
                        probe_details + ("authentication=failed",),
                    )
                if not 200 <= response.status_code < 300:
                    return CapabilityReadiness(
                        capability.id,
                        False,
                        "not_ready",
                        probe_details
                        + (f"http_status={response.status_code}",),
                    )
                body = bytearray()
                chunks = iter(response.iter_bytes())
                while True:
                    if (
                        runtime.clock() - started
                        >= _JENKINS_PROBE_TOTAL_SECONDS
                    ):
                        return CapabilityReadiness(
                            capability.id,
                            False,
                            "manual_check_required",
                            probe_details + ("probe=timeout",),
                        )
                    try:
                        chunk = next(chunks)
                    except StopIteration:
                        break
                    if (
                        runtime.clock() - started
                        >= _JENKINS_PROBE_TOTAL_SECONDS
                    ):
                        return CapabilityReadiness(
                            capability.id,
                            False,
                            "manual_check_required",
                            probe_details + ("probe=timeout",),
                        )
                    if len(body) + len(chunk) > _MAX_JENKINS_RESPONSE_BYTES:
                        return CapabilityReadiness(
                            capability.id,
                            False,
                            "manual_check_required",
                            probe_details + ("response=too_large",),
                        )
                    body.extend(chunk)
                version = _jenkins_version(response.headers.get("X-Jenkins"))
    except httpx.TransportError:
        return CapabilityReadiness(
            capability.id,
            False,
            "not_ready",
            probe_details + ("connection=failed",),
        )
    except httpx.HTTPError:
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            probe_details + ("probe=unclassified",),
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            probe_details + ("response=unclassified",),
        )
    if (
        not isinstance(payload, Mapping)
        or not isinstance(payload.get("mode"), str)
        or not payload["mode"].strip()
    ):
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            probe_details + ("response=unclassified",),
        )
    if version is None:
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            probe_details + ("version=unverified",),
        )
    version_detail = "version=" + ".".join(map(str, version))
    status = (
        "ready"
        if version >= _minimum_version(capability)
        else "not_ready"
    )
    return CapabilityReadiness(
        capability.id,
        False,
        status,
        probe_details + (version_detail,),
    )


def _detect_capability(
    capability: CapabilityContract,
    context: DetectorContext,
    *,
    runtime: _DetectorRuntime | None = None,
) -> CapabilityReadiness:
    active = _runtime_or_default(runtime)
    row = CAPABILITY_DETECTOR_CONTRACTS[capability.id]
    base_details = (f"detector={row.detector_type}",)

    if row.detector_type == "external-service":
        if (
            row.config_label is not None
            and row.config_label not in active.config_snapshots
        ):
            return CapabilityReadiness(
                capability.id,
                False,
                "needs_config",
                base_details + (f"config={row.config_label}",),
            )
        if capability.id == "app.jenkins":
            return _jenkins_readiness(capability, active, base_details)
        if row.executable_names:
            search = _executable_search(capability, context, row, active)
            if search.status == "manual_check_required":
                return CapabilityReadiness(
                    capability.id, False, search.status, base_details + search.details
                )
            if not search.candidates:
                return CapabilityReadiness(
                    capability.id,
                    False,
                    "not_ready",
                    base_details + ("executable=not_found",),
                )
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            base_details + ("service_probe=not_performed",),
        )

    if row.detector_type == "python-import":
        if row.import_name is None or row.distribution_name is None:
            return CapabilityReadiness(
                capability.id,
                False,
                "manual_check_required",
                base_details + ("metadata=incomplete",),
            )
        probe_contract = PYTHON_IMPORT_PROBES.get(capability.id)
        if probe_contract is None:
            return CapabilityReadiness(
                capability.id,
                False,
                "manual_check_required",
                base_details + ("python_probe=unavailable",),
            )
        probe = _python_probe_source(probe_contract)

        manual_failure = False
        detected_versions: list[tuple[int, int, int]] = []
        attempted: set[str] = set()

        def probe_interpreter(interpreter: Path) -> CapabilityReadiness | None:
            nonlocal manual_failure
            key = _path_key(interpreter, system=active.system)
            if key in attempted:
                return None
            attempted.add(key)
            argv = [
                str(interpreter)
                if token == "{python}"
                else probe
                if token == "{import-metadata}"
                else token
                for token in row.version_argv
            ]
            result = active.runner.run(
                argv, timeout=_PROBE_TIMEOUT, output_limit=_OUTPUT_LIMIT
            )
            if result.returncode in {
                -1,
                RUNNER_SPAWN_ERROR,
                RUNNER_TIMEOUT,
                RUNNER_OUTPUT_LIMIT,
            }:
                manual_failure = True
                return None
            if result.returncode != 0:
                return None
            version = _detected_product_version(
                result.output, row.product_aliases
            )
            if version is None:
                manual_failure = True
                return None
            detected_versions.append(version)
            if version < _minimum_version(capability):
                return None
            return CapabilityReadiness(
                capability.id,
                False,
                "ready",
                base_details + (
                    "version=" + ".".join(map(str, version)),
                    f"interpreter={interpreter.resolve(strict=False)}",
                ),
            )

        current = probe_interpreter(Path(sys.executable))
        if current is not None:
            return current
        search = _python_runtime_search(context, active)
        if search.status == "manual_check_required":
            return CapabilityReadiness(
                capability.id,
                False,
                "manual_check_required",
                base_details + search.details,
            )
        for interpreter in search.candidates:
            detected = probe_interpreter(interpreter)
            if detected is not None:
                return detected
        if manual_failure:
            return CapabilityReadiness(
                capability.id,
                False,
                "manual_check_required",
                base_details + ("python_import=unverified",),
            )
        if detected_versions:
            version = max(detected_versions)
            detail = "version=" + ".".join(map(str, version))
        else:
            detail = "python_import=unavailable"
        return CapabilityReadiness(
            capability.id,
            False,
            "not_ready",
            base_details + (detail,),
        )

    search = _executable_search(capability, context, row, active)
    if search.status == "manual_check_required":
        return CapabilityReadiness(
            capability.id, False, search.status, base_details + search.details
        )
    if not search.candidates:
        return CapabilityReadiness(
            capability.id,
            False,
            "not_ready",
            base_details + ("executable=not_found",),
        )
    executable = search.candidates[0]
    path_detail = f"path={executable}"
    if row.detector_type == "mcp-config":
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            base_details + (path_detail, "host_registration=unverified"),
        )
    if not row.version_argv:
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            base_details + (path_detail, "version=unverified"),
        )
    argv = [
        str(executable) if token == "{executable}" else token
        for token in row.version_argv
    ]
    result = active.runner.run(
        argv, timeout=_PROBE_TIMEOUT, output_limit=_OUTPUT_LIMIT
    )
    if result.returncode in {
        -1,
        RUNNER_SPAWN_ERROR,
        RUNNER_TIMEOUT,
        RUNNER_OUTPUT_LIMIT,
    }:
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            base_details + (path_detail, "probe=unverified"),
        )
    if result.returncode != 0:
        return CapabilityReadiness(
            capability.id,
            False,
            "not_ready",
            base_details + (path_detail, "probe=failed"),
        )
    if row.version_parser == "presence":
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            base_details + (path_detail, "version=unverified"),
        )
    version = _detected_product_version(
        result.output, row.product_aliases
    )
    if version is None:
        return CapabilityReadiness(
            capability.id,
            False,
            "manual_check_required",
            base_details + (path_detail, "version=unverified"),
        )
    status = "ready" if version >= _minimum_version(capability) else "not_ready"
    return CapabilityReadiness(
        capability.id,
        False,
        status,
        base_details
        + (path_detail, "version=" + ".".join(map(str, version))),
    )


def _detector(capability: CapabilityContract, context: DetectorContext, *, runtime=None):
    return _detect_capability(capability, context, runtime=runtime)


detect_bambu_studio = _detector
detect_blender = _detector
detect_jenkins = _detector
detect_obsidian = _detector
detect_orcaslicer = _detector
detect_playwright = _detector
detect_prusaslicer = _detector
detect_agent_browser = _detector
detect_ffmpeg = _detector
detect_playwright_cli = _detector
detect_tavily_search = _detector
detect_tesseract = _detector
detect_umi_ocr = _detector
detect_blender_mcp = _detector
detect_funasr = _detector
detect_paddleocr = _detector
detect_voxcpm = _detector
detect_asyncssh = _detector
detect_lancedb = _detector
detect_pillow = _detector

DETECTORS: Mapping[
    str, Callable[[CapabilityContract, DetectorContext], CapabilityReadiness]
] = MappingProxyType(
    {
        "app.bambu-studio": detect_bambu_studio,
        "app.blender": detect_blender,
        "app.jenkins": detect_jenkins,
        "app.obsidian": detect_obsidian,
        "app.orcaslicer": detect_orcaslicer,
        "app.playwright": detect_playwright,
        "app.prusaslicer": detect_prusaslicer,
        "cli.agent-browser": detect_agent_browser,
        "cli.ffmpeg": detect_ffmpeg,
        "cli.playwright-cli": detect_playwright_cli,
        "cli.tavily-search": detect_tavily_search,
        "cli.tesseract": detect_tesseract,
        "cli.umi-ocr": detect_umi_ocr,
        "mcp.blender": detect_blender_mcp,
        "model.funasr": detect_funasr,
        "model.paddleocr": detect_paddleocr,
        "model.voxcpm": detect_voxcpm,
        "python.asyncssh": detect_asyncssh,
        "python.lancedb": detect_lancedb,
        "python.pillow": detect_pillow,
    }
)
DETECTOR_CONTRACTS = CAPABILITY_DETECTOR_CONTRACTS


def _find_workflow(
    catalog: RepositoryCatalog, workflow_name: str
) -> WorkflowDescriptor:
    workflow = next(
        (item for item in catalog.workflows if item.name == workflow_name), None
    )
    if workflow is None:
        raise WorkflowDoctorError("unknown workflow")
    return workflow


class _InvalidConfigSnapshot(Exception):
    pass


def _strict_ini(text: str, *, preserve_case: bool) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        allow_no_value=False,
    )
    if preserve_case:
        parser.optionxform = str
    parser.read_string(text)
    return parser


def _required_ini(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
) -> str:
    if not parser.has_option(section, option):
        raise ValueError("required config field is missing")
    value = parser.get(section, option, raw=True).strip()
    if not value:
        raise ValueError("required config field is blank")
    return value


def _relative_reference(value: str) -> str:
    path = Path(value)
    if (
        path.is_absolute()
        or path.drive
        or path.anchor
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("config reference must be a safe relative path")
    return value


def _path_reference(value: str) -> Path:
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError("config path is invalid")
    if ntpath.isabs(value) or posixpath.isabs(value):
        return Path(value)
    _relative_reference(value)
    return Path(value)


_ENVIRONMENT_PROVISIONABLE_TOOLS = frozenset({"git"})
_ENVIRONMENT_OBSERVABLE_TOOLS = frozenset(
    {"git", "java", "jdk", "wireshark"}
)
_ENVIRONMENT_REMOTE_COMMANDS = frozenset(
    {
        "hostname",
        "ipconfig /all",
        "Get-NetIPConfiguration",
        "ip -brief address",
        "cat /etc/resolv.conf",
    }
)
_ENVIRONMENT_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENVIRONMENT_COMMAND_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")


def _environment_value(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
    default: str | None = None,
) -> str | None:
    if not parser.has_section(section) or not parser.has_option(
        section, option
    ):
        return default
    value = parser.get(section, option, raw=True).strip()
    return value or default


def _environment_required(
    parser: configparser.ConfigParser,
    section: str,
    option: str,
) -> str:
    value = _environment_value(parser, section, option)
    if value is None:
        raise ValueError("required environment config field is missing")
    return value


def _environment_boolean(
    value: str | None,
    *,
    default: bool = False,
) -> bool:
    if value is None:
        return default
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError("environment boolean is invalid")


def _environment_positive_float(
    value: str | None,
    *,
    default: float,
) -> float:
    if value is None:
        return default
    number = float(value)
    if number <= 0 or number > 300:
        raise ValueError("environment timeout is out of range")
    return number


def _environment_integer(
    value: str | None,
    *,
    minimum: int,
) -> int | None:
    if value is None:
        return None
    number = int(value)
    if number < minimum or number > 65535:
        raise ValueError("environment integer is out of range")
    return number


def _environment_path(value: str) -> Path:
    if not value or any(character in value for character in "\r\n\x00"):
        raise ValueError("environment path is invalid")
    return Path(value)


def _environment_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("environment service URL is invalid")
    return value.rstrip("/")


def _environment_host(value: str) -> str:
    if (
        not value
        or value.startswith("-")
        or any(character.isspace() for character in value)
        or any(character in value for character in "@/\\?#")
    ):
        raise ValueError("environment host is invalid")
    return value


def _environment_variable(value: str | None) -> str | None:
    if value is not None and _ENVIRONMENT_VARIABLE.fullmatch(value) is None:
        raise ValueError("environment variable name is invalid")
    return value


def _environment_tool_names(
    value: str | None,
    supported: frozenset[str],
) -> tuple[str, ...]:
    names = tuple(
        item.strip().casefold()
        for item in (value or "").split(",")
        if item.strip()
    )
    if set(names) - supported:
        raise ValueError("environment tool is unsupported")
    return names


def _validate_environment_jenkins(
    parser: configparser.ConfigParser,
) -> None:
    if not parser.has_section("jenkins"):
        return
    url = _environment_url(
        _environment_required(parser, "jenkins", "url")
    )
    name = _environment_value(parser, "jenkins", "name")
    environment = _environment_value(parser, "jenkins", "environment")
    policy_file = _environment_value(parser, "jenkins", "policy_file")
    ca_bundle = _environment_value(parser, "jenkins", "ca_bundle")
    _environment_boolean(
        _environment_value(parser, "jenkins", "allow_insecure_http")
    )
    if any(
        value is not None
        for value in (name, environment, policy_file, ca_bundle)
    ):
        if name is None or policy_file is None:
            raise ValueError("Jenkins operation fields are incomplete")
        if environment not in {"nonproduction", "production"}:
            raise ValueError("Jenkins environment is invalid")
    if policy_file is not None:
        _environment_path(policy_file)
    if ca_bundle is not None:
        if urlparse(url).scheme != "https":
            raise ValueError("Jenkins CA requires HTTPS")
        _environment_path(ca_bundle)

    username = _environment_value(parser, "jenkins", "username")
    token = _environment_value(parser, "jenkins", "token")
    password = _environment_value(parser, "jenkins", "password")
    username_env = _environment_value(parser, "jenkins", "username_env")
    token_env = _environment_value(parser, "jenkins", "token_env")
    direct = any(value is not None for value in (username, token, password))
    indirect = any(value is not None for value in (username_env, token_env))
    if (direct and indirect) or (
        token is not None and password is not None
    ):
        raise ValueError("Jenkins credential sources conflict")
    if direct and (username is None or (token is None and password is None)):
        raise ValueError("Jenkins direct credentials are incomplete")
    if indirect and (username_env is None or token_env is None):
        raise ValueError("Jenkins environment credentials are incomplete")
    if indirect:
        _environment_variable(username_env)
        _environment_variable(token_env)


def _validate_environment_database(
    parser: configparser.ConfigParser,
) -> None:
    if not parser.has_section("database"):
        return
    engine = _environment_required(parser, "database", "engine").casefold()
    if engine not in {"sqlite", "postgresql", "mysql", "mssql"}:
        raise ValueError("database engine is invalid")
    database_path = _environment_value(parser, "database", "path")
    host = _environment_value(parser, "database", "host")
    tls_ca = _environment_value(parser, "database", "tls_ca_file")
    server_name = _environment_value(
        parser, "database", "tls_server_name"
    )
    if engine == "sqlite":
        if database_path is None:
            raise ValueError("SQLite path is required")
        _environment_path(database_path)
    else:
        if host is None:
            raise ValueError("network database host is required")
        _environment_host(host)
    if engine in {"postgresql", "mysql"} and tls_ca is None:
        raise ValueError("database TLS CA is required")
    if tls_ca is not None:
        _environment_path(tls_ca)
    if server_name is not None:
        if engine != "mssql":
            raise ValueError("database TLS server name is unsupported")
        _environment_host(server_name)
    _environment_integer(
        _environment_value(parser, "database", "port"),
        minimum=1,
    )
    _environment_variable(
        _environment_value(parser, "database", "credential_env")
    )
    _environment_variable(
        _environment_value(parser, "database", "username_env")
    )


def _validate_mysql_main(text: str, _path: Path) -> object:
    parser = _strict_ini(text, preserve_case=True)
    if parser.sections() != ["mysql"] or parser.defaults():
        raise ValueError("MySQL config requires only mysql section")
    if set(parser.options("mysql")) - mysql_config._CONFIG_OPTIONS:
        raise ValueError("unknown MySQL config option")
    mysql_config._identifier(mysql_config._required_value(parser, "name"), option="name")
    environment = mysql_config._identifier(
        mysql_config._required_value(parser, "environment"),
        option="environment",
    )
    mysql_config._safe_host(mysql_config._required_value(parser, "host"))
    mysql_config._port(mysql_config._required_value(parser, "port"))
    mysql_config._identifier(
        mysql_config._required_value(parser, "database"),
        option="database",
    )
    tls_verify = mysql_config._boolean_value(
        parser, "tls_verify", default=True
    )
    allow_insecure = mysql_config._boolean_value(
        parser, "allow_insecure_tls", default=False
    )
    if not tls_verify and (not allow_insecure or environment.casefold() == "production"):
        raise ValueError("unsafe MySQL TLS configuration")
    if tls_verify and allow_insecure:
        raise ValueError("conflicting MySQL TLS configuration")
    mysql_config._positive_integer(
        mysql_config._value(parser, "connect_timeout_seconds"),
        option="connect_timeout_seconds",
        default=10,
    )
    _relative_reference(mysql_config._required_value(parser, "policy_file"))
    mysql_config._environment_list(
        mysql_config._required_value(parser, "read_only_environments")
    )
    for option in ("ca_bundle", "migrations_dir"):
        value = mysql_config._value(parser, option)
        if value is not None:
            _relative_reference(value)
    ledger = mysql_config._value(parser, "migration_ledger_table")
    if ledger is not None:
        mysql_config._identifier(ledger, option="migration_ledger_table")
    mysql_config._parse_credentials(parser)
    return parser


def _validate_mysql_target(text: str, _path: Path) -> object:
    parser = _strict_ini(text, preserve_case=True)
    if not parser.has_section("environment"):
        raise ValueError("MySQL target config requires an environment section")
    if not parser.has_section("target.mysql"):
        raise ValueError("MySQL target config requires a target.mysql section")
    unknown_options = (
        set(parser.options("target.mysql")) - mysql_config._TARGET_OPTIONS
    )
    if unknown_options:
        raise ValueError(
            "MySQL target config contains unknown [target.mysql] options"
        )
    mysql_config._identifier(
        mysql_config._required_value(parser, "name", section="environment"),
        option="name",
    )
    mysql_config._safe_host(
        mysql_config._required_value(parser, "host", section="target.mysql")
    )
    mysql_config._port(
        mysql_config._required_value(parser, "port", section="target.mysql")
    )
    mysql_config._optional_identifier(
        parser, "database", section="target.mysql"
    )
    return parser


def _validate_jenkins_main(text: str, _path: Path) -> object:
    loaded = jenkins_config.load_config(_path)
    if len(loaded.controllers) != 1:
        raise ValueError("Jenkins config must declare exactly one controller")
    controller = next(iter(loaded.controllers.values()))
    return _JenkinsSnapshotConfig(
        controller.name,
        controller.url,
        controller.environment,
        controller.allow_insecure_http,
        controller.require_crumb,
        controller.ca_bundle,
        controller.confirm_writes,
        controller,
    )


def _portable_absolute_path(value: str, *, option: str) -> str:
    if not (ntpath.isabs(value) or posixpath.isabs(value)):
        raise ValueError(f"{option} must be absolute")
    if ntpath.splitext(value)[1].casefold() in {".cmd", ".bat", ".ps1"}:
        raise ValueError("runtime wrappers are unsupported")
    return value


def _parse_policy_text(text: str, parser: Callable[[Mapping], object]) -> object:
    raw = yaml.safe_load(text)
    if not isinstance(raw, Mapping):
        raise ValueError("policy must be a mapping")
    if type(raw.get("version")) is not int:
        raise ValueError("policy version must be an integer")
    return parser(raw)


def _validate_mysql_policy(text: str, _path: Path) -> object:
    return _parse_policy_text(text, parse_mysql_policy)


def _validate_jenkins_policy(text: str, _path: Path) -> object:
    return _parse_policy_text(text, parse_jenkins_policy)


def _validate_ssh_main(_text: str, path: Path) -> object:
    return ssh_config.load_config(path)


CONFIG_SCHEMA_VALIDATORS: Mapping[
    tuple[str, str], Callable[[str, Path], object]
] = MappingProxyType(
    {
        ("jenkins-operations", "main"): _validate_jenkins_main,
        ("jenkins-operations", "policy"): _validate_jenkins_policy,
        ("mysql-operations", "main"): _validate_mysql_main,
        ("mysql-operations", "policy"): _validate_mysql_policy,
        ("mysql-operations", "target"): _validate_mysql_target,
        ("ssh-operations", "main"): _validate_ssh_main,
    }
)


def _validate_optional_config_syntax(
    template: ConfigTemplateDescriptor,
    text: str,
) -> object:
    suffix = Path(template.output_name).suffix.casefold()
    if suffix == ".ini":
        parser = _strict_ini(text, preserve_case=True)
        if not parser.sections():
            raise ValueError("optional INI config requires a section")
        return parser
    if suffix in {".yaml", ".yml"}:
        parsed = yaml.safe_load(text)
        if not isinstance(parsed, Mapping):
            raise ValueError("optional YAML config requires a mapping")
        return parsed
    raise ValueError("optional config suffix is unsupported")


def _read_config_snapshot(
    path: Path,
    *,
    workflow_name: str,
    label: str,
) -> tuple[str, Path]:
    normalized = Path(os.path.abspath(path))
    anchor = Path(normalized.anchor)
    if not normalized.anchor:
        raise _InvalidConfigSnapshot
    try:
        snapshot = load_text_snapshot(
            anchor,
            normalized,
            f"workflow config {workflow_name}/{label}",
            max_bytes=_MAX_CONFIG_BYTES,
        )
    except (ContractError, OSError, RuntimeError, ValueError):
        raise _InvalidConfigSnapshot from None
    return snapshot.content, normalized


def _config_facts(
    catalog: RepositoryCatalog,
    workflow: WorkflowDescriptor,
    config_paths: Mapping[str, Path],
) -> tuple[
    tuple[ConfigReadiness, ...],
    Mapping[str, Path],
    Mapping[str, _ConfigSnapshot],
]:
    declared = {template.label: template for template in workflow.config_templates}
    if set(config_paths) - set(declared):
        raise WorkflowDoctorError("unknown config label")
    normalized: dict[str, Path] = {}
    snapshots: dict[str, _ConfigSnapshot] = {}
    readiness: list[ConfigReadiness] = []
    root = Path(os.path.abspath(catalog.root))
    for template in workflow.config_templates:
        supplied = config_paths.get(template.label)
        if supplied is None:
            readiness.append(
                ConfigReadiness(
                    template.label,
                    template.required,
                    template.scope,
                    "needs_config",
                    None,
                )
            )
            continue
        path = Path(supplied)
        if not path.is_absolute():
            raise WorkflowDoctorError("config path must be absolute")
        normalized_path = Path(os.path.abspath(path))
        if template.scope == "repository-external" and (
            normalized_path == root or normalized_path.is_relative_to(root)
        ):
            raise WorkflowDoctorError(
                "repository-external config must resolve outside repository"
            )
        try:
            text, normalized_path = _read_config_snapshot(
                normalized_path,
                workflow_name=workflow.name,
                label=template.label,
            )
        except _InvalidConfigSnapshot:
            readiness.append(
                ConfigReadiness(
                    template.label,
                    template.required,
                    template.scope,
                    "invalid_config",
                    None,
                )
            )
            continue
        validator = CONFIG_SCHEMA_VALIDATORS.get(
            (workflow.name, template.label)
        )
        try:
            if validator is not None:
                parsed = validator(text, normalized_path)
            elif template.required:
                raise ValueError("required config schema is unavailable")
            else:
                parsed = _validate_optional_config_syntax(template, text)
        except (
            AttributeError,
            configparser.Error,
            OSError,
            RecursionError,
            TypeError,
            UnicodeError,
            ValueError,
            yaml.YAMLError,
        ):
            readiness.append(
                ConfigReadiness(
                    template.label,
                    template.required,
                    template.scope,
                    "invalid_config",
                    None,
                )
            )
            continue
        snapshot = _ConfigSnapshot(normalized_path, text, parsed)
        normalized[template.label] = normalized_path
        snapshots[template.label] = snapshot
        readiness.append(
            ConfigReadiness(
                template.label,
                template.required,
                template.scope,
                "ready",
                normalized_path,
            )
        )
    return (
        tuple(readiness),
        MappingProxyType(normalized),
        MappingProxyType(snapshots),
    )


def _validate_selections(
    workflow: WorkflowDescriptor,
    selected_capabilities: Mapping[str, str],
) -> None:
    for slot, capability_id in selected_capabilities.items():
        if slot not in workflow.capability_slots:
            raise WorkflowDoctorError("unknown capability slot")
        if capability_id not in workflow.capability_slots[slot]:
            raise WorkflowDoctorError("capability does not match slot")


def _invoke_detector(
    detector,
    capability: CapabilityContract,
    context: DetectorContext,
    runtime: _DetectorRuntime,
) -> CapabilityReadiness:
    if detector in DETECTORS.values():
        return detector(capability, context, runtime=runtime)
    return detector(capability, context)


def workflow_doctor(
    catalog: RepositoryCatalog,
    workflow_name: str,
    *,
    host: str,
    config_paths: Mapping[str, Path],
    selected_capabilities: Mapping[str, str],
    detectors: Mapping[str, Callable] | None = None,
    runner: Runner | None = None,
    filesystem: Filesystem | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
    system: str | None = None,
    clock: Callable[[], float] | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> WorkflowDoctorReport:
    workflow = _find_workflow(catalog, workflow_name)
    if host not in PROJECT_HOSTS:
        raise WorkflowDoctorError("unknown host")
    if host not in workflow.supported_hosts:
        raise WorkflowDoctorError("unsupported workflow host")
    _validate_selections(workflow, selected_capabilities)
    configs, normalized_configs, config_snapshots = _config_facts(
        catalog, workflow, config_paths
    )
    required_config_block = any(
        config.required and config.status != "ready" for config in configs
    )
    context = DetectorContext(
        catalog.root, normalized_configs, selected_capabilities
    )
    active_detectors = DETECTORS if detectors is None else detectors
    runtime = _DetectorRuntime(
        runner or SubprocessRunner(),
        filesystem or LocalFilesystem(),
        which or shutil.which,
        MappingProxyType(dict(os.environ) if environ is None else dict(environ)),
        system or platform.system(),
        clock or time.monotonic,
        config_snapshots,
        http_transport,
    )

    capabilities: list[CapabilityReadiness] = []
    selected_ids = set(selected_capabilities.values())
    for capability_id in workflow.required_capabilities:
        capability = catalog.capabilities[capability_id]
        detector = active_detectors.get(capability_id)
        if required_config_block:
            result = CapabilityReadiness(
                capability_id,
                True,
                "needs_config",
                ("workflow_config=not_ready",),
            )
        elif detector is None:
            result = CapabilityReadiness(
                capability_id,
                True,
                "manual_check_required",
                ("detector=unavailable",),
            )
        else:
            detected = _invoke_detector(detector, capability, context, runtime)
            result = CapabilityReadiness(
                detected.capability_id, True, detected.status, tuple(detected.details)
            )
        capabilities.append(result)

    for slot in sorted(workflow.capability_slots):
        capability_id = selected_capabilities.get(slot)
        if capability_id is None:
            capabilities.append(
                CapabilityReadiness(
                    slot, False, "not_selected", (f"slot={slot}",)
                )
            )
            continue
        capability = catalog.capabilities[capability_id]
        detector = active_detectors.get(capability_id)
        if required_config_block:
            result = CapabilityReadiness(
                capability_id,
                False,
                "needs_config",
                ("workflow_config=not_ready", f"slot={slot}"),
            )
        elif detector is None:
            result = CapabilityReadiness(
                capability_id,
                False,
                "manual_check_required",
                ("detector=unavailable", f"slot={slot}"),
            )
        else:
            detected = _invoke_detector(detector, capability, context, runtime)
            result = CapabilityReadiness(
                detected.capability_id,
                False,
                detected.status,
                tuple(detected.details) + (f"slot={slot}",),
            )
        capabilities.append(result)

    capability_block = any(
        item.status != "ready"
        and item.status != "not_selected"
        and (item.required or item.capability_id in selected_ids)
        for item in capabilities
    )
    optional_gap = any(
        (not config.required and config.status != "ready") for config in configs
    ) or any(item.status == "not_selected" for item in capabilities)
    if required_config_block or capability_block:
        status = "not_ready"
    elif optional_gap:
        status = "ready_with_optional_gaps"
    else:
        status = "ready"

    relevant_capabilities = [
        catalog.capabilities[capability_id].hosts
        for capability_id in workflow.required_capabilities
    ]
    relevant_capabilities.extend(
        catalog.capabilities[capability_id].hosts
        for capability_id in selected_capabilities.values()
    )
    adapter_status = doctor(catalog.root, host=host).status
    compatibility = host_compatibility(
        PROJECT_HOSTS,
        frozenset(workflow.supported_hosts),
        relevant_capabilities,
        {host: "verified" if adapter_status == "verified" else "unverified"},
    )[host]
    return WorkflowDoctorReport(
        workflow.name,
        status,
        configs,
        tuple(capabilities),
        compatibility,
    )


doctor_workflow = workflow_doctor
diagnose_workflow = workflow_doctor
DETECTOR_CONTRACT_TABLE = CAPABILITY_DETECTOR_CONTRACTS
