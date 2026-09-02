from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import traceback
from pathlib import Path


SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from headless_cut import _apply_connector, _export_standard_3mf, _mesh_stats
from split_plan import ConnectorInstruction


def _box(name: str, location: tuple[float, float, float]):
    import bpy

    bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = (20.0, 20.0, 10.0)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return obj


def main() -> int:
    import bpy

    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", required=True, type=Path)
    payload = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = parser.parse_args(payload)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    pieces = {
        "body": _box("body", (0.0, 0.0, -5.0)),
        "head": _box("head", (0.0, 0.0, 5.0)),
    }
    connector = ConnectorInstruction(
        id="neck-a",
        cut_id="neck",
        type="integrated-keyed-pin",
        male_piece="head",
        female_piece="body",
        center_mm=(0.0, 0.0, 0.0),
        axis=(0.0, 0.0, -1.0),
        key_direction=(1.0, 0.0, 0.0),
        width_mm=6.0,
        height_mm=4.5,
        corner_radius_mm=1.0,
        engagement_mm=7.0,
        root_fillet_mm=0.8,
        tip_chamfer_mm=0.6,
        clearance_per_side_mm=0.25,
        socket_bottom_clearance_mm=0.5,
        minimum_wall_mm=1.2,
        minimum_edge_margin_mm=1.2,
    )
    record = _apply_connector(pieces, connector)
    result = {
        "record": record,
        "body": _mesh_stats(pieces["body"]),
        "head": _mesh_stats(pieces["head"]),
    }
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
    result["standard_3mf"] = dataclasses.asdict(
        _export_standard_3mf(
            pieces,
            args.work_dir / "connector-smoke.3mf",
            {"body": identity, "head": identity},
        )
    )
    if result["body"]["boundary_edges"] or result["head"]["boundary_edges"]:
        raise SystemExit("connector smoke produced an open mesh")
    if result["body"]["non_manifold_edges"] or result["head"]["non_manifold_edges"]:
        raise SystemExit("connector smoke produced a non-manifold mesh")
    (args.work_dir / "connector-smoke-evidence.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
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
