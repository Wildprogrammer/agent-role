from __future__ import annotations

import pytest

from agent_workflow_hub.ssh_operations.forwarding import ForwardService


class FakeListener:
    def __init__(self, port=41234):
        self.port = port
        self.close_calls = 0
        self.wait_calls = 0

    def get_port(self):
        return self.port

    def close(self):
        self.close_calls += 1

    async def wait_closed(self):
        self.wait_calls += 1


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.listeners = []

    async def forward_local_port(self, *args):
        self.calls.append(("local", args))
        listener = FakeListener()
        self.listeners.append(listener)
        return listener

    async def forward_remote_port(self, *args):
        self.calls.append(("remote", args))
        listener = FakeListener(42222)
        self.listeners.append(listener)
        return listener

    async def forward_socks(self, *args):
        self.calls.append(("socks", args))
        listener = FakeListener(43333)
        self.listeners.append(listener)
        return listener


@pytest.mark.anyio
async def test_local_forward_reports_ready_and_closes_idempotently() -> None:
    connection = FakeConnection()
    handle = await ForwardService(connection).local(
        "127.0.0.1", 0, "127.0.0.1", 8080
    )
    assert handle.ready["status"] == "ready"
    assert handle.ready["listen_port"] == 41234
    await handle.close()
    await handle.close()
    assert handle.closed is True
    assert connection.listeners[0].close_calls == 1


@pytest.mark.anyio
async def test_remote_and_socks_forward_use_matching_asyncssh_calls() -> None:
    connection = FakeConnection()
    service = ForwardService(connection)
    remote = await service.remote("127.0.0.1", 0, "db.internal", 5432)
    socks = await service.socks("127.0.0.1", 0)
    assert remote.ready["mode"] == "remote"
    assert remote.ready["listen_port"] == 42222
    assert socks.ready["mode"] == "socks"
    assert [item[0] for item in connection.calls] == ["remote", "socks"]
