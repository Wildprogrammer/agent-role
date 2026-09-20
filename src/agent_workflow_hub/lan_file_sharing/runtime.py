from __future__ import annotations

import hashlib
import hmac
import io
import ntpath
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import uuid
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx

from .assets import (
    DUFS_VERSION,
    MAX_ASSET_SIZE,
    DufsAsset,
    load_asset_manifest,
    select_asset,
)
_DOWNLOAD_SLACK = 64 * 1024
_MAX_EXECUTABLE_SIZE = 32 * 1024 * 1024
_MAX_REDIRECTS = 5
_VERSION_TIMEOUT = 5
_VERSION_OUTPUT_LIMIT = 65_536
_VERSION_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])dufs(?:\s+version)?\s+v?(\d+\.\d+\.\d+)"
    r"(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RuntimeInstallError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RunnerResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        output_limit: int,
    ) -> RunnerResult: ...


@dataclass(frozen=True, slots=True)
class RuntimeInspection:
    status: str
    executable: str
    version: str | None
    sha256: str | None
    expected_sha256: str
    asset_url: str

    def to_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "executable": self.executable,
            "version": self.version,
            "sha256": self.sha256,
            "expected_sha256": self.expected_sha256,
            "asset_url": self.asset_url,
        }


class SubprocessVersionRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        output_limit: int,
    ) -> RunnerResult:
        try:
            completed = subprocess.run(
                list(argv),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Dufs version probe timed out") from exc
        if len(completed.stdout) + len(completed.stderr) > output_limit:
            raise ValueError("Dufs version output exceeded the limit")
        return RunnerResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


def runtime_path(hub_root: Path, asset: DufsAsset) -> Path:
    if not isinstance(hub_root, Path) or not hub_root.is_absolute():
        raise RuntimeInstallError("hub_root must be an absolute Path")
    if not isinstance(asset, DufsAsset):
        raise RuntimeInstallError("asset must be a DufsAsset")
    return (
        hub_root
        / "workspace"
        / "shared"
        / "runtimes"
        / "dufs"
        / DUFS_VERSION
        / f"{asset.system}-{asset.machine}"
        / asset.executable
    )


def installation_candidate(
    hub_root: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    """Describe the one pinned artifact whose archive digest must be confirmed."""

    if not isinstance(hub_root, Path) or not hub_root.is_absolute():
        raise RuntimeInstallError("hub_root must be an absolute Path")
    manifest = load_asset_manifest(
        hub_root / "capabilities" / "cli" / "dufs" / "assets-v0.46.0.json"
    )
    asset = select_asset(
        manifest,
        system=system or platform.system(),
        machine=machine or platform.machine(),
    )
    destination = runtime_path(hub_root, asset)
    return {
        "system": asset.system,
        "machine": asset.machine,
        "version": DUFS_VERSION,
        "filename": asset.filename,
        "url": asset.url,
        "size": asset.size,
        "archive_sha256": asset.archive_sha256,
        "executable_sha256": asset.executable_sha256,
        "destination": str(destination),
        "rollback_path": str(destination.parent),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _is_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISREG(metadata.st_mode) and not bool(attributes & reparse_flag)


def parse_exact_dufs_version(result: RunnerResult) -> str | None:
    if not isinstance(result, RunnerResult) or result.returncode != 0:
        return None
    matches = _VERSION_PATTERN.findall(f"{result.stdout}\n{result.stderr}")
    if len(matches) != 1:
        return None
    return matches[0]


def inspect_runtime(
    hub_root: Path,
    asset: DufsAsset,
    *,
    runner: Runner | None = None,
) -> RuntimeInspection:
    executable = runtime_path(hub_root, asset)
    if not _is_regular_file(executable):
        return RuntimeInspection(
            status="needs_dependency",
            executable=str(executable),
            version=None,
            sha256=None,
            expected_sha256=asset.executable_sha256,
            asset_url=asset.url,
        )
    try:
        digest = file_sha256(executable)
    except OSError:
        return RuntimeInspection(
            status="not_ready",
            executable=str(executable),
            version=None,
            sha256=None,
            expected_sha256=asset.executable_sha256,
            asset_url=asset.url,
        )
    if not hmac.compare_digest(digest, asset.executable_sha256):
        return RuntimeInspection(
            status="not_ready",
            executable=str(executable),
            version=None,
            sha256=digest,
            expected_sha256=asset.executable_sha256,
            asset_url=asset.url,
        )
    active_runner = runner or SubprocessVersionRunner()
    try:
        result = active_runner.run(
            (str(executable), "--version"),
            timeout=_VERSION_TIMEOUT,
            output_limit=_VERSION_OUTPUT_LIMIT,
        )
        version = parse_exact_dufs_version(result)
    except (OSError, ValueError):
        version = None
    return RuntimeInspection(
        status="ready" if version == DUFS_VERSION else "not_ready",
        executable=str(executable),
        version=version,
        sha256=digest,
        expected_sha256=asset.executable_sha256,
        asset_url=asset.url,
    )


def _validate_download_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise RuntimeInstallError("invalid download URL") from exc
    if parsed.scheme != "https":
        raise RuntimeInstallError("download URL must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise RuntimeInstallError("download URL must not contain credentials")
    host = parsed.hostname
    if host is None:
        raise RuntimeInstallError("download URL must contain a host")
    normalized_host = host.casefold()
    allowed = normalized_host == "github.com" or (
        normalized_host.endswith(".githubusercontent.com")
        and normalized_host != "githubusercontent.com"
    )
    if not allowed:
        raise RuntimeInstallError("download URL host is not allowed")
    if parsed.port not in (None, 443):
        raise RuntimeInstallError("download URL port is not allowed")
    return value


def _download_archive(
    asset: DufsAsset,
    *,
    transport: httpx.BaseTransport | None,
) -> bytes:
    current_url = _validate_download_url(asset.url)
    if type(asset.size) is not int or not 0 < asset.size <= MAX_ASSET_SIZE:
        raise RuntimeInstallError("asset declared size is invalid")
    maximum = min(asset.size + _DOWNLOAD_SLACK, MAX_ASSET_SIZE)
    seen: set[str] = set()
    client_options: dict[str, object] = {
        "follow_redirects": False,
        "timeout": httpx.Timeout(30.0),
        "trust_env": False,
    }
    if transport is not None:
        client_options["transport"] = transport
    try:
        with httpx.Client(**client_options) as client:
            for redirect_count in range(_MAX_REDIRECTS + 1):
                if current_url in seen:
                    raise RuntimeInstallError("download redirect loop detected")
                seen.add(current_url)
                with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location:
                            raise RuntimeInstallError(
                                "download redirect is missing Location"
                            )
                        if redirect_count >= _MAX_REDIRECTS:
                            raise RuntimeInstallError("download redirect limit exceeded")
                        next_url = _validate_download_url(
                            urljoin(current_url, location)
                        )
                        if next_url in seen:
                            raise RuntimeInstallError(
                                "download redirect loop detected"
                            )
                        current_url = next_url
                        continue
                    if response.status_code != 200:
                        raise RuntimeInstallError(
                            f"download failed with HTTP {response.status_code}"
                        )
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            declared_length = int(content_length)
                        except ValueError as exc:
                            raise RuntimeInstallError(
                                "download Content-Length is invalid"
                            ) from exc
                        if declared_length > maximum:
                            raise RuntimeInstallError(
                                "download exceeds declared size bound"
                            )
                    digest = hashlib.sha256()
                    buffer = bytearray()
                    for chunk in response.iter_bytes():
                        if len(buffer) + len(chunk) > maximum:
                            raise RuntimeInstallError(
                                "download exceeds declared size bound"
                            )
                        buffer.extend(chunk)
                        digest.update(chunk)
                    archive = bytes(buffer)
                    if len(archive) != asset.size:
                        raise RuntimeInstallError("download declared size mismatch")
                    if not hmac.compare_digest(
                        digest.hexdigest(), asset.archive_sha256
                    ):
                        raise RuntimeInstallError("download SHA-256 mismatch")
                    return archive
    except RuntimeInstallError:
        raise
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise RuntimeInstallError("unable to download Dufs artifact") from exc
    raise RuntimeInstallError("download redirect limit exceeded")


def _archive_member_name(value: str) -> tuple[str, tuple[str, ...]]:
    if not value or "\0" in value:
        raise RuntimeInstallError("invalid archive member path")
    normalized = value.replace("\\", "/")
    drive, _ = ntpath.splitdrive(normalized)
    if drive or normalized.startswith("/"):
        raise RuntimeInstallError("unsafe archive member path")
    parts = tuple(normalized.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeInstallError("unsafe archive member path")
    return "/".join(parts), parts


def _archive_directory_name(value: str) -> tuple[str, tuple[str, ...]]:
    normalized = value.replace("\\", "/").rstrip("/")
    return _archive_member_name(normalized)


def _read_bounded(stream: object, expected_size: int) -> bytes:
    if type(expected_size) is not int or not 0 <= expected_size <= _MAX_EXECUTABLE_SIZE:
        raise RuntimeInstallError("executable exceeds size limit")
    if not hasattr(stream, "read"):
        raise RuntimeInstallError("archive executable is unreadable")
    buffer = bytearray()
    while chunk := stream.read(1024 * 1024):
        if len(buffer) + len(chunk) > _MAX_EXECUTABLE_SIZE:
            raise RuntimeInstallError("executable exceeds size limit")
        buffer.extend(chunk)
    if len(buffer) != expected_size:
        raise RuntimeInstallError("archive executable size mismatch")
    return bytes(buffer)


def _zip_executable(archive: bytes, asset: DufsAsset) -> bytes:
    candidates: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(archive), "r") as container:
            for member in container.infolist():
                if member.is_dir():
                    normalized, parts = _archive_directory_name(member.filename)
                else:
                    normalized, parts = _archive_member_name(member.filename)
                identity = normalized.casefold()
                if identity in seen:
                    raise RuntimeInstallError("duplicate archive member path")
                seen.add(identity)
                mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise RuntimeInstallError("archive link or device is forbidden")
                if member.is_dir():
                    continue
                if parts[-1] == asset.executable:
                    candidates.append(member)
            if len(candidates) != 1:
                raise RuntimeInstallError(
                    "archive must contain exactly one executable entry"
                )
            candidate = candidates[0]
            with container.open(candidate, "r") as stream:
                return _read_bounded(stream, candidate.file_size)
    except RuntimeInstallError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise RuntimeInstallError("invalid ZIP archive") from exc


def _tar_executable(archive: bytes, asset: DufsAsset) -> bytes:
    candidates: list[tarfile.TarInfo] = []
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as container:
            for member in container.getmembers():
                normalized, parts = _archive_member_name(member.name)
                identity = normalized.casefold()
                if identity in seen:
                    raise RuntimeInstallError("duplicate archive member path")
                seen.add(identity)
                if member.isdir():
                    continue
                if not member.isfile():
                    raise RuntimeInstallError("archive link or device is forbidden")
                if parts[-1] == asset.executable:
                    candidates.append(member)
            if len(candidates) != 1:
                raise RuntimeInstallError(
                    "archive must contain exactly one executable entry"
                )
            candidate = candidates[0]
            stream = container.extractfile(candidate)
            if stream is None:
                raise RuntimeInstallError("archive executable is unreadable")
            with stream:
                return _read_bounded(stream, candidate.size)
    except RuntimeInstallError:
        raise
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise RuntimeInstallError("invalid TAR archive") from exc


def _extract_executable(archive: bytes, asset: DufsAsset) -> bytes:
    if asset.filename.casefold().endswith(".zip"):
        executable = _zip_executable(archive, asset)
    elif asset.filename.casefold().endswith(".tar.gz"):
        executable = _tar_executable(archive, asset)
    else:
        raise RuntimeInstallError("unsupported Dufs archive format")
    digest = hashlib.sha256(executable).hexdigest()
    if not hmac.compare_digest(digest, asset.executable_sha256):
        raise RuntimeInstallError("executable SHA-256 mismatch")
    return executable


def _verify_staged_runtime(
    executable: Path,
    asset: DufsAsset,
    runner: Runner | None,
) -> RuntimeInspection:
    digest = file_sha256(executable)
    if not hmac.compare_digest(digest, asset.executable_sha256):
        raise RuntimeInstallError("staged executable SHA-256 mismatch")
    active_runner = runner or SubprocessVersionRunner()
    try:
        result = active_runner.run(
            (str(executable), "--version"),
            timeout=_VERSION_TIMEOUT,
            output_limit=_VERSION_OUTPUT_LIMIT,
        )
    except (OSError, ValueError) as exc:
        raise RuntimeInstallError("unable to verify staged Dufs version") from exc
    version = parse_exact_dufs_version(result)
    if version != DUFS_VERSION:
        raise RuntimeInstallError("staged Dufs version mismatch")
    return RuntimeInspection(
        status="ready",
        executable=str(executable),
        version=version,
        sha256=digest,
        expected_sha256=asset.executable_sha256,
        asset_url=asset.url,
    )


def _remove_owned(path: Path, parent: Path, prefix: str) -> None:
    if path.parent != parent or not path.name.startswith(prefix):
        raise RuntimeInstallError("refusing to remove unowned runtime path")
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
    else:
        shutil.rmtree(path)


def install_runtime(
    hub_root: Path,
    asset: DufsAsset,
    *,
    confirmed_asset_sha256: str,
    transport: httpx.BaseTransport | None = None,
    runner: Runner | None = None,
) -> RuntimeInspection:
    target = runtime_path(hub_root, asset)
    if (
        type(confirmed_asset_sha256) is not str
        or _SHA256_PATTERN.fullmatch(confirmed_asset_sha256) is None
        or not hmac.compare_digest(
            confirmed_asset_sha256, asset.archive_sha256
        )
    ):
        raise RuntimeInstallError("asset confirmation digest does not match")
    _validate_download_url(asset.url)
    archive = _download_archive(asset, transport=transport)
    executable_bytes = _extract_executable(archive, asset)

    target_directory = target.parent
    runtime_root = target_directory.parent
    runtime_root.mkdir(parents=True, exist_ok=True)
    if runtime_root.is_symlink() or target_directory.is_symlink():
        raise RuntimeInstallError("runtime path must not contain a symlink")
    staging_root = Path(
        tempfile.mkdtemp(prefix=".install-", dir=str(runtime_root))
    )
    backup = runtime_root / f".replace-{uuid.uuid4().hex}"
    moved_existing = False
    installed = False
    try:
        staged_directory = staging_root / target_directory.name
        staged_directory.mkdir()
        staged_executable = staged_directory / asset.executable
        with staged_executable.open("xb") as stream:
            stream.write(executable_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            mode = staged_executable.stat().st_mode
            staged_executable.chmod(mode | stat.S_IXUSR)
        inspection = _verify_staged_runtime(staged_executable, asset, runner)

        if target_directory.exists() or target_directory.is_symlink():
            if target_directory.is_symlink():
                raise RuntimeInstallError(
                    "runtime target must not be a symlink"
                )
            os.replace(target_directory, backup)
            moved_existing = True
        try:
            os.replace(staged_directory, target_directory)
            installed = True
        except OSError:
            if moved_existing and backup.exists() and not target_directory.exists():
                os.replace(backup, target_directory)
                moved_existing = False
            raise
        if moved_existing:
            _remove_owned(backup, runtime_root, ".replace-")
            moved_existing = False
        return RuntimeInspection(
            status=inspection.status,
            executable=str(target),
            version=inspection.version,
            sha256=inspection.sha256,
            expected_sha256=inspection.expected_sha256,
            asset_url=inspection.asset_url,
        )
    except RuntimeInstallError:
        raise
    except OSError as exc:
        raise RuntimeInstallError("unable to install Dufs runtime") from exc
    finally:
        if moved_existing and backup.exists() and not target_directory.exists():
            os.replace(backup, target_directory)
        if backup.exists() and installed:
            _remove_owned(backup, runtime_root, ".replace-")
        if staging_root.exists():
            _remove_owned(staging_root, runtime_root, ".install-")


__all__ = (
    "Runner",
    "RunnerResult",
    "RuntimeInspection",
    "RuntimeInstallError",
    "SubprocessVersionRunner",
    "file_sha256",
    "inspect_runtime",
    "installation_candidate",
    "install_runtime",
    "parse_exact_dufs_version",
    "runtime_path",
)
