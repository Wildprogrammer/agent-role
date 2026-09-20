"""Thin CLI adapter for the pinned Dufs executable."""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from .assets import AssetManifestError, DufsAsset, load_asset_manifest, select_asset
from .runtime import (
    RuntimeInstallError,
    inspect_runtime,
    install_runtime,
    installation_candidate,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DufsCliError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


def _absolute_path(value: str) -> Path:
    if "\0" in value:
        raise argparse.ArgumentTypeError("path must not contain NUL")
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _sha256(value: str) -> str:
    if _SHA256.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "digest must be 64 lowercase hexadecimal characters"
        )
    return value


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _bind_address(value: str) -> str:
    if not value or "\0" in value:
        raise argparse.ArgumentTypeError("bind address must be non-empty and contain no NUL")
    return value


def _existing_directory(value: str) -> Path:
    path = _absolute_path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError("directory must exist and be a directory")
    return path


def _add_hub_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hub-root", required=True, type=_absolute_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lan-file-sharing",
        description=(
            "Run pinned Dufs, a limited Python http.server fallback, or an "
            "optional user-managed ngrok tunnel."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser(
        "doctor",
        help="inspect the pinned runtime without changing the host",
    )
    _add_hub_root(doctor)

    install = commands.add_parser(
        "install",
        help="install the confirmed official Dufs artifact",
    )
    _add_hub_root(install)
    install.add_argument("--confirmed-asset-sha256", required=True, type=_sha256)

    run = commands.add_parser(
        "run",
        help="run Dufs in the foreground with native arguments after --",
    )
    _add_hub_root(run)
    run.add_argument(
        "dufs_args",
        nargs=argparse.REMAINDER,
        help="arguments passed unchanged to Dufs; use -- before them",
    )

    basic = commands.add_parser(
        "serve-basic",
        help="run Python http.server as a limited foreground fallback",
    )
    basic.add_argument("--directory", required=True, type=_existing_directory)
    basic.add_argument("--bind", required=True, type=_bind_address)
    basic.add_argument("--port", required=True, type=_port)

    tunnel = commands.add_parser(
        "tunnel",
        help="run a user-managed ngrok HTTP tunnel in the foreground",
    )
    tunnel.add_argument("--port", required=True, type=_port)
    tunnel.add_argument(
        "ngrok_args",
        nargs=argparse.REMAINDER,
        help="arguments passed unchanged after 'ngrok http <port>'; use -- first",
    )
    return parser


def _manifest_path(hub_root: Path) -> Path:
    return hub_root / "capabilities" / "cli" / "dufs" / "assets-v0.46.0.json"


def _asset(hub_root: Path, *, system: str, machine: str) -> DufsAsset:
    return select_asset(
        load_asset_manifest(_manifest_path(hub_root)),
        system=system,
        machine=machine,
    )


def _write_json(value: dict[str, object], *, stream: object) -> None:
    stream.write(  # type: ignore[attr-defined]
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    )


def _native_args(value: Sequence[str]) -> list[str]:
    arguments = list(value)
    if arguments[:1] == ["--"]:
        arguments.pop(0)
    if any("\0" in item for item in arguments):
        raise DufsCliError("invalid_arguments", "Dufs arguments must not contain NUL")
    return arguments


def _ngrok_args(value: Sequence[str]) -> list[str]:
    arguments = _native_args(value)
    if any(
        item == "--authtoken" or item.startswith("--authtoken=")
        for item in arguments
    ):
        raise DufsCliError(
            "invalid_arguments",
            "do not pass an ngrok authtoken in argv; configure it outside the workflow",
        )
    return arguments


def _process_exit_code(
    argv: Sequence[str],
    *,
    process_runner: Callable[..., object],
) -> int:
    completed = process_runner(list(argv), check=False, shell=False)
    returncode = getattr(completed, "returncode", None)
    if type(returncode) is not int:
        raise DufsCliError("run_failed", "process runner returned no exit code")
    return returncode


def main(
    argv: list[str] | None = None,
    *,
    process_runner: Callable[..., object] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    system: str | None = None,
    machine: str | None = None,
) -> int:
    arguments = build_parser().parse_args(argv)
    selected_system = system or platform.system()
    selected_machine = machine or platform.machine()
    try:
        if arguments.command == "serve-basic":
            return _process_exit_code(
                [
                    sys.executable,
                    "-m",
                    "http.server",
                    str(arguments.port),
                    "--bind",
                    arguments.bind,
                    "--directory",
                    str(arguments.directory),
                ],
                process_runner=process_runner,
            )

        if arguments.command == "tunnel":
            executable = which("ngrok")
            if executable is None:
                raise DufsCliError(
                    "needs_dependency",
                    "ngrok is not available on PATH; install and authenticate it first",
                )
            return _process_exit_code(
                [
                    executable,
                    "http",
                    str(arguments.port),
                    *_ngrok_args(arguments.ngrok_args),
                ],
                process_runner=process_runner,
            )

        asset = _asset(
            arguments.hub_root,
            system=selected_system,
            machine=selected_machine,
        )
        if arguments.command == "doctor":
            inspection = inspect_runtime(arguments.hub_root, asset)
            _write_json(
                {
                    "status": inspection.status,
                    "runtime": inspection.to_mapping(),
                    "installation": installation_candidate(
                        arguments.hub_root,
                        system=selected_system,
                        machine=selected_machine,
                    ),
                },
                stream=sys.stdout,
            )
            return 0

        if arguments.command == "install":
            inspection = install_runtime(
                arguments.hub_root,
                asset,
                confirmed_asset_sha256=arguments.confirmed_asset_sha256,
            )
            _write_json(
                {"status": inspection.status, "runtime": inspection.to_mapping()},
                stream=sys.stdout,
            )
            return 0

        inspection = inspect_runtime(arguments.hub_root, asset)
        if inspection.status != "ready":
            raise DufsCliError(
                "needs_dependency",
                "pinned Dufs 0.46.0 is not ready; run doctor and install first",
            )
        native = _native_args(arguments.dufs_args)
        return _process_exit_code(
            [inspection.executable, *native],
            process_runner=process_runner,
        )
    except KeyboardInterrupt:
        return 130
    except DufsCliError as exc:
        category = exc.category
        message = str(exc)
    except AssetManifestError as exc:
        category = "unsupported_platform"
        message = str(exc)
    except RuntimeInstallError as exc:
        category = "runtime_error"
        message = str(exc)
    except OSError as exc:
        category = "run_failed"
        message = str(exc)
    _write_json(
        {"status": "error", "category": category, "message": message},
        stream=sys.stderr,
    )
    return 3 if category in {"needs_dependency", "unsupported_platform"} else 4


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ("DufsCliError", "build_parser", "main")
