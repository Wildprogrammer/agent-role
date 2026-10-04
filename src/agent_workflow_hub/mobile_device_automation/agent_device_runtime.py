from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol

from .models import ViewportSpec


AGENT_DEVICE_VERSION = "0.21.19"
MAX_OUTPUT_BYTES = 1_048_576
MAX_PUBLIC_DETAIL_CHARS = 768
PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
RUNTIME_PARAMETER_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
PARAMETER_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_-]*)\}")
SECRET_VALUE = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*([:=])\s*([^\s,;]+)"
)
UPSTREAM_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
STATE_DIR_HINT = re.compile(r"--state-dir\s+(?:'([^'\r\n]+)'|\"([^\"\r\n]+)\")")


class RuntimeFailure(RuntimeError):
    """Stable mobile runtime failure safe to expose to workflow callers."""


def runtime_parameter_name(name: str) -> str:
    if not isinstance(name, str) or not PARAMETER_NAME.fullmatch(name):
        raise RuntimeFailure("agent_device_invalid_parameter")
    runtime_name = name.replace("-", "_").upper()
    if (
        not RUNTIME_PARAMETER_NAME.fullmatch(runtime_name)
        or runtime_name.startswith("AD_")
    ):
        raise RuntimeFailure("agent_device_invalid_parameter")
    return runtime_name


def runtime_parameter_values(parameters: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    owners: dict[str, str] = {}
    for name, value in parameters.items():
        if not isinstance(value, str):
            raise RuntimeFailure("agent_device_invalid_parameter")
        runtime_name = runtime_parameter_name(name)
        previous = owners.get(runtime_name)
        if previous is not None and previous != name:
            raise RuntimeFailure("agent_device_parameter_collision")
        owners[runtime_name] = name
        normalized[runtime_name] = value
    return normalized


def _materialize_script_parameters(
    script: Path,
    parameters: Mapping[str, str],
    destination_root: Path,
) -> Path:
    replacements: dict[str, str] = {}
    for name in parameters:
        runtime_name = runtime_parameter_name(name)
        if runtime_name != name:
            replacements[name] = runtime_name
    if not replacements:
        return script
    try:
        text = script.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeFailure("agent_device_invalid_script") from exc
    materialized = PARAMETER_TOKEN.sub(
        lambda match: "${" + replacements.get(match.group(1), match.group(1)) + "}",
        text,
    )
    if materialized == text:
        return script
    digest = hashlib.sha256(materialized.encode("utf-8")).hexdigest()[:16]
    root = destination_root / "materialized-scripts"
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"materialized-{digest}.ad"
    if not destination.exists():
        destination.write_text(materialized, encoding="utf-8", newline="\n")
    return destination.resolve()


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> ProcessResult: ...


@dataclass(frozen=True)
class AgentDeviceProbe:
    status: str
    version: str | None = None
    executable: Path | None = None


@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    name: str
    kind: str
    claimed_by: str | None
    viewport: ViewportSpec | None = None


def _sanitized_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("NODE_OPTIONS", None)
    environment.pop("NODE_PATH", None)
    return environment


def run_process(argv: Sequence[str], *, timeout_seconds: float) -> ProcessResult:
    if not argv or any(not isinstance(value, str) or not value for value in argv):
        raise RuntimeFailure("agent_device_invalid_argv")
    try:
        completed = subprocess.run(
            list(argv),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            env=_sanitized_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeFailure("agent_device_timeout") from exc
    except OSError as exc:
        raise RuntimeFailure(f"agent_device_start_failed: {type(exc).__name__}") from exc
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    if (
        len(stdout.encode("utf-8")) > MAX_OUTPUT_BYTES
        or len(stderr.encode("utf-8")) > MAX_OUTPUT_BYTES
    ):
        raise RuntimeFailure("agent_device_output_too_large")
    return ProcessResult(completed.returncode, stdout, stderr)


def agent_device_executable(hub_root: Path, *, windows: bool | None = None) -> Path:
    is_windows = os.name == "nt" if windows is None else windows
    filename = "agent-device.cmd" if is_windows else "agent-device"
    return (
        hub_root
        / "workspace"
        / "shared"
        / "runtimes"
        / "agent-device"
        / AGENT_DEVICE_VERSION
        / "node_modules"
        / ".bin"
        / filename
    ).resolve(strict=False)


def _redacted_detail(stderr: str) -> str:
    flattened = " ".join(stderr.split())
    redacted = SECRET_VALUE.sub(lambda match: f"{match.group(1)}=[REDACTED]", flattened)
    return redacted[:MAX_PUBLIC_DETAIL_CHARS]


def _error_code(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    if isinstance(code, str) and UPSTREAM_ERROR_CODE.fullmatch(code):
        return code
    return None


def _error_message(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    error = value.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    return message if isinstance(message, str) else None


def _json_value(stdout: str) -> dict[str, object] | None:
    try:
        value = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _command_failure(
    result: ProcessResult, *, upstream_error_code: str | None = None
) -> RuntimeFailure:
    detail = _redacted_detail(result.stderr)
    prefix = (
        "replay_diverged"
        if upstream_error_code == "REPLAY_DIVERGENCE"
        else "agent_device_command_failed"
    )
    message = f"{prefix} upstream_code={result.returncode}"
    if upstream_error_code is not None:
        message += f" upstream_error_code={upstream_error_code}"
    if detail:
        message += f" detail={detail}"
    return RuntimeFailure(message)


def _ensure_executable(executable: Path) -> Path:
    if not executable.is_absolute() or not executable.is_file():
        raise RuntimeFailure("agent_device_missing")
    return executable.resolve()


def _state_dir_arguments(state_dir: Path | None) -> list[str]:
    if state_dir is None:
        return []
    if not state_dir.is_absolute() or not state_dir.is_dir():
        raise RuntimeFailure("agent_device_invalid_state_dir")
    return ["--state-dir", str(state_dir.resolve())]


def _replay_state_dir(value: dict[str, object]) -> Path | None:
    data = value.get("data")
    if not isinstance(data, dict) or data.get("sessionActive") is not True:
        return None
    candidate = data.get("stateDir")
    if not isinstance(candidate, str):
        candidate = None
        for field in ("hint", "message"):
            text = data.get(field)
            if not isinstance(text, str):
                continue
            match = STATE_DIR_HINT.search(text)
            if match:
                candidate = match.group(1) or match.group(2)
                break
    if not isinstance(candidate, str):
        raise RuntimeFailure("agent_device_invalid_state_dir")
    state_dir = Path(candidate)
    if not state_dir.is_absolute() or not state_dir.is_dir():
        raise RuntimeFailure("agent_device_invalid_state_dir")
    return state_dir.resolve()


def _run_json(
    executable: Path,
    arguments: Sequence[str],
    *,
    runner: ProcessRunner,
    timeout_seconds: float,
    ignored_error_codes: frozenset[str] = frozenset(),
) -> dict[str, object]:
    fixed = _ensure_executable(executable)
    result = runner([str(fixed), *arguments], timeout_seconds=timeout_seconds)
    value = _json_value(result.stdout)
    upstream_error_code = _error_code(value)
    if result.returncode != 0:
        if upstream_error_code in ignored_error_codes and value is not None:
            return value
        raise _command_failure(result, upstream_error_code=upstream_error_code)
    if len(result.stdout.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise RuntimeFailure("agent_device_output_too_large")
    if value is None:
        raise RuntimeFailure("agent_device_invalid_json")
    if value.get("success") is False:
        if upstream_error_code in ignored_error_codes:
            return value
        raise _command_failure(result, upstream_error_code=upstream_error_code)
    return value


def probe_agent_device(
    executable: Path,
    *,
    runner: ProcessRunner = run_process,
) -> AgentDeviceProbe:
    if not executable.is_absolute() or not executable.is_file():
        return AgentDeviceProbe(status="missing", executable=executable)
    fixed = executable.resolve()
    try:
        result = runner([str(fixed), "--version"], timeout_seconds=10.0)
    except RuntimeFailure:
        return AgentDeviceProbe(status="runtime-failed", executable=fixed)
    if result.returncode != 0:
        return AgentDeviceProbe(status="runtime-failed", executable=fixed)
    match = re.fullmatch(r"\s*(?:agent-device\s+)?([0-9]+\.[0-9]+\.[0-9]+)\s*", result.stdout)
    if not match:
        return AgentDeviceProbe(status="wrong-version", executable=fixed)
    version = match.group(1)
    status = "ready" if version == AGENT_DEVICE_VERSION else "wrong-version"
    return AgentDeviceProbe(status=status, version=version, executable=fixed)


def _claimed_by(item: dict[str, object]) -> str | None:
    direct = item.get("claimedBy", item.get("claimed_by"))
    if isinstance(direct, str) and direct:
        return direct
    claim = item.get("claim")
    if isinstance(claim, dict):
        owner = claim.get("owner", claim.get("session"))
        if isinstance(owner, str) and owner:
            return owner
    return None


def list_android_devices(
    executable: Path,
    *,
    runner: ProcessRunner = run_process,
) -> tuple[AndroidDevice, ...]:
    payload = _run_json(
        executable,
        ("devices", "--platform", "android", "--json"),
        runner=runner,
        timeout_seconds=20.0,
    )
    if "success" in payload:
        data = payload.get("data")
        if payload.get("success") is not True or not isinstance(data, dict):
            raise RuntimeFailure("agent_device_invalid_json")
        raw_devices = data.get("devices")
    else:
        raw_devices = payload.get("devices")
    if not isinstance(raw_devices, list):
        raise RuntimeFailure("agent_device_invalid_json")
    normalized: list[AndroidDevice] = []
    seen: set[str] = set()
    for raw in raw_devices:
        if not isinstance(raw, dict):
            continue
        platform_name = str(raw.get("platform", "")).casefold()
        target = str(raw.get("target", "")).casefold()
        status = str(raw.get("status", "online")).casefold()
        if (
            platform_name != "android"
            or target != "mobile"
            or raw.get("booted") is not True
            or raw.get("online") is False
            or status in {"offline", "unauthorized", "disconnected"}
        ):
            continue
        identifiers = raw.get("identifiers")
        serial = identifiers.get("serial") if isinstance(identifiers, dict) else None
        if not isinstance(serial, str) or not serial:
            serial = raw.get("id")
        if not isinstance(serial, str) or not serial or serial in seen:
            continue
        seen.add(serial)
        name_value = raw.get("name")
        kind_value = raw.get("kind")
        viewport = None
        viewport_value = raw.get("viewport")
        if isinstance(viewport_value, dict):
            try:
                width = viewport_value["width"]
                height = viewport_value["height"]
                orientation = viewport_value["orientation"]
                density = viewport_value["density"]
                if (
                    isinstance(width, int)
                    and not isinstance(width, bool)
                    and isinstance(height, int)
                    and not isinstance(height, bool)
                    and width > 0
                    and height > 0
                    and orientation in {"portrait", "landscape"}
                    and isinstance(density, (int, float))
                    and not isinstance(density, bool)
                    and density > 0
                ):
                    viewport = ViewportSpec(
                        width=width,
                        height=height,
                        orientation=orientation,
                        density=float(density),
                    )
            except KeyError:
                viewport = None
        normalized.append(
            AndroidDevice(
                serial=serial,
                name=name_value if isinstance(name_value, str) and name_value else serial,
                kind=kind_value if isinstance(kind_value, str) and kind_value else "unknown",
                claimed_by=_claimed_by(raw),
                viewport=viewport,
            )
        )
    return tuple(normalized)


def bind_android_device(
    devices: Sequence[AndroidDevice],
    requested_serial: str | None,
) -> AndroidDevice:
    if requested_serial is not None:
        matches = [device for device in devices if device.serial == requested_serial]
        if len(matches) != 1:
            raise RuntimeFailure("device_not_found")
        selected = matches[0]
    else:
        if not devices:
            raise RuntimeFailure("device_not_found")
        if len(devices) != 1:
            raise RuntimeFailure("ambiguous_device")
        selected = devices[0]
    if selected.claimed_by:
        raise RuntimeFailure("target_identity_changed: device has a foreign claim")
    return selected


def replay_ad(
    executable: Path,
    script: Path,
    *,
    serial: str,
    session: str,
    parameters: Mapping[str, str],
    state_dir: Path | None = None,
    runner: ProcessRunner = run_process,
) -> dict[str, object]:
    runtime_parameters = runtime_parameter_values(parameters)
    with TemporaryDirectory(prefix="agent-device-materialized-") as temporary:
        materialization_root = state_dir or Path(temporary)
        runtime_script = _materialize_script_parameters(
            script, parameters, materialization_root
        )
        arguments = [
            "replay",
            runtime_script.as_posix(),
            "--platform",
            "android",
            "--serial",
            serial,
            "--session",
            session,
            "--keep-session",
        ]
        arguments.extend(_state_dir_arguments(state_dir))
        for name, value in sorted(runtime_parameters.items()):
            arguments.extend(("-e", f"{name}={value}"))
        arguments.append("--json")
        value = _run_json(
            executable,
            arguments,
            runner=runner,
            timeout_seconds=300.0,
        )
    if state_dir is not None:
        return {**value, "_state_dir": str(state_dir.resolve())}
    replay_state_dir = _replay_state_dir(value)
    if replay_state_dir is not None:
        return {**value, "_state_dir": str(replay_state_dir)}
    return value


def wait_selector(
    executable: Path,
    selector: str,
    *,
    serial: str,
    session: str,
    timeout_seconds: float,
    state_dir: Path | None = None,
    runner: ProcessRunner = run_process,
) -> dict[str, object]:
    if not selector or timeout_seconds <= 0:
        raise RuntimeFailure("agent_device_invalid_wait")
    timeout_ms = str(round(timeout_seconds * 1000))
    arguments = [
        "wait",
        selector,
        timeout_ms,
        "--platform",
        "android",
        "--serial",
        serial,
        "--session",
        session,
    ]
    arguments.extend(_state_dir_arguments(state_dir))
    arguments.append("--json")
    value = _run_json(
        executable,
        arguments,
        runner=runner,
        timeout_seconds=timeout_seconds + 5.0,
        ignored_error_codes=frozenset({"COMMAND_FAILED", "SELECTOR_NOT_FOUND"}),
    )
    if value.get("success") is False:
        error_code = _error_code(value)
        error_message = _error_message(value) or ""
        if error_code == "SELECTOR_NOT_FOUND" or (
            error_code == "COMMAND_FAILED"
            and error_message.startswith("wait timed out for selector:")
        ):
            raise RuntimeFailure("selector_not_found")
        raise RuntimeFailure(
            f"agent_device_command_failed upstream_error_code={error_code or 'UNKNOWN'}"
        )
    return value


def close_session(
    executable: Path,
    *,
    serial: str,
    session: str,
    state_dir: Path | None = None,
    runner: ProcessRunner = run_process,
) -> None:
    arguments = [
        "close",
        "--platform",
        "android",
        "--serial",
        serial,
        "--session",
        session,
    ]
    arguments.extend(_state_dir_arguments(state_dir))
    arguments.append("--json")
    _run_json(
        executable,
        arguments,
        runner=runner,
        timeout_seconds=20.0,
        ignored_error_codes=frozenset({"SESSION_NOT_FOUND"}),
    )


@dataclass(frozen=True)
class AgentDeviceRuntime:
    executable: Path
    runner: ProcessRunner = run_process

    def devices(self) -> tuple[AndroidDevice, ...]:
        return list_android_devices(self.executable, runner=self.runner)

    def replay_ad(
        self,
        script: Path,
        *,
        serial: str,
        session: str,
        parameters: Mapping[str, str],
        state_dir: Path | None = None,
    ) -> dict[str, object]:
        return replay_ad(
            self.executable,
            script,
            serial=serial,
            session=session,
            parameters=parameters,
            state_dir=state_dir,
            runner=self.runner,
        )

    def wait_selector(
        self,
        selector: str,
        *,
        serial: str,
        session: str,
        timeout_seconds: float,
        state_dir: Path | None = None,
    ) -> dict[str, object]:
        return wait_selector(
            self.executable,
            selector,
            serial=serial,
            session=session,
            timeout_seconds=timeout_seconds,
            state_dir=state_dir,
            runner=self.runner,
        )

    def close_session(
        self, session: str, *, serial: str, state_dir: Path | None = None
    ) -> None:
        close_session(
            self.executable,
            serial=serial,
            session=session,
            state_dir=state_dir,
            runner=self.runner,
        )
