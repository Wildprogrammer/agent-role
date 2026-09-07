from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from agent_workflow_hub.desktop_client_automation.cua_runtime import probe_cua_driver


def _completed(
    args: list[str], stdout: str, returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args,
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_probe_reports_missing_without_running_a_command() -> None:
    called = False

    def runner(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        raise AssertionError("runner must not be called")

    result = probe_cua_driver(which=lambda _: None, runner=runner)

    assert result == {
        "status": "missing",
        "path": None,
        "version": None,
        "health": None,
        "error": None,
    }
    assert called is False


def test_probe_requires_the_pinned_version(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, "cua-driver 0.23.1\n")

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "incompatible-version"
    assert result["version"] == "0.23.1"
    assert result["health"] is None


def test_probe_rejects_a_version_with_a_build_suffix(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, "cua-driver 0.23.2-nightly\n")

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "incompatible-version"
    assert result["version"] is None
    assert result["health"] is None


def test_probe_accepts_ok_health_report(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def runner(
        args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, kwargs))
        if args[-1] == "--version":
            return _completed(args, "cua-driver 0.23.2\n")
        return _completed(
            args,
            json.dumps(
                {
                    "schema_version": "1",
                    "platform": "win32",
                    "driver_version": "0.23.2",
                    "overall": "ok",
                    "checks": [],
                }
            ),
        )

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "ready"
    assert result["health"]["overall"] == "ok"
    assert [call[0] for call in calls] == [
        [executable, "--version"],
        [
            executable,
            "call",
            "health_report",
            "--json",
            "{}",
            "--compact",
        ],
    ]
    assert all(call[1]["shell"] is False for call in calls)
    assert all(call[1]["timeout"] == 15 for call in calls)


def test_probe_preserves_degraded_health_status(tmp_path: Path) -> None:
    result = _probe_with_health(tmp_path, "degraded")

    assert result["status"] == "degraded"
    assert result["health"]["overall"] == "degraded"


def test_probe_rejects_failed_health_status(tmp_path: Path) -> None:
    result = _probe_with_health(tmp_path, "failed")

    assert result["status"] == "failed"
    assert result["health"]["overall"] == "failed"


def test_probe_reports_timeout_without_retrying(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())
    attempts = 0

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        raise subprocess.TimeoutExpired(args, 15)

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "timeout"
    assert attempts == 1


def test_probe_sanitizes_a_disappearing_executable(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("private executable diagnostic")

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "failed"
    assert result["error"] == "version-command-unavailable"
    assert "private executable diagnostic" not in str(result)


def test_probe_rejects_invalid_health_json(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())
    attempts = 0

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _completed(args, "cua-driver 0.23.2\n")
        return _completed(args, "not-json")

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "invalid-response"
    assert result["health"] is None


def test_probe_reports_nonzero_health_call_without_exposing_stderr(
    tmp_path: Path,
) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())
    attempts = 0

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _completed(args, "cua-driver 0.23.2\n")
        return _completed(args, "", returncode=1, stderr="private diagnostic")

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "failed"
    assert result["error"] == "health-report-failed"
    assert "private diagnostic" not in str(result)


def test_probe_bounds_version_output(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(args, "x" * 70_000)

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "invalid-response"
    assert result["error"] == "version-output-too-large"


def test_probe_sanitizes_health_and_child_environment(
    tmp_path: Path, monkeypatch: Any
) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())
    environments: list[dict[str, str]] = []
    monkeypatch.setenv("CREDENTIAL_FOR_TEST", "must-not-reach-child")

    def runner(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        environments.append(kwargs["env"])
        if args[-1] == "--version":
            return _completed(args, "cua-driver 0.23.2\n")
        return _completed(
            args,
            json.dumps(
                {
                    "schema_version": "1",
                    "platform": "win32",
                    "driver_version": "0.23.2",
                    "overall": "degraded",
                    "checks": [
                        {
                            "name": "ax_capability",
                            "status": "pass",
                            "message": "private diagnostic",
                            "hint": "private remediation",
                            "data": {"path": "private path"},
                        }
                    ],
                    "unexpected": "private value",
                }
            ),
        )

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["health"] == {
        "schema_version": "1",
        "platform": "win32",
        "driver_version": "0.23.2",
        "overall": "degraded",
        "checks": [{"name": "ax_capability", "status": "pass"}],
    }
    assert all("CREDENTIAL_FOR_TEST" not in environment for environment in environments)


def test_probe_rejects_a_mismatched_health_driver_version(tmp_path: Path) -> None:
    executable = str((tmp_path / "cua-driver.exe").resolve())

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[-1] == "--version":
            return _completed(args, "cua-driver 0.23.2\n")
        return _completed(
            args,
            json.dumps(
                {
                    "schema_version": "1",
                    "platform": "win32",
                    "driver_version": "0.23.1",
                    "overall": "ok",
                    "checks": [],
                }
            ),
        )

    result = probe_cua_driver(which=lambda _: executable, runner=runner)

    assert result["status"] == "invalid-response"
    assert result["health"] is None


def _probe_with_health(tmp_path: Path, overall: str) -> dict[str, Any]:
    executable = str((tmp_path / "cua-driver.exe").resolve())
    attempts = 0

    def runner(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return _completed(args, "cua-driver 0.23.2\n")
        return _completed(
            args,
            json.dumps(
                {
                    "schema_version": "1",
                    "platform": "win32",
                    "driver_version": "0.23.2",
                    "overall": overall,
                    "checks": [],
                }
            ),
        )

    return probe_cua_driver(which=lambda _: executable, runner=runner)
