from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any

from palette import PRESET_SIZES, load_palette
from runs import accept_candidate, create_candidate, load_accepted


PALETTE_PATH = Path(__file__).parents[1] / "data" / "a-m-v1.json"
MIN_PILLOW = (12, 3, 0)


class WorkflowError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


class WorkflowArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise WorkflowError("invalid-input", message)


def _json_print(payload: dict[str, Any], *, stream: Any = sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), file=stream)


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?", value)
    if match is None:
        return None
    return tuple(int(part or 0) for part in match.groups())


def _require_pillow() -> str:
    try:
        import PIL
    except ImportError as error:
        raise WorkflowError(
            "needs-dependency",
            "Pillow >=12.3.0 is required; follow capabilities/python/pillow/CAPABILITY.md",
        ) from error
    installed = getattr(PIL, "__version__", "")
    parsed = _version_tuple(installed)
    if parsed is None or parsed < MIN_PILLOW:
        raise WorkflowError(
            "needs-dependency",
            f"Pillow >=12.3.0 is required; found {installed or 'unknown'}; "
            "follow capabilities/python/pillow/CAPABILITY.md",
        )
    return installed


def _existing_absolute_directory(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise WorkflowError("invalid-input", "--hub-root must be an existing absolute directory")
    return path.resolve(strict=True)


def _existing_absolute_file(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise WorkflowError("invalid-input", "--input must be an existing absolute file")
    return path.resolve(strict=True)


def _candidate_summary(candidate: Any, palette: Any) -> dict[str, Any]:
    usage = [
        {"code": code, "rgb": "#" + "".join(f"{part:02X}" for part in palette.colours[code].rgb), "count": count}
        for code, count in sorted(candidate.counts.items(), key=lambda item: palette.colours[item[0]].ordinal)
    ]
    return {
        "palette_id": candidate.palette_id,
        "palette_digest": candidate.palette_digest,
        "preset": candidate.preset_size,
        "grid": {"columns": candidate.columns, "rows": candidate.rows, "board": candidate.board},
        "background_code": candidate.background_code,
        "empty_cells": candidate.empty_cells,
        "total_beads": sum(candidate.counts.values()),
        "colour_usage": usage,
    }


def _plan(arguments: argparse.Namespace) -> dict[str, Any]:
    pillow_version = _require_pillow()
    hub_root = _existing_absolute_directory(arguments.hub_root)
    source = _existing_absolute_file(arguments.input)
    palette = load_palette(PALETTE_PATH)
    from pipeline import build_candidate

    candidate = build_candidate(
        source,
        palette,
        preset_size=arguments.preset,
        columns=arguments.columns,
        rows=arguments.rows,
        board=arguments.board,
        background_code=arguments.background_code,
    )
    candidate_path = create_candidate(hub_root, arguments.run_id, candidate)
    return {
        "status": "candidate-created",
        "run_id": arguments.run_id,
        "candidate_path": str(candidate_path),
        "pillow_version": pillow_version,
        **_candidate_summary(candidate, palette),
        "next_action": "Show this candidate to the user and obtain explicit confirmation before accept.",
    }


def _accept(arguments: argparse.Namespace) -> dict[str, Any]:
    hub_root = _existing_absolute_directory(arguments.hub_root)
    try:
        candidate = accept_candidate(hub_root, arguments.run_id)
    except FileNotFoundError as error:
        raise WorkflowError("invalid-input", "candidate run does not exist") from error
    return {
        "status": "accepted",
        "run_id": arguments.run_id,
        "next_action": "Render the accepted matrix to produce pattern.png.",
        "total_beads": sum(candidate.counts.values()),
    }


def _render(arguments: argparse.Namespace) -> dict[str, Any]:
    _require_pillow()
    hub_root = _existing_absolute_directory(arguments.hub_root)
    try:
        accepted = load_accepted(hub_root, arguments.run_id)
    except FileNotFoundError as error:
        raise WorkflowError(
            "needs-user-confirmation",
            "no accepted pattern exists; obtain explicit user confirmation and run accept first",
        ) from error
    palette = load_palette(PALETTE_PATH)
    from render import render_final_png

    output = render_final_png(accepted, palette=palette, hub_root=hub_root, run_id=arguments.run_id)
    return {"status": "rendered", "run_id": arguments.run_id, "output": str(output)}


def _parser() -> argparse.ArgumentParser:
    parser = WorkflowArgumentParser(description="Create reviewed bead-pattern PNG charts.")
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan", help="create a private candidate only")
    plan.add_argument("--hub-root", required=True)
    plan.add_argument("--input", required=True)
    plan.add_argument("--run-id", required=True)
    plan.add_argument("--preset", type=int, choices=PRESET_SIZES, default=144)
    plan.add_argument("--columns", type=int, required=True)
    plan.add_argument("--rows", type=int, required=True)
    plan.add_argument("--board", choices=("52", "78", "104"))
    plan.add_argument("--background-code")
    plan.set_defaults(handler=_plan)

    accept = commands.add_parser("accept", help="freeze a candidate after user confirmation")
    accept.add_argument("--hub-root", required=True)
    accept.add_argument("--run-id", required=True)
    accept.set_defaults(handler=_accept)

    render = commands.add_parser("render", help="render only an accepted frozen matrix")
    render.add_argument("--hub-root", required=True)
    render.add_argument("--run-id", required=True)
    render.set_defaults(handler=_render)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        _json_print(arguments.handler(arguments))
        return 0
    except WorkflowError as error:
        _json_print({"status": "error", "category": error.category, "message": error.message}, stream=sys.stderr)
        return 2
    except FileExistsError as error:
        _json_print({"status": "error", "category": "output-exists", "message": str(error)}, stream=sys.stderr)
        return 3
    except (ValueError, FileNotFoundError) as error:
        _json_print({"status": "error", "category": "invalid-input", "message": str(error)}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
