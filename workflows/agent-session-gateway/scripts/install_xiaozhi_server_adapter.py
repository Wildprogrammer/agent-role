#!/usr/bin/env python3
"""Install the pinned Xiaozhi server adapters without touching other files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import tempfile
from pathlib import Path


PINNED_SERVER_COMMIT = "87c6df77b0220ddbd8fc804f984f83f69f3225e5"
KNOWN_MANAGED_SHA256 = {
    "plugins_func/functions/hermes_bridge.py": {
        "5f09c9887bd3cdc953b765fcff876f6d1baf4743134aeb741abe93e878bbe201",
        "ef65c802ddf35a460d3308ad9451f3b3f1df5c2ed3f3490ab2ed466e6b5a8ed1",
        "d0e951e6b078ebaf47fa68259ed020351214334a16782206a7ab2536d172c551",
        "a332f1d3c35875acffad9b4dbcca5a28932b8a57ad109d5030c1537318c92d00",
        "ba9b51a187f706ba8ca093ccb63d38f8584feece64a0150ba28a451451333d94",
        "e5e9d5d6ee96103a8d76cca43e699a88d75361532775c4a8dc0884bd7f4744b4",
        "72881e9bd976cf119d9fe9e3827417b1c8d3571d4ba98b37ae221e0c9e3c562c",
        "72a06234912bf7a92dac573d1f80196ca59eeff5f16131e0235417d219053103",
        "be7e831efef413bf3c85c9e4a8fc4842bcc5709da09c6c7561e9c9bf8b145382",
        "524d6e222543b20364fb2c70d01fc086854b4ba5542b9655f4ccfe9fa6ca5858",
        "7ce83826bdf7a0907142d6cea4b9088f22342353532b890df0410de353a92f48",
        "39ca6433f7b3231b11a0ec965cb3fdcb88b2f77f741e592aeb8a70321c8684a0",
        "999573e4b56e31f747974ff3e285d34ec0d867fede2a55a7d2b4659b83245379",
        "3b5ab5c56a3512fd597263f05a0288f9965b6aef9d5cbe33449c44f527d332e0",
        "8fedfb503d8b5740fc00ac19b9f61daaef22896d73f132bdc2eca83ec06a41f3",
        "8995dafa157f958369f5cc3d0d841b264b3ae440f177378be3049e1aab28085e",
        "a81a7e37648b2e6ebea1e99e45ec03e7956941aac918dd75926ef4e1f0a6bcdb",
        "e45018782208e822d3397699136c87f6651140f1ca3a22495d4998610b4f2a48",
        "285a7582a6f9772eef6dd71d3fa963681163dce8e21e05b1939d86ff030fb23e",
        "9eee28b8a926befffbe31032d8f2fa3a3c5ff1853356a030f96ef7398137ef7d",
        "0edc55da5ce3189ec6835693a2ef63f789a9e161f2ede705e9449abc628a63d1",
        "7b735d295c660c82c513b3da2a82bd419b591d127f2c1064d5d6910c25c645bf",
        "461ba3800f1fb6d530073e3e7aa857a453be618c413c812c2fd2ae75cd59d407",
        "3bc08c5cd983494811ad0e87331516a2eb8d9610492060141aba486dfc9691a9",
        "43076f9680218439babe493a30f13840bc7eb74e5e5b08d4fb2d5f5e33472e93",
        "f4970d1f437dcd26e748bf9d08dbb315d9df37fac84f9a0e599b4bd3ba5f20a1",
    },
    "core/providers/llm/ollama_nonstream_tools/ollama_nonstream_tools.py": {
        "97dd1c24e021a9ab6cd3e4b0ff2225b6e9c93e4b0bcb6ae49b522e044ee37086",
        "af630fdb26ba2c01953845da2e00eab05acc02167d1cdd3aa1172ece15306591",
        "70e2b787f1b391c4fcf0f114195cadebbdf9e07846dfc3aa261a263e29178abd",
        "f40fa8995aed43077dcb6402e6f673936d18ae5471d5e994a5b857509f58e596",
        "e16cc3c19bc3c5df1a11b6d789b7363a7591582d0e3af1d0bf710ce9c61891fe",
        "ab764d8e00f62dc6d4f524f0a548de09925809642c9ee3bd4ce7b70d5861541f",
        "03235aef6bc4496e1b6aa72a39f0a9fa3b0bbe08a262e3505d7c8ed62e5e723b",
        "06ea5a124212883eb451d1d2c1f8198fe0bb95382848b29ea47c9f62ba084351",
        "452baddcecb61eaa8c18322cdd2f4434ec186d8cc6da754734ab1821ea2b60c6",
        "be5570a6f6e253d804cd57fcfa54c8001a5d05bc05a048f7cd68644b03ae6d22",
        "2fa3e72804934c7df828ccee18e121831474ddb142765c33dc720caeeff59241",
        "2ad354f3adfc92c0abc43b7d18d7f28bec0fb91c2e7f0eb2e4bd6910f83a8795",
        "5edc09b9b76281c91201d2c5db769a71a256a74b2f7d9ce018ebd122268993c5",
        "f1d5139af0ecc1c359fe1465531a590be3e4c3eb77024400c01b7697cad9ac08",
        "03f5a4297ba3886acdbe8b3736d83123f4fc131e057db26ac3c2a9e3bb4e5295",
        "f6ef844615b29c7d8a162e081d2a736c676d222007fc06ca445bad6ad14e8b36",
    },
}


class InstallError(RuntimeError):
    pass


def _git(server_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(server_root), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise InstallError("cannot inspect Xiaozhi server Git checkout")
    return completed.stdout.strip()


def _atomic_replace(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validate_managed_target(target: Path, server_source: Path) -> None:
    """Reject escapes and Windows reparse points in managed target ancestry."""

    root = server_source.resolve()
    lexical_target = Path(os.path.abspath(target))
    try:
        relative = lexical_target.relative_to(root)
    except ValueError:
        raise InstallError("managed adapter target escapes the pinned server root") from None
    try:
        target.resolve(strict=False).relative_to(root)
    except ValueError:
        raise InstallError("managed adapter target resolves outside the pinned server root") from None
    current = root
    for part in relative.parts:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        metadata = os.lstat(current)
        attributes = getattr(metadata, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if current.is_symlink() or attributes & reparse_flag:
            raise InstallError("managed adapter target uses a reparse point")


def _install_artifacts(
    installs: tuple[tuple[Path, Path], ...],
    server_source: Path,
    *,
    apply: bool,
) -> tuple[list[dict[str, object]], bool]:
    """Validate every artifact before replacing any managed target."""

    plans: list[tuple[Path, bytes, bytes | None, dict[str, object]]] = []
    changed = False
    for source, target in installs:
        _validate_managed_target(target, server_source)
        if not source.is_file():
            raise InstallError("pinned server adapter directory is missing")
        if target.parent.exists() and not target.parent.is_dir():
            raise InstallError("pinned server adapter directory is missing")
        if not apply and not target.parent.is_dir():
            raise InstallError("pinned server adapter directory is missing")

        source_bytes = source.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        target_bytes = target.read_bytes() if target.exists() else None
        artifact_changed = target_bytes != source_bytes
        if target_bytes is not None and artifact_changed:
            relative_target = target.relative_to(server_source).as_posix()
            target_sha256 = hashlib.sha256(target_bytes).hexdigest()
            if target_sha256 not in KNOWN_MANAGED_SHA256.get(relative_target, set()):
                raise InstallError(
                    f"existing adapter differs; refusing to overwrite it: {target.name}"
                )

        artifact = {
            "source_sha256": source_sha256,
            "target": str(target),
            "changed": artifact_changed,
        }
        plans.append((target, source_bytes, target_bytes, artifact))
        changed = changed or artifact_changed

    if apply:
        for target, source_bytes, expected_target_bytes, artifact in plans:
            if artifact["changed"]:
                _validate_managed_target(target, server_source)
                current_target_bytes = target.read_bytes() if target.exists() else None
                if current_target_bytes != expected_target_bytes:
                    raise InstallError(
                        f"existing adapter changed during install: {target.name}"
                    )
                _atomic_replace(target, source_bytes)

    return [artifact for _, _, _, artifact in plans], changed


def install(server_root: Path, *, apply: bool) -> dict[str, object]:
    root = server_root.resolve()
    if not root.is_absolute() or not (root / ".git").is_dir():
        raise InstallError("server root must be an absolute Git checkout")
    head = _git(root, "rev-parse", "HEAD")
    if head != PINNED_SERVER_COMMIT:
        raise InstallError(
            f"Xiaozhi server HEAD must be pinned to {PINNED_SERVER_COMMIT}"
        )
    adapter_root = Path(__file__).resolve().parents[1] / "adapters"
    server_source = (root / "main" / "xiaozhi-server").resolve()
    try:
        server_source.relative_to(root)
    except ValueError:
        raise InstallError("pinned server source escapes the Git checkout") from None
    installs = (
        (
            adapter_root / "xiaozhi_server_plugin.py",
            server_source / "plugins_func" / "functions" / "hermes_bridge.py",
        ),
        (
            adapter_root / "software_test_silence_tts.py",
            server_source / "core" / "providers" / "tts" / "software_test_silence.py",
        ),
        (
            adapter_root / "windows_sapi_tts.py",
            server_source / "core" / "providers" / "tts" / "windows_sapi.py",
        ),
        (
            adapter_root / "ollama_nonstream_tools_llm.py",
            server_source
            / "core"
            / "providers"
            / "llm"
            / "ollama_nonstream_tools"
            / "ollama_nonstream_tools.py",
        ),
    )
    artifacts, changed = _install_artifacts(installs, server_source, apply=apply)
    return {
        "status": "installed" if apply and changed else "unchanged" if not changed else "planned",
        "server_commit": head,
        "artifacts": artifacts,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-root", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = install(args.server_root, apply=args.apply)
    except InstallError as exc:
        print(json.dumps({"status": "rejected", "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
