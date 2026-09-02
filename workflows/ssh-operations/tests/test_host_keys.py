from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from agent_workflow_hub.ssh_operations.host_keys import (
    HostKeyMismatch,
    KnownHostStore,
)

KEY_A = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
KEY_B = b"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


def test_first_key_is_persisted_and_same_key_is_accepted(tmp_path: Path) -> None:
    store = KnownHostStore(tmp_path / "known_hosts")
    assert store.validate_or_record("alias", "192.0.2.1", 22, KEY_A) == "recorded"
    assert store.validate_or_record("alias", "192.0.2.1", 22, KEY_A) == "trusted"
    body = (tmp_path / "known_hosts").read_text(encoding="utf-8")
    assert "[192.0.2.1]:22 ssh-ed25519" in body
    assert "alias=alias" in body


def test_changed_key_is_rejected_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "known_hosts"
    store = KnownHostStore(path)
    store.validate_or_record("alias", "192.0.2.1", 22, KEY_A)
    before = path.read_bytes()
    with pytest.raises(HostKeyMismatch, match="host key changed"):
        store.validate_or_record("alias", "192.0.2.1", 22, KEY_B)
    assert path.read_bytes() == before


def test_concurrent_first_use_keeps_one_consistent_key(tmp_path: Path) -> None:
    store = KnownHostStore(tmp_path / "known_hosts", lock_timeout=2)

    def record(key: bytes) -> str:
        try:
            return store.validate_or_record("alias", "host.test", 2222, key)
        except HostKeyMismatch:
            return "mismatch"

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = tuple(pool.map(record, (KEY_A, KEY_B) * 4))
    assert outcomes.count("recorded") == 1
    assert set(outcomes) <= {"recorded", "trusted", "mismatch"}
    lines = (tmp_path / "known_hosts").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
