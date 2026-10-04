from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import platform
import re
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from pathlib import Path

from .agent_device_runtime import (
    AGENT_DEVICE_VERSION,
    AgentDeviceRuntime,
    RuntimeFailure,
    agent_device_executable,
    bind_android_device,
    probe_agent_device,
)
from .airtest_runtime import (
    AirtestRuntime,
    capture_device,
    click_image,
    crop_template,
    locate_image,
)
from .contracts import ContractError, load_request
from .composition import compose_bundle
from .composition_contracts import load_composition_request
from .readiness import evaluate_readiness
from .renderer import compile_bundle
from .runner import preview_bundle, run_bundle


HUB_ROOT = Path(__file__).resolve().parents[3]
IME_HELPER_PACKAGE = "com.callstack.agentdevice.imehelper"
IME_HELPER_VERSION_CODE = 21019


class _JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _absolute_existing_file(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ContractError(f"{label} must be an existing absolute file")
    return path.resolve()


def _absolute_directory(value: str, label: str, *, existing: bool) -> Path:
    path = Path(value)
    if not path.is_absolute() or (existing and not path.is_dir()):
        requirement = "existing absolute directory" if existing else "absolute directory"
        raise ContractError(f"{label} must be an {requirement}")
    return path.resolve(strict=False)


def _absolute_output(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ContractError(f"{label} must be absolute")
    return path.resolve(strict=False)


def _threshold(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("threshold must be numeric") from exc
    if not math.isfinite(number) or not 0 < number <= 1:
        raise argparse.ArgumentTypeError("threshold must be greater than zero and at most one")
    return number


def _positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return number


def _digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("digest must be 64 lowercase hexadecimal characters")
    return value


def _make_agent_runtime() -> AgentDeviceRuntime:
    return AgentDeviceRuntime(agent_device_executable(HUB_ROOT))


def _make_airtest_runtime() -> AirtestRuntime:
    return AirtestRuntime()


def _node_version() -> str | None:
    try:
        completed = subprocess.run(
            ["node", "--version"],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def _runtime_python() -> Path:
    runtime = HUB_ROOT / "workspace" / "workflows" / "mobile-device-automation" / "runtime"
    return runtime / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python")


def _airtest_ready() -> bool:
    try:
        version = importlib.metadata.version("airtest")
    except importlib.metadata.PackageNotFoundError:
        return False
    expected = _runtime_python().resolve(strict=False)
    actual = Path(sys.executable).resolve(strict=False)
    return (
        version == "1.4.3"
        and actual == expected
        and sys.version_info[:2] == (3, 11)
        and _runtime_lock_status()["status"] == "hash-locked"
    )


def _host_architecture() -> str:
    machine = platform.machine().casefold()
    if machine in {"amd64", "x86_64", "x64"}:
        return "amd64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    build_platform = sysconfig.get_platform().casefold()
    if "amd64" in build_platform or "x86_64" in build_platform:
        return "amd64"
    if "arm64" in build_platform or "aarch64" in build_platform:
        return "arm64"
    return machine or build_platform


def _runtime_lock_status() -> dict[str, object]:
    system = platform.system()
    machine = _host_architecture()
    if system == "Windows" and machine == "amd64":
        filename = "runtime-mobile-windows-x64-py311.lock"
    elif system == "Darwin" and machine == "arm64":
        filename = "runtime-mobile-macos-arm64-py311.lock"
    elif system == "Linux" and machine == "amd64":
        filename = "runtime-mobile-linux-x64-py311.lock"
    else:
        return {"status": "unsupported-profile", "path": None}
    path = HUB_ROOT / "workflows" / "mobile-device-automation" / "references" / filename
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "missing", "path": str(path)}
    status = (
        "unverified-runtime-lock"
        if "unverified-runtime-lock" in text
        else "hash-locked"
        if "airtest==1.4.3" in text and "--hash=sha256:" in text
        else "invalid"
    )
    return {"status": status, "path": str(path)}


def _selected_viewport(serial: str):
    runtime = _make_agent_runtime()
    selected = bind_android_device(runtime.devices(), requested_serial=serial)
    if selected.viewport is not None:
        return selected.viewport
    return _make_airtest_runtime().inspect_viewport(serial)


def _ime_helper_status(serial: str) -> dict[str, object]:
    try:
        completed = subprocess.run(
            ["adb", "-s", serial, "shell", "dumpsys", "package", IME_HELPER_PACKAGE],
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"status": "unverified", "version_code": None}
    if completed.returncode != 0:
        return {"status": "unverified", "version_code": None}
    match = re.search(r"\bversionCode=(\d+)", completed.stdout or "")
    if match is None:
        return {"status": "missing", "version_code": None}
    version_code = int(match.group(1))
    return {
        "status": "installed" if version_code == IME_HELPER_VERSION_CODE else "wrong-version",
        "version_code": version_code,
    }


def _doctor(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    executable = agent_device_executable(HUB_ROOT)
    probe = probe_agent_device(executable)
    devices = ()
    if probe.status == "ready":
        try:
            devices = _make_agent_runtime().devices()
            if args.serial is not None:
                devices = (bind_android_device(devices, args.serial),)
        except RuntimeFailure as exc:
            if str(exc).startswith("ambiguous_device"):
                devices = tuple(_make_agent_runtime().devices())
            else:
                devices = ()
    lock_status = _runtime_lock_status()
    airtest_ready = _airtest_ready()
    readiness = evaluate_readiness(
        os_name=platform.system(),
        mode=args.mode,
        node_version=_node_version(),
        agent_probe=probe,
        airtest_ready=airtest_ready,
        devices=tuple(devices),
    )
    ime_helper = (
        _ime_helper_status(devices[0].serial)
        if len(devices) == 1
        else {"status": "not-checked", "version_code": None}
    )
    text_input_mode = (
        "direct-ime"
        if ime_helper["status"] == "installed"
        else "pinyin-fallback" if len(devices) == 1 else "not-checked"
    )
    result = {
        **readiness,
        "agent_device_version": probe.version,
        "expected_agent_device_version": AGENT_DEVICE_VERSION,
        "airtest_version": "1.4.3" if airtest_ready else None,
        "runtime_lock": lock_status,
        "ime_helper": ime_helper,
        "text_input_mode": text_input_mode,
    }
    return (0 if readiness["status"] == "ready" else 2), result


def _devices(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    devices = _make_agent_runtime().devices()
    if args.serial is not None:
        devices = (bind_android_device(devices, args.serial),)
    return 0, {
        "status": "ready",
        "devices": [
            {
                "serial": device.serial,
                "name": device.name,
                "kind": device.kind,
                "claimed": device.claimed_by is not None,
            }
            for device in devices
        ],
    }


def _capture(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    output = _absolute_output(args.output, "output")
    crop_values = (args.x, args.y, args.width, args.height)
    if any(value is not None for value in crop_values) and any(
        value is None for value in crop_values
    ):
        raise ContractError("capture crop requires x, y, width, and height together")
    viewport = _selected_viewport(args.serial)
    runtime = _make_airtest_runtime()
    if all(value is None for value in crop_values):
        result = capture_device(
            serial=args.serial,
            expected_viewport=viewport,
            output_path=output,
            dependencies_factory=runtime.dependencies_factory,
        )
        return 0, result
    source = output.with_name(f"{output.stem}-source.png")
    capture_device(
        serial=args.serial,
        expected_viewport=viewport,
        output_path=source,
        dependencies_factory=runtime.dependencies_factory,
    )
    result = crop_template(
        source_screenshot=source,
        output_path=output,
        crop_box=tuple(crop_values),
        viewport=viewport,
        threshold=args.threshold,
        description=args.description,
        dependencies_factory=runtime.dependencies_factory,
    )
    result["source_screenshot"] = str(source)
    return 0, result


def _locate(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    template = _absolute_existing_file(args.template, "template")
    viewport = _selected_viewport(args.serial)
    runtime = _make_airtest_runtime()
    result = locate_image(
        serial=args.serial,
        expected_viewport=viewport,
        template_path=template,
        threshold=args.threshold,
        timeout_seconds=args.timeout_seconds,
        dependencies_factory=runtime.dependencies_factory,
    )
    return 0, result


def _click(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    template = _absolute_existing_file(args.template, "template")
    evidence = _absolute_directory(args.evidence_dir, "evidence-dir", existing=False)
    viewport = _selected_viewport(args.serial)
    runtime = _make_airtest_runtime()
    result = click_image(
        serial=args.serial,
        expected_viewport=viewport,
        template_path=template,
        evidence_dir=evidence,
        action_id=args.action_id,
        threshold=args.threshold,
        timeout_seconds=args.timeout_seconds,
        dependencies_factory=runtime.dependencies_factory,
    )
    return 0, result


def _compile(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    request_path = _absolute_existing_file(args.request, "request")
    output_root = _absolute_directory(args.output_root, "output-root", existing=False)
    request = load_request(request_path)
    bundle = compile_bundle(request, output_root)
    return 0, {
        "status": "generated-unverified",
        "bundle": str(bundle),
        "plan_sha256": preview_bundle(bundle)["plan_sha256"],
    }


def _compose(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    request_path = _absolute_existing_file(args.request, "request")
    output_root = _absolute_directory(args.output_root, "output-root", existing=False)
    request = load_composition_request(request_path)
    bundle = compose_bundle(request, output_root)
    return 0, {
        "status": "generated-unverified",
        "bundle": str(bundle),
        "plan_sha256": preview_bundle(bundle)["plan_sha256"],
    }


def _preview(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    bundle = _absolute_directory(args.bundle, "bundle", existing=True)
    return 0, preview_bundle(bundle)


def _replay(args: argparse.Namespace) -> tuple[int, dict[str, object]]:
    bundle = _absolute_directory(args.bundle, "bundle", existing=True)
    parameters = (
        _absolute_existing_file(args.parameters, "parameters")
        if args.parameters is not None
        else None
    )
    completed = run_bundle(
        bundle,
        parameters_path=parameters,
        confirmed_plan_sha256=args.confirmed_plan_sha256,
        agent_runtime=_make_agent_runtime(),
        airtest_runtime=_make_airtest_runtime(),
    )
    if completed == 0:
        return 0, {"status": "replay-verified", "bundle": str(bundle)}
    return 1, {"status": "replay-failed", "bundle": str(bundle)}


def build_parser() -> argparse.ArgumentParser:
    parser = _JsonArgumentParser(prog="mobile-device-automation")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--mode", choices=("explore", "image", "full"), default="full")
    doctor.add_argument("--serial")
    doctor.set_defaults(handler=_doctor)

    devices = commands.add_parser("devices")
    devices.add_argument("--serial")
    devices.set_defaults(handler=_devices)

    capture = commands.add_parser("capture")
    capture.add_argument("--serial", required=True)
    capture.add_argument("--output", required=True)
    capture.add_argument("--x", type=int)
    capture.add_argument("--y", type=int)
    capture.add_argument("--width", type=int)
    capture.add_argument("--height", type=int)
    capture.add_argument("--threshold", type=_threshold, default=0.85)
    capture.add_argument("--description", default="Captured mobile image template")
    capture.set_defaults(handler=_capture)

    locate = commands.add_parser("locate-image")
    locate.add_argument("--serial", required=True)
    locate.add_argument("--template", required=True)
    locate.add_argument("--threshold", type=_threshold, default=0.85)
    locate.add_argument("--timeout-seconds", type=_positive_float, default=10.0)
    locate.set_defaults(handler=_locate)

    click = commands.add_parser("click-image")
    click.add_argument("--serial", required=True)
    click.add_argument("--template", required=True)
    click.add_argument("--evidence-dir", required=True)
    click.add_argument("--action-id", required=True)
    click.add_argument("--threshold", type=_threshold, default=0.85)
    click.add_argument("--timeout-seconds", type=_positive_float, default=10.0)
    click.set_defaults(handler=_click)

    compile_parser = commands.add_parser("compile")
    compile_parser.add_argument("--request", required=True)
    compile_parser.add_argument("--output-root", required=True)
    compile_parser.set_defaults(handler=_compile)

    compose_parser = commands.add_parser("compose")
    compose_parser.add_argument("--request", required=True)
    compose_parser.add_argument("--output-root", required=True)
    compose_parser.set_defaults(handler=_compose)

    preview = commands.add_parser("preview")
    preview.add_argument("--bundle", required=True)
    preview.set_defaults(handler=_preview)

    replay = commands.add_parser("replay")
    replay.add_argument("--bundle", required=True)
    replay.add_argument("--parameters")
    replay.add_argument("--confirmed-plan-sha256", type=_digest)
    replay.set_defaults(handler=_replay)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        code, result = args.handler(args)
        _print(result)
        return int(code)
    except (ContractError, RuntimeFailure, ValueError, argparse.ArgumentError) as exc:
        _print({"status": "invalid", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
