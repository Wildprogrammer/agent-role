from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from agent_workflow_hub.ssh_operations.connections import ConnectionManager
from agent_workflow_hub.ssh_operations.models import SSHConfig, TargetConfig


class FakeConnection:
    def __init__(self, name):
        self.name = name
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def _config(tmp_path: Path) -> SSHConfig:
    targets = {
        "bastion": TargetConfig(
            "bastion", "bastion.test", username="jump", auth="password", password="jump-pass"
        ),
        "leaf": TargetConfig(
            "leaf", "leaf.test", port=2222, username="user", auth="key",
            private_key=tmp_path / "id", private_key_passphrase="key-pass",
            via="bastion", forward_agent=True,
        ),
    }
    return SSHConfig(
        tmp_path / "ssh.ini", tmp_path / "known_hosts", MappingProxyType(targets)
    )


@pytest.mark.anyio
async def test_jump_chain_uses_tunnel_and_closes_in_reverse_order(tmp_path: Path) -> None:
    calls = []

    async def connect(host, **options):
        connection = FakeConnection("bastion" if host == "bastion.test" else "leaf")
        calls.append((host, options, connection))
        return connection

    manager = ConnectionManager(_config(tmp_path), connect_callable=connect)
    async with manager.connect("leaf") as leaf:
        assert leaf.name == "leaf"
        assert calls[0][1]["tunnel"] is None
        assert calls[1][1]["tunnel"] is calls[0][2]
        assert calls[1][1]["client_keys"] == [str(tmp_path / "id")]
        assert calls[1][1]["agent_forwarding"] is True
    assert manager.close_order == ("leaf", "bastion")
    assert all(item[2].closed for item in calls)


@pytest.mark.anyio
async def test_password_auth_is_not_exposed_in_host_or_username(tmp_path: Path) -> None:
    calls = []

    async def connect(host, **options):
        calls.append((host, options))
        return FakeConnection("bastion")

    manager = ConnectionManager(_config(tmp_path), connect_callable=connect)
    async with manager.connect("bastion"):
        pass
    assert calls[0][0] == "bastion.test"
    assert calls[0][1]["username"] == "jump"
    assert calls[0][1]["password"] == "jump-pass"
    assert calls[0][1]["client_keys"] is None
    assert calls[0][1]["agent_path"] is None
    assert "jump-pass" not in repr(calls[0][0])


@pytest.mark.anyio
async def test_pre_auth_socket_failure_retries_but_auth_failure_does_not(tmp_path: Path) -> None:
    attempts = 0

    async def flaky(host, **options):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary refusal")
        return FakeConnection("bastion")

    manager = ConnectionManager(_config(tmp_path), connect_callable=flaky, retries=1)
    async with manager.connect("bastion"):
        pass
    assert attempts == 2

    class PermissionDenied(Exception):
        pass

    attempts = 0

    async def denied(host, **options):
        nonlocal attempts
        attempts += 1
        raise PermissionDenied("bad password")

    manager = ConnectionManager(
        _config(tmp_path), connect_callable=denied, retries=3,
        non_retryable=(PermissionDenied,),
    )
    with pytest.raises(PermissionDenied):
        async with manager.connect("bastion"):
            pass
    assert attempts == 1
