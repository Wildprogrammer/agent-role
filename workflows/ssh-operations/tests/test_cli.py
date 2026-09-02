from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from agent_workflow_hub.ssh_operations import cli
from agent_workflow_hub.ssh_operations.models import CommandResult, OperationResult


class FakeService:
    config = SimpleNamespace(targets={})

    async def exec_many(self, targets, command, **kwargs):
        return OperationResult(
            "success", (CommandResult("success", stdout="host", target=targets[0]),)
        )


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "ssh.ini"
    path.write_text(
        "[target:linux]\nhost=127.0.0.1\nusername=tester\n",
        encoding="utf-8",
    )
    return path.resolve()


def test_exec_prints_one_json_object(monkeypatch, capsys, tmp_path: Path) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "build_service", lambda _: FakeService())
    code = cli.main(
        [
            "exec", "--config", str(config), "--target", "linux",
            "--command", "hostname",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert len(captured.out.splitlines()) == 1
    assert json.loads(captured.out)["status"] == "success"
    assert captured.err == ""


def test_relative_config_is_rejected_without_traceback(capsys) -> None:
    code = cli.main(
        ["exec", "--config", "ssh.ini", "--target", "linux", "--command", "hostname"]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert json.loads(captured.out)["status"] == "invalid-request"
    assert "absolute" in captured.out


def test_doctor_never_connects_and_reports_dependency(monkeypatch, capsys, tmp_path: Path) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(cli, "asyncssh_version", lambda: None)
    code = cli.main(["doctor", "--config", str(config)])
    report = json.loads(capsys.readouterr().out)
    assert code == 3
    assert report["status"] == "needs_dependency"
    assert report["targets"] == ["linux"]


def test_launcher_discovers_workflow_private_runtime(tmp_path: Path) -> None:
    config = _config(tmp_path)
    script = Path(__file__).resolve().parents[1] / "scripts" / "ssh_operations.py"
    completed = subprocess.run(
        [sys.executable, str(script), "doctor", "--config", str(config)],
        check=False,
        text=True,
        capture_output=True,
        encoding="utf-8",
    )
    report = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert report["status"] == "ready"
    assert report["asyncssh_version"] == "2.24.0"
    assert report["connected"] is False
