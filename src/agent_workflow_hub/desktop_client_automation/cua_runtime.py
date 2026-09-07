from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


PINNED_CUA_VERSION = "0.23.2"
VERSION_PATTERN = re.compile(r"(?:cua-driver\s+)?(?P<version>\d+\.\d+\.\d+)")
MAX_PROBE_OUTPUT_BYTES = 64 * 1024
MAX_HEALTH_CHECKS = 64
Runner = Callable[..., subprocess.CompletedProcess[str]]
Which = Callable[[str], str | None]


class ProbeOutputTooLarge(RuntimeError):
    pass


def _result(
    status: str,
    *,
    path: str | None,
    version: str | None,
    health: dict[str, Any] | None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "path": path,
        "version": version,
        "health": health,
        "error": error,
    }


def _probe_environment() -> dict[str, str]:
    allowed = {
        "appdata",
        "home",
        "lang",
        "lc_all",
        "localappdata",
        "path",
        "pathext",
        "systemroot",
        "temp",
        "tmp",
        "tmpdir",
        "userprofile",
        "windir",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key.casefold() in allowed
    }


def _bounded_stdout(value: str | bytes | None) -> str:
    if value is None:
        return ""
    raw = value.encode("utf-8") if isinstance(value, str) else value
    if len(raw) > MAX_PROBE_OUTPUT_BYTES:
        raise ProbeOutputTooLarge
    return raw.decode("utf-8", errors="replace")


def _run(
    runner: Runner | None,
    args: list[str],
) -> subprocess.CompletedProcess[str]:
    environment = _probe_environment()
    if runner is not None:
        completed = runner(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
            env=environment,
        )
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout=_bounded_stdout(completed.stdout),
            stderr="",
        )

    with tempfile.TemporaryFile() as stdout_file:
        completed = subprocess.run(
            args,
            stdout=stdout_file,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            shell=False,
            env=environment,
        )
        size = stdout_file.tell()
        if size > MAX_PROBE_OUTPUT_BYTES:
            raise ProbeOutputTooLarge
        stdout_file.seek(0)
        stdout = _bounded_stdout(stdout_file.read(MAX_PROBE_OUTPUT_BYTES + 1))
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout=stdout,
        stderr="",
    )


def _sanitize_health(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != "1":
        return None
    platform_name = value.get("platform")
    driver_version = value.get("driver_version")
    overall = value.get("overall")
    checks = value.get("checks")
    if platform_name not in {"darwin", "win32", "linux"}:
        return None
    if driver_version != PINNED_CUA_VERSION:
        return None
    if overall not in {"ok", "degraded", "failed"}:
        return None
    if not isinstance(checks, list) or len(checks) > MAX_HEALTH_CHECKS:
        return None
    sanitized_checks: list[dict[str, str]] = []
    for check in checks:
        if not isinstance(check, dict):
            return None
        name = check.get("name")
        status = check.get("status")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > 128
            or status not in {"pass", "fail", "skip"}
        ):
            return None
        sanitized_checks.append({"name": name, "status": status})
    return {
        "schema_version": "1",
        "platform": platform_name,
        "driver_version": driver_version,
        "overall": overall,
        "checks": sanitized_checks,
    }


def probe_cua_driver(
    *,
    which: Which = shutil.which,
    runner: Runner | None = None,
) -> dict[str, Any]:
    discovered = which("cua-driver")
    if discovered is None:
        return _result(
            "missing",
            path=None,
            version=None,
            health=None,
        )
    executable = str(Path(discovered).resolve(strict=False))
    try:
        version_call = _run(runner, [executable, "--version"])
    except subprocess.TimeoutExpired:
        return _result(
            "timeout",
            path=executable,
            version=None,
            health=None,
            error="version-timeout",
        )
    except ProbeOutputTooLarge:
        return _result(
            "invalid-response",
            path=executable,
            version=None,
            health=None,
            error="version-output-too-large",
        )
    except OSError:
        return _result(
            "failed",
            path=executable,
            version=None,
            health=None,
            error="version-command-unavailable",
        )
    if version_call.returncode != 0:
        return _result(
            "failed",
            path=executable,
            version=None,
            health=None,
            error="version-command-failed",
        )
    match = VERSION_PATTERN.fullmatch(version_call.stdout.strip())
    version = match.group("version") if match else None
    if version != PINNED_CUA_VERSION:
        return _result(
            "incompatible-version",
            path=executable,
            version=version,
            health=None,
            error="version-mismatch",
        )
    try:
        health_call = _run(
            runner,
            [
                executable,
                "call",
                "health_report",
                "--json",
                "{}",
                "--compact",
            ],
        )
    except subprocess.TimeoutExpired:
        return _result(
            "timeout",
            path=executable,
            version=version,
            health=None,
            error="health-report-timeout",
        )
    except ProbeOutputTooLarge:
        return _result(
            "invalid-response",
            path=executable,
            version=version,
            health=None,
            error="health-output-too-large",
        )
    except OSError:
        return _result(
            "failed",
            path=executable,
            version=version,
            health=None,
            error="health-command-unavailable",
        )
    if health_call.returncode != 0:
        return _result(
            "failed",
            path=executable,
            version=version,
            health=None,
            error="health-report-failed",
        )
    try:
        raw_health = json.loads(health_call.stdout)
    except json.JSONDecodeError:
        return _result(
            "invalid-response",
            path=executable,
            version=version,
            health=None,
            error="invalid-health-json",
        )
    health = _sanitize_health(raw_health)
    if health is None:
        return _result(
            "invalid-response",
            path=executable,
            version=version,
            health=None,
            error="unsupported-health-schema",
        )
    status = {
        "ok": "ready",
        "degraded": "degraded",
        "failed": "failed",
    }.get(health.get("overall"), "invalid-response")
    error = None if status in {"ready", "degraded"} else "health-not-ready"
    return _result(
        status,
        path=executable,
        version=version,
        health=health,
        error=error,
    )
