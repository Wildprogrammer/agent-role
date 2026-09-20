from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

DUFS_VERSION = "0.46.0"
RELEASE_URL_PREFIX = (
    "https://github.com/sigoden/dufs/releases/download/v0.46.0/"
)
MAX_ASSET_SIZE = 16 * 1024 * 1024

SYSTEM_ALIASES = {
    "windows": "windows",
    "darwin": "macos",
    "linux": "linux",
}
MACHINE_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "arm": "arm",
    "armv6l": "arm",
    "armv7": "armv7",
    "armv7l": "armv7",
}

_TOP_LEVEL_KEYS = {"schema_version", "version", "assets"}
_ASSET_KEYS = {
    "system",
    "machine",
    "filename",
    "url",
    "size",
    "archive_sha256",
    "executable_sha256",
    "executable",
}
_EXPECTED_ASSETS: Mapping[
    tuple[str, str], tuple[str, str, int, str, str, str]
] = MappingProxyType(
    {
        ("macos", "arm64"): (
            "dufs-v0.46.0-aarch64-apple-darwin.tar.gz",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-aarch64-apple-darwin.tar.gz",
            2462445,
            "2c73fa330447c344111aea9159621af5e2d4460b9227562e09138e5c01d3becf",
            "f8dd2a99048d254bab5e3d70e3384904c5e494cb6c38f0cf0417756556784f30",
            "dufs",
        ),
        ("macos", "x86_64"): (
            "dufs-v0.46.0-x86_64-apple-darwin.tar.gz",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-x86_64-apple-darwin.tar.gz",
            2764841,
            "f494db0de9e95764199781754147c4a341227d4794b1bcf5dac862bd21c1216a",
            "7a10900f1b541d3a2941cb44670e3376a179af87b211b8e652ac054d745090ab",
            "dufs",
        ),
        ("windows", "arm64"): (
            "dufs-v0.46.0-aarch64-pc-windows-msvc.zip",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-aarch64-pc-windows-msvc.zip",
            2331484,
            "530e061b5a26182a7cfebc3d473c397aaef931b07f6eccab9592d5df57ef0c39",
            "1b3f981a423cc26511d0a0a99a025bce5f2e20b4b0a82460600caf10684a8dba",
            "dufs.exe",
        ),
        ("windows", "x86_64"): (
            "dufs-v0.46.0-x86_64-pc-windows-msvc.zip",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-x86_64-pc-windows-msvc.zip",
            2323817,
            "21d829653ac1f178b124ed384e2a57df19b99ae404935cba9a1a5719c199b0bf",
            "3e443496f7811f931232e1f79eeca23d7d7ff1ea0c9dcac67b6586d69b47e11e",
            "dufs.exe",
        ),
        ("linux", "arm64"): (
            "dufs-v0.46.0-aarch64-unknown-linux-musl.tar.gz",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-aarch64-unknown-linux-musl.tar.gz",
            2618631,
            "1472123ae3aa07e49404d16b20305c2dec90c59883ebda9308717f7205e6511b",
            "5753d125dedb51bc4194e491de7d9081d7a1914a62c1fb4390d68ae55620334b",
            "dufs",
        ),
        ("linux", "arm"): (
            "dufs-v0.46.0-arm-unknown-linux-musleabihf.tar.gz",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-arm-unknown-linux-musleabihf.tar.gz",
            2385372,
            "a2c0a922f944952673ab27706b66f272a76760a8480dc6a067993ab002eca122",
            "11141fef0f5d801ed06e2b6db7a6f518df048c7869107769c41e89b6574700cf",
            "dufs",
        ),
        ("linux", "armv7"): (
            "dufs-v0.46.0-armv7-unknown-linux-musleabihf.tar.gz",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-armv7-unknown-linux-musleabihf.tar.gz",
            2311684,
            "079f0b7ebfb50851a4c9f88c9b12e100322a1137eb57356d1e902f369618c9f6",
            "555cd4f791c99dc8c40b8a7819ece313dce00591a9b3243f7eb53f6eaab8a0bf",
            "dufs",
        ),
        ("linux", "x86_64"): (
            "dufs-v0.46.0-x86_64-unknown-linux-musl.tar.gz",
            f"{RELEASE_URL_PREFIX}dufs-v0.46.0-x86_64-unknown-linux-musl.tar.gz",
            2898409,
            "817769f726613194bcff9d0e3e481eaccc86ac11208857614f36a8c02f410977",
            "2c91337a4a0cabd2ce444dc4b5ffda6c0d4180b4b94278d1345dc958bf24ea68",
            "dufs",
        ),
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


class AssetManifestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DufsAsset:
    system: str
    machine: str
    filename: str
    url: str
    size: int
    archive_sha256: str
    executable_sha256: str
    executable: str


@dataclass(frozen=True, slots=True)
class DufsAssetManifest:
    schema_version: int
    version: str
    assets: tuple[DufsAsset, ...]


def _reject_duplicate_object_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssetManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _require_exact_keys(
    value: dict[str, Any], expected: set[str], label: str
) -> None:
    if set(value) != expected:
        raise AssetManifestError(f"{label} must contain exactly {sorted(expected)}")


def _require_string(value: dict[str, Any], field: str) -> str:
    item = value[field]
    if type(item) is not str or not item:
        raise AssetManifestError(f"asset {field} must be a non-empty string")
    return item


def _load_asset(value: Any) -> DufsAsset:
    if type(value) is not dict:
        raise AssetManifestError("each asset must be an object")
    _require_exact_keys(value, _ASSET_KEYS, "asset")

    system = _require_string(value, "system")
    machine = _require_string(value, "machine")
    filename = _require_string(value, "filename")
    url = _require_string(value, "url")
    archive_sha256 = _require_string(value, "archive_sha256")
    executable_sha256 = _require_string(value, "executable_sha256")
    executable = _require_string(value, "executable")
    size = value["size"]

    expected = _EXPECTED_ASSETS.get((system, machine))
    if expected is None:
        raise AssetManifestError("asset platform or architecture is unsupported")
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise AssetManifestError("asset filename must be a plain filename")
    if url != f"{RELEASE_URL_PREFIX}{filename}":
        raise AssetManifestError(
            "asset URL must use the exact release prefix and match its filename"
        )
    if type(size) is not int or not 0 < size <= MAX_ASSET_SIZE:
        raise AssetManifestError("asset size must be a positive bounded integer")
    if _SHA256.fullmatch(archive_sha256) is None:
        raise AssetManifestError(
            "asset archive_sha256 must be exactly 64 lowercase hexadecimal characters"
        )
    if _SHA256.fullmatch(executable_sha256) is None:
        raise AssetManifestError(
            "asset executable_sha256 must be exactly 64 lowercase hexadecimal characters"
        )
    expected_executable = "dufs.exe" if system == "windows" else "dufs"
    if executable != expected_executable:
        raise AssetManifestError(
            f"asset executable must be {expected_executable!r} for {system}"
        )
    return DufsAsset(
        system=system,
        machine=machine,
        filename=filename,
        url=url,
        size=size,
        archive_sha256=archive_sha256,
        executable_sha256=executable_sha256,
        executable=executable,
    )


def load_asset_manifest(path: Path) -> DufsAssetManifest:
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text, object_pairs_hook=_reject_duplicate_object_keys)
    except AssetManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AssetManifestError(f"could not load asset manifest: {exc}") from exc

    if type(raw) is not dict:
        raise AssetManifestError("asset manifest must be an object")
    _require_exact_keys(raw, _TOP_LEVEL_KEYS, "asset manifest")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
        raise AssetManifestError("schema_version must be integer 2")
    if type(raw["version"]) is not str or raw["version"] != DUFS_VERSION:
        raise AssetManifestError(f"version must be exactly {DUFS_VERSION}")
    if type(raw["assets"]) is not list:
        raise AssetManifestError("assets must be a list")

    assets: list[DufsAsset] = []
    platform_keys: set[tuple[str, str]] = set()
    archive_digests: set[str] = set()
    executable_digests: set[str] = set()
    for value in raw["assets"]:
        asset = _load_asset(value)
        platform_key = (asset.system, asset.machine)
        if platform_key in platform_keys:
            raise AssetManifestError(
                f"duplicate asset for {asset.system}/{asset.machine}"
            )
        if asset.archive_sha256 in archive_digests:
            raise AssetManifestError(
                f"duplicate asset digest: {asset.archive_sha256}"
            )
        if asset.executable_sha256 in executable_digests:
            raise AssetManifestError(
                "duplicate executable digest: " f"{asset.executable_sha256}"
            )
        expected = _EXPECTED_ASSETS[platform_key]
        if (
            asset.filename,
            asset.url,
            asset.size,
            asset.archive_sha256,
            asset.executable_sha256,
            asset.executable,
        ) != expected:
            raise AssetManifestError(
                "asset metadata does not match pinned matrix for "
                f"{asset.system}/{asset.machine}"
            )
        platform_keys.add(platform_key)
        archive_digests.add(asset.archive_sha256)
        executable_digests.add(asset.executable_sha256)
        assets.append(asset)

    if platform_keys != _EXPECTED_ASSETS.keys():
        raise AssetManifestError(
            "asset platforms must match the exact supported set"
        )

    return DufsAssetManifest(
        schema_version=2,
        version=DUFS_VERSION,
        assets=tuple(assets),
    )


def select_asset(
    manifest: DufsAssetManifest,
    *,
    system: str,
    machine: str,
) -> DufsAsset:
    if type(system) is not str or type(machine) is not str:
        raise AssetManifestError("unsupported platform or architecture")
    system_key = SYSTEM_ALIASES.get(system.casefold())
    machine_key = MACHINE_ALIASES.get(machine.casefold())
    if system_key is None or machine_key is None:
        raise AssetManifestError("unsupported platform or architecture")
    matches = [
        item
        for item in manifest.assets
        if item.system == system_key and item.machine == machine_key
    ]
    if len(matches) != 1:
        raise AssetManifestError("official Dufs asset is unavailable")
    return matches[0]
