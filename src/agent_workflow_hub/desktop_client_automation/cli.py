from __future__ import annotations

import argparse
import importlib.metadata
import json
import platform
import struct
import sys
from pathlib import Path
from typing import Sequence

from .airtest_runtime import RuntimeFailure, _uia_windows, run_bundle
from .contracts import ContractError, load_request
from .cua_runtime import probe_cua_driver
from .exploration import capture_target, click_image, locate_image
from .readiness import evaluate_readiness
from .renderer import compile_bundle


HUB_ROOT = Path(__file__).resolve().parents[3]
AIRTEST_RUNTIME_PYTHON = (
    HUB_ROOT
    / "workspace"
    / "workflows"
    / "desktop-client-automation"
    / "runtime"
    / "Scripts"
    / "python.exe"
)
AIRTEST_LOCKED_VERSIONS = {
    "airtest": "1.4.3",
    "pywinauto": "0.6.3",
    "opencv-contrib-python": "4.6.0.66",
    "psutil": "7.2.2",
    "pywin32": "312",
}


def _version(distribution: str) -> str | None:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None


def _print(value: dict[str, object]) -> None:
    print(json.dumps(value, ensure_ascii=False))


def _image_target(args: argparse.Namespace) -> tuple[bool, str | None]:
    desktop = bool(getattr(args, "desktop", False))
    display_id = getattr(args, "display_id", None)
    if desktop and display_id is None:
        display_id = "primary"
    return desktop, display_id


def _airtest_runtime_status(
    platform_name: str,
    *,
    versions: dict[str, str | None] | None = None,
    executable: Path | None = None,
    expected_executable: Path = AIRTEST_RUNTIME_PYTHON,
    python_version: tuple[int, int] | None = None,
    pointer_bits: int | None = None,
) -> dict[str, object]:
    installed = (
        versions
        if versions is not None
        else {name: _version(name) for name in AIRTEST_LOCKED_VERSIONS}
    )
    actual_executable = (executable or Path(sys.executable)).resolve(strict=False)
    expected = expected_executable.resolve(strict=False)
    actual_python = python_version or (sys.version_info.major, sys.version_info.minor)
    bits = pointer_bits or (struct.calcsize("P") * 8)
    ready = (
        platform_name == "Windows"
        and actual_executable == expected
        and actual_python == (3, 11)
        and bits == 64
        and installed == AIRTEST_LOCKED_VERSIONS
    )
    return {
        "ready": ready,
        "versions": installed,
        "interpreter": str(actual_executable),
        "expected_interpreter": str(expected),
        "python_version": f"{actual_python[0]}.{actual_python[1]}",
        "pointer_bits": bits,
    }


def _doctor(args: argparse.Namespace) -> int:
    cua = probe_cua_driver()
    platform_name = platform.system()
    airtest = _airtest_runtime_status(platform_name)
    airtest_ready = bool(airtest["ready"])
    readiness = evaluate_readiness(
        mode=args.mode,
        os_name=platform_name,
        host=args.host,
        cua_probe=cua,
        cua_mcp=args.cua_mcp,
        native_computer_use=args.native_computer_use,
        airtest_ready=airtest_ready,
    )
    _print(
        {
            **readiness,
            "mode": args.mode,
            "host": args.host,
            "platform": platform_name,
            "cua_mcp": args.cua_mcp,
            "native_computer_use": args.native_computer_use,
            "cua": cua,
            "airtest": airtest,
            "note": (
                "MCP availability must come from the current agent tool inventory; "
                "a local binary does not prove the MCP surface is connected."
            ),
        }
    )
    return (
        0
        if readiness["status"] in {"ready", "ready-native-compatibility"}
        else 2
    )


def _compile(args: argparse.Namespace) -> int:
    request = load_request(Path(args.request))
    bundle = compile_bundle(request, Path(args.output_root))
    _print(
        {
            "status": "generated-unverified",
            "bundle": str(bundle),
            "decision": request.decision,
            "aggregate_effect": request.aggregate_effect,
        }
    )
    return 0


def _capture(args: argparse.Namespace) -> int:
    crop_values = (args.x, args.y, args.width, args.height)
    if any(value is not None for value in crop_values):
        if any(value is None for value in crop_values):
            raise ContractError("capture crop requires x, y, width, and height together")
        crop = tuple(int(value) for value in crop_values)
    else:
        crop = None
    desktop, display_id = _image_target(args)
    result = capture_target(
        output=Path(args.output),
        desktop=desktop,
        display_id=display_id,
        window_title_regex=args.window_title_regex,
        crop=crop,
        windows_factory=_uia_windows,
    )
    _print(result)
    return 0


def _replay(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle)
    if not bundle.is_absolute() or not bundle.is_dir():
        raise ContractError("bundle must be an existing absolute directory")
    platform_name = platform.system()
    if platform_name != "Windows":
        status = (
            "needs-replay-backend"
            if platform_name == "Darwin"
            else "unsupported-platform"
        )
        _print({"status": status, "platform": platform_name})
        return 2
    manifest_path = bundle / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid bundle manifest: {exc}") from exc
    return run_bundle(bundle)


def _locate_image(args: argparse.Namespace) -> int:
    desktop, display_id = _image_target(args)
    result = locate_image(
        desktop=desktop,
        display_id=display_id,
        window_title_regex=args.window_title_regex,
        template_path=Path(args.template),
        threshold=args.threshold,
        timeout_seconds=args.timeout_seconds,
    )
    _print(result)
    return 0 if result["status"] == "matched" else 1


def _click_image(args: argparse.Namespace) -> int:
    desktop, display_id = _image_target(args)
    result = click_image(
        desktop=desktop,
        display_id=display_id,
        window_title_regex=args.window_title_regex,
        template_path=Path(args.template),
        evidence_dir=Path(args.evidence_dir),
        action_id=args.action_id,
        threshold=args.threshold,
        timeout_seconds=args.timeout_seconds,
    )
    _print(result)
    return 0 if result["status"] == "clicked" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="desktop-client-automation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor")
    doctor.add_argument(
        "--host",
        choices=("codex", "openclaw", "claude-code", "hermes", "opencode"),
        default="codex",
    )
    doctor.add_argument(
        "--mode",
        choices=("explore", "replay", "full"),
        default="full",
    )
    doctor.add_argument(
        "--cua-mcp",
        choices=("available", "unavailable", "unknown"),
        default="unknown",
    )
    doctor.add_argument(
        "--native-computer-use",
        "--computer-use",
        dest="native_computer_use",
        choices=("available", "unavailable", "unknown"),
        default="unknown",
    )
    doctor.set_defaults(handler=_doctor)

    compile_parser = subparsers.add_parser("compile")
    compile_parser.add_argument("--request", required=True)
    compile_parser.add_argument("--output-root", required=True)
    compile_parser.set_defaults(handler=_compile)

    def add_image_target(command: argparse.ArgumentParser) -> None:
        target = command.add_mutually_exclusive_group(required=True)
        target.add_argument("--window-title-regex")
        target.add_argument("--desktop", action="store_true")
        command.add_argument("--display-id", choices=["primary"])

    capture = subparsers.add_parser("capture")
    add_image_target(capture)
    capture.add_argument("--output", required=True)
    capture.add_argument("--x", type=int)
    capture.add_argument("--y", type=int)
    capture.add_argument("--width", type=int)
    capture.add_argument("--height", type=int)
    capture.set_defaults(handler=_capture)

    locate = subparsers.add_parser("locate-image")
    add_image_target(locate)
    locate.add_argument("--template", required=True)
    locate.add_argument("--threshold", type=float, default=0.8)
    locate.add_argument("--timeout-seconds", type=float, default=10.0)
    locate.set_defaults(handler=_locate_image)

    click = subparsers.add_parser("click-image")
    add_image_target(click)
    click.add_argument("--template", required=True)
    click.add_argument("--evidence-dir", required=True)
    click.add_argument("--action-id", required=True)
    click.add_argument("--threshold", type=float, default=0.8)
    click.add_argument("--timeout-seconds", type=float, default=10.0)
    click.set_defaults(handler=_click_image)

    replay = subparsers.add_parser("replay")
    replay.add_argument("--bundle", required=True)
    replay.set_defaults(handler=_replay)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ContractError, RuntimeFailure, ValueError) as exc:
        _print({"status": "invalid", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
