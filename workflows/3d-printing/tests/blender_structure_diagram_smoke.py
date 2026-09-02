from __future__ import annotations

import argparse
import hashlib
import os
import struct
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace


SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from structure_diagram import render_structure_diagram  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(__file__).parents[1] / "outputs" / "structure-diagram-smoke",
    )
    payload = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    return parser.parse_args(payload)


def _box(name, center, dimensions):
    import bpy

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=center)
    obj = bpy.context.active_object
    obj.name = name
    obj.dimensions = dimensions
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return obj


def _mesh_digest(obj):
    digest = hashlib.sha256()
    for vertex in obj.data.vertices:
        digest.update(struct.pack("<3d", *(float(value) for value in vertex.co)))
    digest.update(struct.pack("<16d", *(float(value) for row in obj.matrix_world for value in row)))
    return digest.hexdigest()


def main() -> int:
    import bpy

    args = _parse_args()
    args.work_dir = args.work_dir.resolve()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    pieces = {
        "body": _box("body", (0.0, 0.0, 0.0), (8.0, 5.0, 12.0)),
        "head": _box("head", (0.0, 0.0, 9.0), (7.0, 5.0, 6.0)),
    }
    connectors = tuple(
        SimpleNamespace(
            id=connector_id,
            male_piece=piece_id,
            female_piece="body",
        )
        for connector_id, piece_id in (
            ("neck-a", "head"),
            ("neck-b", "head"),
        )
    )
    before = {piece_id: _mesh_digest(obj) for piece_id, obj in pieces.items()}
    destination = args.work_dir / "structure-diagram.png"

    evidence = render_structure_diagram(pieces, connectors, destination)

    after = {piece_id: _mesh_digest(obj) for piece_id, obj in pieces.items()}
    if before != after:
        raise AssertionError("structure diagram changed source piece geometry")
    if destination.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("structure diagram is not PNG")
    if destination.stat().st_size <= 10_000:
        raise AssertionError("structure diagram is unexpectedly small")
    if (evidence["width_px"], evidence["height_px"]) != (1800, 1400):
        raise AssertionError(evidence)
    if set(evidence["piece_ids"]) != set(pieces):
        raise AssertionError("piece evidence mismatch")
    if set(evidence["connector_ids"]) != {item.id for item in connectors}:
        raise AssertionError("connector evidence mismatch")
    layout = evidence["debug_layout"]
    half_width = layout["camera_ortho_scale"] / 2.0
    half_height = half_width / (1800.0 / 1400.0)
    center_x, _center_y, center_z = layout["camera_center"]
    if not (
        center_x - half_width <= layout["content_min"][0]
        and layout["content_max"][0] <= center_x + half_width
        and center_z - half_height <= layout["content_min"][2]
        and layout["content_max"][2] <= center_z + half_height
    ):
        raise AssertionError("camera must contain all diagram content")
    print("STRUCTURE_DIAGRAM_SMOKE_OK")
    return 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    raise SystemExit(exit_code)
