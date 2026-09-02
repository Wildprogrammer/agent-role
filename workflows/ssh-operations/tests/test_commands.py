from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_workflow_hub.ssh_operations.commands import CommandExecutor
from agent_workflow_hub.ssh_operations.models import StepSpec
from agent_workflow_hub.ssh_operations.shells import PosixShell, PowerShell


class FakeConnection:
    def __init__(self, result=None, process=None):
        self.result = result or SimpleNamespace(
            stdout="ok", stderr="", exit_status=0, exit_signal=None
        )
        self.process = process
        self.calls = []

    async def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.result

    async def create_process(self, **kwargs):
        self.calls.append(("create_process", kwargs))
        return self.process


@pytest.mark.anyio
async def test_exec_returns_streams_exit_status_and_duration() -> None:
    connection = FakeConnection()
    result = await CommandExecutor(connection, PosixShell()).exec("printf ok", timeout=5)
    assert result.status == "success"
    assert result.exit_code == 0
    assert result.stdout == "ok"
    assert result.stderr == ""
    assert connection.calls[0][1] == {"check": False, "timeout": 5}


@pytest.mark.anyio
async def test_exec_preserves_nonzero_result() -> None:
    connection = FakeConnection(
        SimpleNamespace(stdout="partial", stderr="bad", exit_status=7, exit_signal=None)
    )
    result = await CommandExecutor(connection, PosixShell()).exec("false")
    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.stdout == "partial"
    assert result.stderr == "bad"


@pytest.mark.anyio
async def test_exec_timeout_is_structured() -> None:
    class TimeoutConnection(FakeConnection):
        async def run(self, command, **kwargs):
            raise asyncio.TimeoutError

    result = await CommandExecutor(TimeoutConnection(), PosixShell()).exec("sleep", timeout=0.01)
    assert result.status == "failed"
    assert result.error == "timeout"


@pytest.mark.anyio
async def test_single_sudo_sends_password_on_stdin_without_putting_it_in_command() -> None:
    connection = FakeConnection()
    result = await CommandExecutor(
        connection, PosixShell(), sudo_password="sudo-secret"
    ).exec("id", sudo=True)
    command, options = connection.calls[0]
    assert result.status == "success"
    assert "sudo-secret" not in command
    assert options["input"] == "sudo-secret\n"


def test_reference_substitution_quotes_only_completed_dependencies() -> None:
    executor = CommandExecutor(FakeConnection(), PosixShell())
    command = executor.substitute_references(
        "printf %s ${steps.one.stdout}",
        {"one": SimpleNamespace(stdout="x'; echo bad")},
        allowed={"one"},
    )
    assert command == "printf %s 'x'\"'\"'; echo bad'"
    with pytest.raises(ValueError, match="dependency"):
        executor.substitute_references(
            "printf %s ${steps.other.stdout}", {}, allowed={"one"}
        )


@pytest.mark.anyio
async def test_windows_sudo_returns_needs_elevation() -> None:
    result = await CommandExecutor(FakeConnection(), PowerShell()).exec("whoami", sudo=True)
    assert result.status == "needs-elevation"
    assert not result.stdout
