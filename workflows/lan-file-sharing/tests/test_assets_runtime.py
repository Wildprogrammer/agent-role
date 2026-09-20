from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_workflow_hub.catalog import load_repository_catalog
from agent_workflow_hub.lan_file_sharing.assets import (
    AssetManifestError,
    DufsAsset,
    load_asset_manifest,
    select_asset,
)
from agent_workflow_hub.lan_file_sharing.runtime import (
    RunnerResult,
    RuntimeInstallError,
    file_sha256,
    inspect_runtime,
    installation_candidate,
    install_runtime,
    runtime_path,
)

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "capabilities/cli/dufs/assets-v0.46.0.json"
RELEASE_URL_PREFIX = (
    "https://github.com/sigoden/dufs/releases/download/v0.46.0/"
)
PLATFORMS = (
    ("macos", "arm64"),
    ("macos", "x86_64"),
    ("windows", "arm64"),
    ("windows", "x86_64"),
    ("linux", "arm64"),
    ("linux", "arm"),
    ("linux", "armv7"),
    ("linux", "x86_64"),
)
PINNED_FIELDS = (
    "filename",
    "url",
    "size",
    "archive_sha256",
    "executable_sha256",
    "executable",
)
EXECUTABLE_SHA256 = {
    ("macos", "arm64"): "f8dd2a99048d254bab5e3d70e3384904c5e494cb6c38f0cf0417756556784f30",
    ("macos", "x86_64"): "7a10900f1b541d3a2941cb44670e3376a179af87b211b8e652ac054d745090ab",
    ("windows", "arm64"): "1b3f981a423cc26511d0a0a99a025bce5f2e20b4b0a82460600caf10684a8dba",
    ("windows", "x86_64"): "3e443496f7811f931232e1f79eeca23d7d7ff1ea0c9dcac67b6586d69b47e11e",
    ("linux", "arm64"): "5753d125dedb51bc4194e491de7d9081d7a1914a62c1fb4390d68ae55620334b",
    ("linux", "arm"): "11141fef0f5d801ed06e2b6db7a6f518df048c7869107769c41e89b6574700cf",
    ("linux", "armv7"): "555cd4f791c99dc8c40b8a7819ece313dce00591a9b3243f7eb53f6eaab8a0bf",
    ("linux", "x86_64"): "2c91337a4a0cabd2ce444dc4b5ffda6c0d4180b4b94278d1345dc958bf24ea68",
}


def _archive_asset(
    content: bytes,
    *,
    system: str = "windows",
    machine: str = "x86_64",
    filename: str | None = None,
    executable: str | None = None,
    url: str | None = None,
    size: int | None = None,
    archive_sha256: str | None = None,
    executable_sha256: str | None = None,
) -> DufsAsset:
    selected_filename = filename or (
        "dufs-v0.46.0-x86_64-pc-windows-msvc.zip"
        if system == "windows"
        else "dufs-v0.46.0-x86_64-unknown-linux-musl.tar.gz"
    )
    selected_executable = executable or ("dufs.exe" if system == "windows" else "dufs")
    selected_url = url or f"https://github.com/sigoden/dufs/releases/download/v0.46.0/{selected_filename}"
    return DufsAsset(
        system=system,
        machine=machine,
        filename=selected_filename,
        url=selected_url,
        size=len(content) if size is None else size,
        archive_sha256=(
            hashlib.sha256(content).hexdigest()
            if archive_sha256 is None
            else archive_sha256
        ),
        executable_sha256=(
            hashlib.sha256(content).hexdigest()
            if executable_sha256 is None
            else executable_sha256
        ),
        executable=selected_executable,
    )


def _zip_bytes(*entries: tuple[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


def _tar_bytes(*entries: tuple[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()


def _tar_symlink_bytes(name: str, target: str) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo(name)
        info.type = tarfile.SYMTYPE
        info.linkname = target
        archive.addfile(info)
    return output.getvalue()


class _VersionRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[tuple[tuple[str, ...], float, int]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout: float,
        output_limit: int,
    ) -> RunnerResult:
        self.calls.append((argv, timeout, output_limit))
        return RunnerResult(returncode=0, stdout=self.stdout, stderr="")


def _mock_download(
    content: bytes,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    calls: list[str] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(status_code, headers=headers, content=content, request=request)

    return httpx.MockTransport(handler)


def _manifest_data() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _write_manifest(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "assets.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_dufs_capability_is_fixed_and_workspace_shared() -> None:
    capability = load_repository_catalog(ROOT).capabilities["cli.dufs"]
    assert capability.locked_version == "0.46.0"
    assert capability.recommended_version == "0.46.0"
    assert capability.frontmatter["license"] == "MIT OR Apache-2.0"
    assert capability.installation == {
        "policy": "agent-managed",
        "scope": "workspace-shared",
        "methods": ("existing", "official-artifact"),
    }


def test_manifest_contains_all_official_assets_with_unique_hashes() -> None:
    manifest = load_asset_manifest(MANIFEST)
    assert manifest.schema_version == 2
    assert manifest.version == "0.46.0"
    assert len(manifest.assets) == 8
    assert len({item.archive_sha256 for item in manifest.assets}) == 8
    assert len({item.executable_sha256 for item in manifest.assets}) == 8
    assert all(len(item.archive_sha256) == 64 for item in manifest.assets)
    assert all(len(item.executable_sha256) == 64 for item in manifest.assets)
    assert {
        (item.system, item.machine): item.executable_sha256
        for item in manifest.assets
    } == EXECUTABLE_SHA256
    assert all(
        item.url.startswith(
            "https://github.com/sigoden/dufs/releases/download/v0.46.0/"
        )
        for item in manifest.assets
    )


def test_asset_selection_normalizes_windows_x64() -> None:
    manifest = load_asset_manifest(MANIFEST)
    asset = select_asset(manifest, system="Windows", machine="AMD64")
    assert asset.filename == "dufs-v0.46.0-x86_64-pc-windows-msvc.zip"
    assert asset.archive_sha256 == (
        "21d829653ac1f178b124ed384e2a57df19b99ae404935cba9a1a5719c199b0bf"
    )
    assert asset.executable_sha256 == EXECUTABLE_SHA256[("windows", "x86_64")]
    assert asset.executable == "dufs.exe"


def test_installation_candidate_separates_archive_confirmation_from_runtime_hash() -> None:
    candidate = installation_candidate(
        ROOT,
        system="Windows",
        machine="AMD64",
    )

    assert candidate == {
        "system": "windows",
        "machine": "x86_64",
        "version": "0.46.0",
        "filename": "dufs-v0.46.0-x86_64-pc-windows-msvc.zip",
        "url": (
            "https://github.com/sigoden/dufs/releases/download/v0.46.0/"
            "dufs-v0.46.0-x86_64-pc-windows-msvc.zip"
        ),
        "size": 2323817,
        "archive_sha256": (
            "21d829653ac1f178b124ed384e2a57df19b99ae404935cba9a1a5719c199b0bf"
        ),
        "executable_sha256": EXECUTABLE_SHA256[("windows", "x86_64")],
        "destination": str(
            ROOT
            / "workspace"
            / "shared"
            / "runtimes"
            / "dufs"
            / "0.46.0"
            / "windows-x86_64"
            / "dufs.exe"
        ),
        "rollback_path": str(
            ROOT
            / "workspace"
            / "shared"
            / "runtimes"
            / "dufs"
            / "0.46.0"
            / "windows-x86_64"
        ),
    }


def test_manifest_rejects_duplicate_platform_keys(tmp_path: Path) -> None:
    raw = _manifest_data()
    raw["assets"].append(dict(raw["assets"][0]))
    with pytest.raises(AssetManifestError, match="duplicate asset"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    "remaining_assets",
    [
        pytest.param(0, id="empty"),
        pytest.param(7, id="missing-one"),
    ],
)
def test_manifest_rejects_incomplete_supported_platform_set(
    tmp_path: Path, remaining_assets: int
) -> None:
    raw = _manifest_data()
    raw["assets"] = raw["assets"][:remaining_assets]

    with pytest.raises(AssetManifestError):
        load_asset_manifest(_write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    ("asset_index", "field"),
    [
        pytest.param(index, field, id=f"{system}-{machine}-{field}")
        for index, (system, machine) in enumerate(PLATFORMS)
        for field in PINNED_FIELDS
    ],
)
def test_manifest_rejects_any_pinned_matrix_value_mismatch(
    tmp_path: Path, asset_index: int, field: str
) -> None:
    raw = _manifest_data()
    asset = raw["assets"][asset_index]

    if field == "filename":
        asset["filename"] = f"unexpected-{asset['filename']}"
        asset["url"] = f"{RELEASE_URL_PREFIX}{asset['filename']}"
    elif field == "url":
        asset["url"] = f"{RELEASE_URL_PREFIX}unexpected-{asset['filename']}"
    elif field == "size":
        asset["size"] += 1
    elif field in {"archive_sha256", "executable_sha256"}:
        first = "0" if asset[field][0] != "0" else "1"
        asset[field] = first + asset[field][1:]
    elif field == "executable":
        asset["executable"] = (
            "dufs" if asset["system"] == "windows" else "dufs.exe"
        )
    else:  # pragma: no cover - the parameter table is fixed above
        raise AssertionError(f"unhandled field: {field}")

    with pytest.raises(AssetManifestError):
        load_asset_manifest(_write_manifest(tmp_path, raw))


def test_manifest_rejects_swapped_platform_labels(tmp_path: Path) -> None:
    raw = _manifest_data()
    first = raw["assets"][0]
    second = raw["assets"][-1]
    first_key = (first["system"], first["machine"])
    second_key = (second["system"], second["machine"])
    first["system"], first["machine"] = second_key
    second["system"], second["machine"] = first_key

    with pytest.raises(AssetManifestError):
        load_asset_manifest(_write_manifest(tmp_path, raw))


@pytest.mark.parametrize("location", ["top-level", "asset"])
def test_manifest_rejects_unknown_keys(tmp_path: Path, location: str) -> None:
    raw = _manifest_data()
    target = raw if location == "top-level" else raw["assets"][0]
    target["unknown"] = "value"

    with pytest.raises(AssetManifestError, match="exactly"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


def test_manifest_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    text = MANIFEST.read_text(encoding="utf-8").replace(
        '  "version": "0.46.0",',
        '  "version": "0.46.0",\n  "version": "0.46.0",',
        1,
    )
    path = tmp_path / "assets.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(AssetManifestError, match="duplicate JSON key"):
        load_asset_manifest(path)


def test_manifest_rejects_bool_as_size(tmp_path: Path) -> None:
    raw = _manifest_data()
    raw["assets"][0]["size"] = True

    with pytest.raises(AssetManifestError, match="positive bounded integer"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


def test_manifest_rejects_url_filename_mismatch(tmp_path: Path) -> None:
    raw = _manifest_data()
    raw["assets"][0]["url"] = f"{RELEASE_URL_PREFIX}different.tar.gz"

    with pytest.raises(AssetManifestError, match="match its filename"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


@pytest.mark.parametrize(
    "field",
    ["archive_sha256", "executable_sha256"],
)
@pytest.mark.parametrize(
    "sha256",
    [
        pytest.param("A" * 64, id="uppercase"),
        pytest.param("not-a-sha256", id="malformed"),
    ],
)
def test_manifest_rejects_noncanonical_sha256(
    tmp_path: Path, field: str, sha256: str
) -> None:
    raw = _manifest_data()
    raw["assets"][0][field] = sha256

    with pytest.raises(AssetManifestError, match="lowercase hexadecimal"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


def test_manifest_rejects_wrong_executable(tmp_path: Path) -> None:
    raw = _manifest_data()
    raw["assets"][0]["executable"] = "dufs.exe"

    with pytest.raises(AssetManifestError, match="asset executable"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


def test_manifest_rejects_duplicate_digest(tmp_path: Path) -> None:
    raw = _manifest_data()
    raw["assets"][1]["archive_sha256"] = raw["assets"][0]["archive_sha256"]

    with pytest.raises(AssetManifestError, match="duplicate asset digest"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


def test_manifest_rejects_duplicate_executable_digest(tmp_path: Path) -> None:
    raw = _manifest_data()
    raw["assets"][1]["executable_sha256"] = raw["assets"][0][
        "executable_sha256"
    ]

    with pytest.raises(AssetManifestError, match="duplicate executable digest"):
        load_asset_manifest(_write_manifest(tmp_path, raw))


def test_select_asset_rejects_unsupported_platform() -> None:
    manifest = load_asset_manifest(MANIFEST)

    with pytest.raises(AssetManifestError, match="unsupported platform"):
        select_asset(manifest, system="FreeBSD", machine="AMD64")


def test_runtime_path_is_workspace_shared(tmp_path: Path) -> None:
    asset = _archive_asset(b"archive")
    expected = (
        tmp_path
        / "workspace/shared/runtimes/dufs/0.46.0/windows-x86_64/dufs.exe"
    )
    assert runtime_path(tmp_path, asset) == expected


def test_inspect_runtime_reports_absent_runtime_without_running_anything(
    tmp_path: Path,
) -> None:
    asset = _archive_asset(b"archive")
    runner = _VersionRunner("dufs 0.46.0")

    inspection = inspect_runtime(tmp_path, asset, runner=runner)

    assert inspection.status == "needs_dependency"
    assert inspection.version is None
    assert runner.calls == []


def test_inspect_runtime_rejects_wrong_installed_hash(tmp_path: Path) -> None:
    executable = b"right executable"
    asset = _archive_asset(
        b"archive", executable_sha256=hashlib.sha256(executable).hexdigest()
    )
    path = runtime_path(tmp_path, asset)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"wrong executable")
    runner = _VersionRunner("dufs 0.46.0")

    inspection = inspect_runtime(tmp_path, asset, runner=runner)

    assert inspection.status == "not_ready"
    assert inspection.sha256 == file_sha256(path)
    assert runner.calls == []


def test_inspect_runtime_requires_exact_dufs_version(tmp_path: Path) -> None:
    executable = b"right executable"
    asset = _archive_asset(
        b"archive", executable_sha256=hashlib.sha256(executable).hexdigest()
    )
    path = runtime_path(tmp_path, asset)
    path.parent.mkdir(parents=True)
    path.write_bytes(executable)
    runner = _VersionRunner("dufs 0.45.0")

    inspection = inspect_runtime(tmp_path, asset, runner=runner)

    assert inspection.status == "not_ready"
    assert inspection.version == "0.45.0"
    assert runner.calls == [((str(path), "--version"), 5, 65_536)]


def test_inspect_runtime_accepts_exact_version_and_hash(tmp_path: Path) -> None:
    executable = b"right executable"
    asset = _archive_asset(
        b"archive", executable_sha256=hashlib.sha256(executable).hexdigest()
    )
    path = runtime_path(tmp_path, asset)
    path.parent.mkdir(parents=True)
    path.write_bytes(executable)
    runner = _VersionRunner("dufs 0.46.0\n")

    inspection = inspect_runtime(tmp_path, asset, runner=runner)

    assert inspection.status == "ready"
    assert inspection.version == "0.46.0"


def test_install_requires_matching_confirmation_before_http(tmp_path: Path) -> None:
    calls: list[str] = []
    asset = _archive_asset(b"archive")
    transport = _mock_download(b"archive", calls=calls)

    with pytest.raises(RuntimeInstallError, match="confirmation"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256="0" * 64,
            transport=transport,
        )

    assert calls == []


def test_install_rejects_download_size_overflow(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(content, size=len(content) - 1)

    with pytest.raises(RuntimeInstallError, match="declared size"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content),
        )


def test_install_rejects_download_hash_mismatch(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(content, archive_sha256="f" * 64)

    with pytest.raises(RuntimeInstallError, match="SHA-256"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content),
        )


@pytest.mark.parametrize(
    "entries",
    [
        pytest.param(("../dufs.exe", b"executable"), id="dot-dot"),
        pytest.param(("C:/dufs.exe", b"executable"), id="drive-path"),
        pytest.param(("/dufs.exe", b"executable"), id="absolute"),
    ],
)
def test_install_rejects_unsafe_zip_member_paths(
    tmp_path: Path, entries: tuple[str, bytes]
) -> None:
    content = _zip_bytes(entries)
    asset = _archive_asset(content)

    with pytest.raises(RuntimeInstallError, match="archive member"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content),
        )


def test_install_rejects_tar_symlink(tmp_path: Path) -> None:
    content = _tar_symlink_bytes("dufs", "../../outside")
    asset = _archive_asset(
        content,
        system="linux",
        filename="dufs-v0.46.0-x86_64-unknown-linux-musl.tar.gz",
        executable="dufs",
    )

    with pytest.raises(RuntimeInstallError, match="link"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content),
        )


def test_install_rejects_duplicate_archive_paths(tmp_path: Path) -> None:
    output = io.BytesIO()
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("dufs.exe", b"first")
            archive.writestr("dufs.exe", b"second")
    content = output.getvalue()
    asset = _archive_asset(content)

    with pytest.raises(RuntimeInstallError, match="duplicate"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content),
        )


def test_install_rejects_multiple_executable_entries(tmp_path: Path) -> None:
    content = _zip_bytes(
        ("one/dufs.exe", b"first"),
        ("two/dufs.exe", b"second"),
    )
    asset = _archive_asset(content)

    with pytest.raises(RuntimeInstallError, match="executable"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content),
        )


def test_install_accepts_safe_archive_and_atomically_replaces_runtime(
    tmp_path: Path,
) -> None:
    executable = b"dufs executable"
    content = _zip_bytes(("README.txt", b"ignored"), ("release/dufs.exe", executable))
    asset = _archive_asset(
        content,
        executable_sha256=hashlib.sha256(executable).hexdigest(),
    )
    existing = runtime_path(tmp_path, asset)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")
    runner = _VersionRunner("dufs 0.46.0")

    inspection = install_runtime(
        tmp_path,
        asset,
        confirmed_asset_sha256=asset.archive_sha256,
        transport=_mock_download(content),
        runner=runner,
    )

    assert inspection.status == "ready"
    assert existing.read_bytes() == executable
    assert not list(existing.parent.glob(".install-*"))


def test_install_accepts_safe_zip_directory_entries(tmp_path: Path) -> None:
    executable = b"dufs executable"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("release/", b"")
        archive.writestr("release/dufs.exe", executable)
    content = output.getvalue()
    asset = _archive_asset(
        content,
        executable_sha256=hashlib.sha256(executable).hexdigest(),
    )

    inspection = install_runtime(
        tmp_path,
        asset,
        confirmed_asset_sha256=asset.archive_sha256,
        transport=_mock_download(content),
        runner=_VersionRunner("dufs 0.46.0"),
    )

    assert inspection.status == "ready"


def test_install_accepts_safe_tar_directory_entries(tmp_path: Path) -> None:
    executable = b"dufs executable"
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        directory = tarfile.TarInfo("release/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        member = tarfile.TarInfo("release/dufs")
        member.size = len(executable)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(executable))
    content = output.getvalue()
    asset = _archive_asset(
        content,
        system="linux",
        filename="dufs-v0.46.0-x86_64-unknown-linux-musl.tar.gz",
        executable="dufs",
        executable_sha256=hashlib.sha256(executable).hexdigest(),
    )

    inspection = install_runtime(
        tmp_path,
        asset,
        confirmed_asset_sha256=asset.archive_sha256,
        transport=_mock_download(content),
        runner=_VersionRunner("dufs 0.46.0"),
    )

    assert inspection.status == "ready"


def test_install_sets_user_execute_bits_on_posix(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX execute bits do not apply on Windows")
    executable = b"dufs executable"
    content = _tar_bytes(("dufs", executable))
    asset = _archive_asset(
        content,
        system="linux",
        filename="dufs-v0.46.0-x86_64-unknown-linux-musl.tar.gz",
        executable="dufs",
        executable_sha256=hashlib.sha256(executable).hexdigest(),
    )
    runner = _VersionRunner("dufs 0.46.0")

    install_runtime(
        tmp_path,
        asset,
        confirmed_asset_sha256=asset.archive_sha256,
        transport=_mock_download(content),
        runner=runner,
    )
    mode = runtime_path(tmp_path, asset).stat().st_mode
    assert mode & stat.S_IXUSR


def test_install_follows_approved_githubusercontent_redirect(tmp_path: Path) -> None:
    executable = b"dufs executable"
    content = _zip_bytes(("dufs.exe", executable))
    asset = _archive_asset(
        content,
        executable_sha256=hashlib.sha256(executable).hexdigest(),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={
                    "Location": "https://release-assets.githubusercontent.com/dufs.zip"
                },
                request=request,
            )
        return httpx.Response(200, content=content, request=request)

    inspection = install_runtime(
        tmp_path,
        asset,
        confirmed_asset_sha256=asset.archive_sha256,
        transport=httpx.MockTransport(handler),
        runner=_VersionRunner("dufs 0.46.0"),
    )

    assert inspection.status == "ready"
    assert len(calls) == 2
    assert calls[1].startswith("https://release-assets.githubusercontent.com/")


@pytest.mark.parametrize(
    "location",
    [
        pytest.param("http://github.com/dufs.zip", id="http-downgrade"),
        pytest.param("https://user:pass@github.com/dufs.zip", id="credentials"),
        pytest.param("https://github.com.attacker.example/dufs.zip", id="deceptive-github"),
        pytest.param("https://githubusercontent.com/dufs.zip", id="bare-githubusercontent"),
        pytest.param("https://assets.githubusercontent.com.evil/dufs.zip", id="deceptive-content"),
        pytest.param("https://example.com/dufs.zip", id="outside-allowlist"),
    ],
)
def test_install_rejects_unapproved_redirect_before_following(
    tmp_path: Path, location: str
) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(content)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": location}, request=request)

    with pytest.raises(RuntimeInstallError, match="URL|redirect|HTTPS|host|credentials"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=httpx.MockTransport(handler),
        )

    assert len(calls) == 1


def test_install_rejects_initial_url_credentials_before_http(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(
        content,
        url="https://user:pass@github.com/sigoden/dufs/releases/download/v0.46.0/dufs.zip",
    )
    calls: list[str] = []

    with pytest.raises(RuntimeInstallError, match="credentials"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content, calls=calls),
        )

    assert calls == []


def test_install_rejects_initial_unapproved_host_before_http(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(content, url="https://example.com/dufs.zip")
    calls: list[str] = []

    with pytest.raises(RuntimeInstallError, match="host"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content, calls=calls),
        )

    assert calls == []


def test_install_rejects_redirect_without_location(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(content)

    with pytest.raises(RuntimeInstallError, match="Location"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(b"", status_code=302),
        )


def test_install_rejects_redirect_loop(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(content)

    with pytest.raises(RuntimeInstallError, match="loop"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(
                b"", status_code=302, headers={"Location": asset.url}
            ),
        )


def test_install_rejects_sixth_redirect(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"executable"))
    asset = _archive_asset(content, url="https://github.com/redirect/0")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        index = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            302,
            headers={"Location": f"https://github.com/redirect/{index + 1}"},
            request=request,
        )

    with pytest.raises(RuntimeInstallError, match="redirect limit"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=httpx.MockTransport(handler),
        )

    assert len(calls) == 6


def test_install_cleans_owned_staging_after_failure(tmp_path: Path) -> None:
    content = _zip_bytes(("dufs.exe", b"wrong executable"))
    asset = _archive_asset(content, executable_sha256="a" * 64)
    final_path = runtime_path(tmp_path, asset)

    with pytest.raises(RuntimeInstallError, match="executable SHA-256"):
        install_runtime(
            tmp_path,
            asset,
            confirmed_asset_sha256=asset.archive_sha256,
            transport=_mock_download(content),
            runner=_VersionRunner("dufs 0.46.0"),
        )

    assert not final_path.exists()
    assert not list(final_path.parent.parent.glob(".install-*"))
