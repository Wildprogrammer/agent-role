from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_workflow_hub.specialized_agent_deployment.filesystem import (
    MANAGED_MARKER,
    ManagedWrite,
    ManagedTargetError,
    TransactionApplyError,
    TransactionOutcomeUnknown,
    apply_managed_transaction,
    reconcile_uncertain_write,
    validate_managed_target,
)


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def marker() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "deployment_id": "fixture-deployment",
        "agent_id": "fixture-agent",
        "host": "hermes",
    }


def managed_target(tmp_path: Path) -> Path:
    target = (tmp_path / "profile").resolve()
    target.mkdir()
    (target / MANAGED_MARKER).write_text(
        json.dumps(marker(), sort_keys=True), encoding="utf-8"
    )
    return target


def test_create_rejects_existing_target(tmp_path: Path) -> None:
    target = (tmp_path / "existing").resolve()
    target.mkdir()
    with pytest.raises(ManagedTargetError):
        validate_managed_target(target, mode="create", expected_marker=marker())


def test_update_requires_matching_managed_marker(tmp_path: Path) -> None:
    target = managed_target(tmp_path)
    validate_managed_target(
        target,
        mode="update",
        expected_marker=MappingProxyType(marker()),
    )

    wrong = marker() | {"agent_id": "other-agent"}
    with pytest.raises(ManagedTargetError):
        validate_managed_target(target, mode="update", expected_marker=wrong)

    (target / MANAGED_MARKER).unlink()
    with pytest.raises(ManagedTargetError):
        validate_managed_target(target, mode="update", expected_marker=marker())


@pytest.mark.parametrize(
    "target",
    [Path("relative"), Path.cwd().resolve(), Path.home().resolve()],
)
def test_target_rejects_relative_or_broad_roots(target: Path) -> None:
    with pytest.raises(ManagedTargetError):
        validate_managed_target(target, mode="create", expected_marker=marker())


def test_target_rejects_symlink_or_reparse_parent(tmp_path: Path) -> None:
    real = (tmp_path / "real").resolve()
    real.mkdir()
    linked = (tmp_path / "linked").resolve()
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")
    with pytest.raises(ManagedTargetError):
        validate_managed_target(
            linked / "profile",
            mode="create",
            expected_marker=marker(),
        )


def test_update_failure_restores_only_managed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    target = managed_target(tmp_path)
    agents = target / "AGENTS.md"
    config = target / "managed.json"
    unknown = target / "user-notes.md"
    agents.write_bytes(b"old-agents")
    config.write_bytes(b"old-config")
    unknown.write_bytes(b"keep")
    writes = (
        ManagedWrite(
            target=agents,
            content=b"new-agents",
            expected_before_sha256=digest(b"old-agents"),
        ),
        ManagedWrite(
            target=config,
            content=b"new-config",
            expected_before_sha256=digest(b"old-config"),
        ),
    )
    real_replace = filesystem._atomic_replace
    calls = 0

    def fail_second(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture failure")
        real_replace(path, content)

    monkeypatch.setattr(filesystem, "_atomic_replace", fail_second)
    with pytest.raises(TransactionApplyError):
        apply_managed_transaction(
            writes,
            backup_root=(tmp_path / "backups").resolve(),
        )
    assert unknown.read_bytes() == b"keep"
    assert agents.read_bytes() == b"old-agents"
    assert config.read_bytes() == b"old-config"


def test_create_failure_removes_only_current_transaction_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    target = (tmp_path / "profile").resolve()
    target.mkdir()
    unknown = target / "user-notes.md"
    unknown.write_bytes(b"keep")
    first = target / "AGENTS.md"
    second = target / "managed.json"
    writes = (
        ManagedWrite(target=first, content=b"new", expected_before_sha256=None),
        ManagedWrite(target=second, content=b"new", expected_before_sha256=None),
    )
    real_replace = filesystem._atomic_replace
    calls = 0

    def fail_second(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture failure")
        real_replace(path, content)

    monkeypatch.setattr(filesystem, "_atomic_replace", fail_second)
    with pytest.raises(TransactionApplyError):
        apply_managed_transaction(
            writes,
            backup_root=(tmp_path / "backups").resolve(),
        )
    assert unknown.read_bytes() == b"keep"
    assert not first.exists()
    assert not second.exists()


def test_identity_change_during_failure_returns_outcome_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    target = (tmp_path / "profile").resolve()
    target.mkdir()
    first = target / "AGENTS.md"
    second = target / "managed.json"
    writes = (
        ManagedWrite(target=first, content=b"ours", expected_before_sha256=None),
        ManagedWrite(target=second, content=b"next", expected_before_sha256=None),
    )
    real_replace = filesystem._atomic_replace
    calls = 0

    def change_then_fail(path: Path, content: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            real_replace(path, content)
            path.write_bytes(b"changed-by-someone-else")
            return
        raise OSError("fixture failure")

    monkeypatch.setattr(filesystem, "_atomic_replace", change_then_fail)
    with pytest.raises(TransactionOutcomeUnknown):
        apply_managed_transaction(
            writes,
            backup_root=(tmp_path / "backups").resolve(),
        )
    assert first.read_bytes() == b"changed-by-someone-else"


def test_replace_that_applies_then_raises_is_reconciled_and_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    target = (tmp_path / "profile").resolve()
    target.mkdir()
    managed = target / "AGENTS.md"
    write = ManagedWrite(
        target=managed,
        content=b"new",
        expected_before_sha256=None,
    )
    real_replace = filesystem._atomic_replace

    def apply_then_raise(path: Path, content: bytes) -> None:
        real_replace(path, content)
        raise OSError("result was not returned")

    monkeypatch.setattr(filesystem, "_atomic_replace", apply_then_raise)
    with pytest.raises(TransactionApplyError):
        apply_managed_transaction(
            (write,),
            backup_root=(tmp_path / "backups").resolve(),
        )
    assert not managed.exists()


def test_atomic_replace_flushes_and_uses_os_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    calls = {"fsync": 0, "replace": 0}
    real_fsync = os.fsync
    real_replace = os.replace

    def tracked_fsync(fd: int) -> None:
        calls["fsync"] += 1
        real_fsync(fd)

    def tracked_replace(source, destination) -> None:
        calls["replace"] += 1
        real_replace(source, destination)

    monkeypatch.setattr(filesystem.os, "fsync", tracked_fsync)
    monkeypatch.setattr(filesystem.os, "replace", tracked_replace)
    target = (tmp_path / "target.txt").resolve()
    filesystem._atomic_replace(target, b"content")
    assert target.read_bytes() == b"content"
    assert calls["fsync"] >= 1
    assert calls["replace"] == 1


def test_atomic_replace_uses_a_short_same_directory_temporary_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    real_mkstemp = filesystem.tempfile.mkstemp

    def guarded_mkstemp(*, prefix: str, suffix: str, dir: Path):
        assert prefix == ".awh-"
        assert suffix == ".tmp"
        return real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    monkeypatch.setattr(filesystem.tempfile, "mkstemp", guarded_mkstemp)
    target = (tmp_path / ("long-target-name-" * 8 + ".txt")).resolve()

    filesystem._atomic_replace(target, b"content")

    assert target.read_bytes() == b"content"


def test_reconcile_uncertain_write_uses_exact_digest(tmp_path: Path) -> None:
    target = (tmp_path / "target.txt").resolve()
    expected = digest(b"expected")
    assert reconcile_uncertain_write(target, expected) == "not_applied"
    target.write_bytes(b"different")
    assert reconcile_uncertain_write(target, expected) == "outcome_unknown"
    target.write_bytes(b"expected")
    assert reconcile_uncertain_write(target, expected) == "applied"


def test_module_does_not_export_general_delete_or_clean() -> None:
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    assert not hasattr(filesystem, "delete_deployment")
    assert not hasattr(filesystem, "clean")


@pytest.mark.skipif(os.name != "nt", reason="Windows long-path regression")
@pytest.mark.parametrize("fail_after_write", [False, True])
def test_long_backup_path_supports_update_and_rollback(tmp_path, monkeypatch, fail_after_write):
    import agent_workflow_hub.specialized_agent_deployment.filesystem as filesystem

    target = tmp_path / "managed.txt"
    target.write_bytes(b"before")
    backup_root = tmp_path / "backups"
    while len(str(backup_root)) < 210:
        backup_root /= "nested-backups"
    assert len(str(backup_root / ("f" * 64 + ".bak"))) > 260
    original_replace = filesystem._atomic_replace
    raised = False

    def replace_then_fail(path, content):
        nonlocal raised
        original_replace(path, content)
        if fail_after_write and not raised:
            raised = True
            raise OSError("injected post-write failure")

    monkeypatch.setattr(filesystem, "_atomic_replace", replace_then_fail)
    writes = (ManagedWrite(target=target, content=b"after", expected_before_sha256=digest(b"before")),)
    if fail_after_write:
        with pytest.raises(TransactionApplyError, match="managed transaction failed"):
            apply_managed_transaction(writes, backup_root=backup_root)
        assert raised  # failure must occur after backup, not during backup creation
        assert target.read_bytes() == b"before"
    else:
        apply_managed_transaction(writes, backup_root=backup_root)
        assert target.read_bytes() == b"after"
    assert filesystem._backup_path(backup_root, target).read_bytes() == b"before"
