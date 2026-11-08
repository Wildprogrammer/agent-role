from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "install_xiaozhi_server_adapter.py"
)


def _load_installer():
    spec = importlib.util.spec_from_file_location("test_xiaozhi_installer", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_preflight_rejects_unknown_late_target_before_any_write(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_installer()
    source_root = tmp_path / "sources"
    server_source = tmp_path / "server"
    source_root.mkdir()
    server_source.mkdir()

    first_source = source_root / "first.py"
    second_source = source_root / "second.py"
    first_target = server_source / "first.py"
    second_target = server_source / "second.py"
    first_source.write_bytes(b"first-new")
    second_source.write_bytes(b"second-new")
    first_target.write_bytes(b"first-old")
    second_target.write_bytes(b"second-unknown")
    monkeypatch.setattr(
        module,
        "KNOWN_MANAGED_SHA256",
        {
            "first.py": {_sha256(b"first-old")},
            "second.py": set(),
        },
    )

    with pytest.raises(module.InstallError, match="refusing to overwrite"):
        module._install_artifacts(
            ((first_source, first_target), (second_source, second_target)),
            server_source,
            apply=True,
        )

    assert first_target.read_bytes() == b"first-old"
    assert second_target.read_bytes() == b"second-unknown"


def test_apply_replaces_changed_artifacts_atomically(tmp_path: Path, monkeypatch) -> None:
    module = _load_installer()
    source_root = tmp_path / "sources"
    server_source = tmp_path / "server"
    source_root.mkdir()
    server_source.mkdir()

    source = source_root / "adapter.py"
    target = server_source / "adapter.py"
    source.write_bytes(b"new-adapter")
    target.write_bytes(b"old-adapter")
    monkeypatch.setattr(
        module,
        "KNOWN_MANAGED_SHA256",
        {"adapter.py": {_sha256(b"old-adapter")}},
    )
    replaced: list[tuple[Path, Path]] = []
    real_replace = module.os.replace

    def observe_replace(source_path, target_path):
        replaced.append((Path(source_path), Path(target_path)))
        real_replace(source_path, target_path)

    monkeypatch.setattr(module.os, "replace", observe_replace)

    artifacts, changed = module._install_artifacts(
        ((source, target),), server_source, apply=True
    )

    assert changed is True
    assert artifacts[0]["changed"] is True
    assert target.read_bytes() == b"new-adapter"
    assert len(replaced) == 1
    assert replaced[0][1] == target
    assert not list(server_source.glob("*.tmp"))


def test_apply_rechecks_target_immediately_before_replace(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_installer()
    source_root = tmp_path / "sources"
    server_source = tmp_path / "server"
    source_root.mkdir()
    server_source.mkdir()
    source = source_root / "adapter.py"
    target = server_source / "adapter.py"
    source.write_bytes(b"new-adapter")
    target.write_bytes(b"old-adapter")
    monkeypatch.setattr(
        module,
        "KNOWN_MANAGED_SHA256",
        {"adapter.py": {_sha256(b"old-adapter")}},
    )
    real_validate = module._validate_managed_target
    validations = 0

    def mutate_after_preflight(target_path, root_path):
        nonlocal validations
        real_validate(target_path, root_path)
        validations += 1
        if validations == 2:
            target.write_bytes(b"raced-adapter")

    monkeypatch.setattr(module, "_validate_managed_target", mutate_after_preflight)

    with pytest.raises(module.InstallError, match="changed during install"):
        module._install_artifacts(
            ((source, target),), server_source, apply=True
        )

    assert target.read_bytes() == b"raced-adapter"
