from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.contracts import HostFacts
from agent_workflow_hub.specialized_agent_deployment.hosts.base import (
    ApplyContext,
    HostAdapter,
    HostApplyResult,
    VerifyContext,
    guidance_host_facts,
)
from agent_workflow_hub.specialized_agent_deployment.runner import (
    CommandExecutionError,
    CommandRunner,
)


def test_runner_accepts_only_argv_tuple() -> None:
    runner = CommandRunner()
    with pytest.raises(CommandExecutionError):
        runner.run("hermes --version", phase="preview")  # type: ignore[arg-type]
    with pytest.raises(CommandExecutionError):
        runner.run((), phase="preview")
    with pytest.raises(CommandExecutionError):
        runner.run(("hermes", "bad\x00arg"), phase="preview")


@pytest.mark.parametrize(
    "argv",
    [
        ("pnpm", "install"),
        ("npm", "run", "build"),
        ("npx", "tool"),
        ("pip", "install", "package"),
        ("uv", "pip", "install", "package"),
        ("hermes", "profile", "create", "agent"),
        ("hermes", "profile", "delete", "agent"),
        ("hermes", "--profile", "agent", "config", "set", "x", "y"),
    ],
)
def test_preview_rejects_installers_and_host_writes(
    argv: tuple[str, ...],
) -> None:
    with pytest.raises(CommandExecutionError):
        CommandRunner().run(argv, phase="preview")


def test_runner_rejects_package_manager_by_absolute_executable_path() -> None:
    with pytest.raises(CommandExecutionError):
        CommandRunner().run(
            (r"C:\tools\pnpm.exe", "install"),
            phase="apply",
        )


def test_preview_allows_fixed_read_only_git_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"value", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    argv = ("git", "-C", str(tmp_path.resolve()), "rev-parse", "HEAD")
    assert CommandRunner().run(argv, phase="preview").stdout == "value"


@pytest.mark.parametrize(
    "argv",
    [
        ("hermes", "--version"),
        ("hermes", "profile", "list"),
        ("hermes", "profile", "create", "--help"),
        ("hermes", "--profile", "agent", "config", "path"),
        ("hermes", "--profile", "agent", "config", "get", "terminal.cwd"),
        ("hermes", "--profile", "agent", "skills", "list"),
    ],
)
def test_preview_allows_only_documented_read_commands(
    argv: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"ok", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert CommandRunner().run(argv, phase="preview").stdout == "ok"


def test_runner_uses_shell_false_and_strict_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(args[0], 7, b"stdout", b"stderr")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = CommandRunner().run(("hermes", "--version"), phase="preview")
    assert result.exit_code == 7
    assert result.stdout == "stdout"
    assert result.stderr == "stderr"
    assert captured["shell"] is False
    assert captured["check"] is False
    assert captured["capture_output"] is True

    def invalid_utf8(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"\xff", b"")

    monkeypatch.setattr(subprocess, "run", invalid_utf8)
    with pytest.raises(CommandExecutionError):
        CommandRunner().run(("hermes", "--version"), phase="preview")


def test_runner_rejects_unbounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def too_large(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, b"x" * 65_537, b"")

    monkeypatch.setattr(subprocess, "run", too_large)
    with pytest.raises(CommandExecutionError):
        CommandRunner(max_output_bytes=65_536).run(
            ("hermes", "--version"), phase="preview"
        )


def test_missing_or_unknown_host_is_guidance_not_install(tmp_path: Path) -> None:
    missing = guidance_host_facts(
        host="hermes",
        compatibility="missing",
        version=None,
        target_root=None,
        guidance=("Install the host yourself, then retry preview.",),
    )
    unknown = guidance_host_facts(
        host="deepseek-harness",
        compatibility="unverified",
        version="unknown",
        target_root=(tmp_path / "preset").resolve(),
        guidance=("Use a documented compatible version.",),
    )
    assert isinstance(missing, HostFacts)
    assert missing.compatibility == "missing"
    assert unknown.compatibility == "unverified"
    assert "guidance" in unknown.facts


def test_host_protocol_separates_lifecycle_methods() -> None:
    for method in ("discover", "plan_writes", "apply", "verify"):
        assert hasattr(HostAdapter, method)
    assert ApplyContext.__dataclass_fields__
    assert VerifyContext.__dataclass_fields__
    assert HostApplyResult.__dataclass_fields__
