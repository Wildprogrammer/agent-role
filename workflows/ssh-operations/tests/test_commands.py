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


class BlockingStream:
    async def readline(self):
        await asyncio.Event().wait()


class SequenceStream:
    def __init__(self, *lines):
        self.lines = list(lines)

    async def readline(self):
        if self.lines:
            return self.lines.pop(0)
        await asyncio.Event().wait()


class FakeStdin:
    def __init__(self):
        self.writes = []

    def write(self, value):
        self.writes.append(value)

    def write_eof(self):
        return None


class BlockingProcess:
    def __init__(self):
        self.stdin = FakeStdin()
        self.stdout = BlockingStream()
        self.stderr = BlockingStream()
        self.terminated = False
        self.killed = False

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait_closed(self):
        return None


class OutputLimitProcess(BlockingProcess):
    def __init__(self):
        super().__init__()
        self.stdout = SequenceStream("output-too-large\n")


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
    class TimeoutThenCleanupConnection(FakeConnection):
        def __init__(self):
            super().__init__()
            self.run_count = 0

        async def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            self.run_count += 1
            if self.run_count == 1:
                raise asyncio.TimeoutError
            return SimpleNamespace(
                stdout="4242\n", stderr="", exit_status=0, exit_signal=None
            )

    connection = TimeoutThenCleanupConnection()
    result = await CommandExecutor(connection, PosixShell()).exec("sleep", timeout=0.01)
    assert result.status == "failed"
    assert result.error == "timeout"
    assert result.execution_state == "timed_out_cleaned"
    assert result.cleanup_attempted is True
    assert result.cleanup_verified is True
    assert result.remote_pid == 4242
    assert "setsid sh -c" in connection.calls[0][0]
    assert "setsid sh -lc" not in connection.calls[0][0]
    assert "exec 3<&0" in connection.calls[0][0]
    assert "<&3 &" in connection.calls[0][0]
    assert "kill -TERM" in connection.calls[1][0]


@pytest.mark.anyio
async def test_exec_timeout_without_supported_cleanup_reports_unknown_state() -> None:
    class TimeoutConnection(FakeConnection):
        async def run(self, command, **kwargs):
            raise asyncio.TimeoutError

    result = await CommandExecutor(TimeoutConnection(), PowerShell()).exec(
        "Start-Sleep 30", timeout=0.01
    )
    assert result.status == "failed"
    assert result.error == "timeout"
    assert result.execution_state == "unknown"
    assert result.cleanup_attempted is False
    assert result.cleanup_verified is None


@pytest.mark.anyio
async def test_exec_cancellation_attempts_posix_process_group_cleanup() -> None:
    class CancelThenCleanupConnection(FakeConnection):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.run_count = 0

        async def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            self.run_count += 1
            if self.run_count == 1:
                self.started.set()
                await asyncio.Event().wait()
            return SimpleNamespace(
                stdout="4242\n", stderr="", exit_status=0, exit_signal=None
            )

    connection = CancelThenCleanupConnection()
    task = asyncio.create_task(
        CommandExecutor(connection, PosixShell()).exec("sleep 30", timeout=30)
    )
    await connection.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(connection.calls) == 2
    assert "kill -TERM" in connection.calls[1][0]


@pytest.mark.anyio
async def test_exec_marks_successful_but_truncated_output_partial() -> None:
    connection = FakeConnection(
        SimpleNamespace(stdout="123456", stderr="abcdef", exit_status=0, exit_signal=None)
    )
    result = await CommandExecutor(
        connection, PosixShell(), output_limit=4
    ).exec("printf output")
    assert result.status == "partial"
    assert result.stdout == "1234"
    assert result.stderr == "abcd"
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert result.captured_stdout_bytes == 4
    assert result.captured_stderr_bytes == 4
    assert result.output_limit_bytes == 4


@pytest.mark.anyio
async def test_run_steps_inherits_default_timeout_and_closes_shell() -> None:
    process = BlockingProcess()
    connection = FakeConnection(process=process)
    results = await asyncio.wait_for(
        CommandExecutor(connection, PowerShell()).run_steps(
            (StepSpec("slow", "Start-Sleep 30"),), default_timeout=0.01
        ),
        timeout=0.2,
    )
    assert results[0].status == "failed"
    assert results[0].error == "timeout"
    assert results[0].execution_state == "unknown"
    assert process.terminated is True


@pytest.mark.anyio
async def test_run_steps_output_limit_cleans_posix_process_group() -> None:
    process = OutputLimitProcess()
    connection = FakeConnection(
        result=SimpleNamespace(
            stdout="4242\n", stderr="", exit_status=0, exit_signal=None
        ),
        process=process,
    )
    results = await CommandExecutor(
        connection, PosixShell(), output_limit=4
    ).run_steps((StepSpec("noisy", "yes output", timeout_seconds=1),))
    assert results[0].status == "failed"
    assert results[0].error == "remote step output limit exceeded"
    assert results[0].execution_state == "aborted_cleaned"
    assert results[0].cleanup_attempted is True
    assert results[0].cleanup_verified is True
    assert results[0].remote_pid == 4242
    assert process.terminated is True


@pytest.mark.anyio
async def test_run_steps_cancellation_attempts_posix_process_group_cleanup() -> None:
    process = BlockingProcess()
    connection = FakeConnection(
        result=SimpleNamespace(
            stdout="4242\n", stderr="", exit_status=0, exit_signal=None
        ),
        process=process,
    )
    task = asyncio.create_task(
        CommandExecutor(connection, PosixShell()).run_steps(
            (StepSpec("slow", "sleep 30", timeout_seconds=30),)
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert any("kill -TERM" in call[0] for call in connection.calls if call[0] != "create_process")
    assert process.terminated is True


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


@pytest.mark.anyio
async def test_secret_environment_uses_stdin_without_putting_value_in_command() -> None:
    connection = FakeConnection()
    result = await CommandExecutor(connection, PosixShell()).exec(
        "printenv API_TOKEN",
        secret_environment={"API_TOKEN": "token-value"},
    )
    command, options = connection.calls[0]
    assert result.status == "success"
    assert "token-value" not in command
    assert "API_TOKEN" in command
    assert options["input"] == "dG9rZW4tdmFsdWU=\n"


@pytest.mark.anyio
async def test_sudo_password_precedes_secret_environment_on_stdin() -> None:
    connection = FakeConnection()
    await CommandExecutor(
        connection, PosixShell(), sudo_password="sudo-secret"
    ).exec(
        "printenv API_TOKEN",
        sudo=True,
        secret_environment={"API_TOKEN": "token-value"},
    )
    command, options = connection.calls[0]
    assert "token-value" not in command
    assert options["input"] == "sudo-secret\ndG9rZW4tdmFsdWU=\n"


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
