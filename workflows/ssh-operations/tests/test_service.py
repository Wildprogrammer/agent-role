from __future__ import annotations

import asyncio
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from agent_workflow_hub.ssh_operations.models import CommandResult, SSHConfig, TargetConfig
from agent_workflow_hub.ssh_operations.service import (
    SSHOperationsService,
    classify_operation,
    redact_values,
)
from agent_workflow_hub.ssh_operations.transfers import HighImpactConfirmationRequired


@pytest.mark.anyio
async def test_target_failure_does_not_cancel_other_targets() -> None:
    active = 0
    peak = 0

    async def worker(target: str) -> CommandResult:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        if target == "bad":
            raise RuntimeError("offline")
        return CommandResult("success", stdout=target)

    service = SSHOperationsService(operation_worker=worker)
    result = await service.exec_many(("good", "bad", "other"), "hostname", max_parallel=2)
    assert result.status == "partial"
    assert [item.target for item in result.results] == ["good", "bad", "other"]
    assert result.results[2].status == "success"
    assert peak == 2


def test_sudo_and_explicit_overwrite_are_ordinary() -> None:
    assert classify_operation("exec", sudo=True, explicit_high_impact=False) == "ordinary"
    assert classify_operation("upload", overwrite=True, explicit_high_impact=False) == "ordinary"


def test_only_delete_or_main_agent_classification_requires_confirmation() -> None:
    assert classify_operation("remove") == "high-impact"
    assert classify_operation("rmdir") == "high-impact"
    assert classify_operation("exec", explicit_high_impact=True) == "high-impact"
    with pytest.raises(HighImpactConfirmationRequired):
        SSHOperationsService.require_confirmation("remove", confirmed=False)
    SSHOperationsService.require_confirmation("remove", confirmed=True)


def test_redaction_removes_configured_credential_values_recursively() -> None:
    value = {
        "stdout": "prefix secret-value suffix",
        "nested": ["secret-value", {"password": "secret-value"}],
    }
    assert redact_values(value, {"secret-value"}) == {
        "stdout": "prefix *** suffix",
        "nested": ["***", {"password": "***"}],
    }


@pytest.mark.anyio
async def test_explicit_shell_runs_fixed_availability_probe(tmp_path: Path) -> None:
    target = TargetConfig(
        "win", "win.test", username="tester", remote_os="windows", shell="powershell"
    )
    config = SSHConfig(
        tmp_path / "ssh.ini", tmp_path / "known_hosts",
        MappingProxyType({"win": target}),
    )

    class Connection:
        def __init__(self):
            self.calls = []

        async def run(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return SimpleNamespace(exit_status=0, stdout="powershell", stderr="")

    connection = Connection()
    adapter = await SSHOperationsService(config)._detect_adapter(connection, "win")
    assert adapter.name == "powershell"
    assert connection.calls[0][0] == (
        "powershell -NoProfile -NonInteractive -Command "
        "\"Get-Command powershell -ErrorAction Stop | Out-Null\""
    )
