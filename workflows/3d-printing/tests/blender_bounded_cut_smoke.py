from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path


SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bounded_cut import (  # noqa: E402
    ComponentAssignmentRequired,
    bounded_split,
    mesh_component_count,
    object_volume,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=Path(__file__).parents[1] / "outputs" / "bounded-cut-smoke",
    )
    payload = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    return parser.parse_args(payload)


def _joined_disconnected_spheres(name: str):
    import bpy

    objects = []
    for location in ((0.0, 0.0, 0.0), (0.0, 5.0, 5.0)):
        bpy.ops.mesh.primitive_ico_sphere_add(
            subdivisions=3,
            radius=3.0,
            location=location,
        )
        objects.append(bpy.context.active_object)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.join()
    source = bpy.context.active_object
    source.name = name
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    return source


def _joined_close_boxes(name: str):
    import bpy

    objects = []
    for location, dimensions in (
        ((0.0, 0.0, 0.0), (4.0, 2.0, 2.0)),
        ((1.0, 2.0001, 0.0), (2.0, 2.0, 2.0)),
    ):
        bpy.ops.mesh.primitive_cube_add(size=1.0, location=location)
        obj = bpy.context.active_object
        obj.dimensions = dimensions
        bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
        objects.append(obj)
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.object.join()
    source = bpy.context.active_object
    source.name = name
    return source


def _overlap(first_min, first_max, second_min, second_max):
    return [
        min(first_max[index], second_max[index])
        - max(first_min[index], second_min[index])
        for index in range(3)
    ]


def main() -> int:
    import bpy

    args = _parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.read_factory_settings(use_empty=True)

    source = _joined_disconnected_spheres("overlap-aabb-source")
    negative, positive, record = bounded_split(
        source,
        cut_id="overlap-aabb",
        point_mm=(0.0, 0.0, 0.0),
        normal=(1.0, 0.0, 0.0),
        target_side="positive",
        seed_points={"pin": (0.25, 0.0, 0.0)},
        diagnostic_path=args.work_dir / "unexpected-ambiguity.png",
    )

    if record["candidate_count"] != 2:
        raise AssertionError(record)
    candidates = record["candidates"]
    if not all(
        value > 0
        for value in _overlap(
            candidates[0]["bbox_min"],
            candidates[0]["bbox_max"],
            candidates[1]["bbox_min"],
            candidates[1]["bbox_max"],
        )
    ):
        raise AssertionError("fixture AABBs must overlap on all three axes")
    if mesh_component_count(positive) != 1:
        raise AssertionError("target result must contain one connected component")
    negative_components = mesh_component_count(negative)
    if negative_components != 2:
        raise AssertionError(
            f"remote shell must remain with the remainder: {negative_components}"
        )
    if object_volume(positive) <= 40.0 or object_volume(negative) <= 80.0:
        raise AssertionError("bounded split lost fixture volume")

    close_source = _joined_close_boxes("close-shell-source")
    close_negative, close_positive, close_record = bounded_split(
        close_source,
        cut_id="close-shell",
        point_mm=(0.0, 0.0, 0.0),
        normal=(1.0, 0.0, 0.0),
        target_side="positive",
        seed_points={"pin": (0.25, 0.0, 0.0)},
        diagnostic_path=args.work_dir / "unexpected-close-shell-ambiguity.png",
    )
    if close_record["candidate_count"] != 2:
        raise AssertionError(close_record)
    if mesh_component_count(close_positive) != 1:
        raise AssertionError("nearby shell must not leak into the selected target")
    if mesh_component_count(close_negative) != 2:
        raise AssertionError("nearby shell must remain intact in the remainder")
    if len(close_record["returned_components"]) != 1:
        raise AssertionError("nearby shell must be explicitly returned to the remainder")
    returned = close_record["returned_components"][0]
    if returned["candidate_index"] != 1 or returned["returned_to"] != "remainder":
        raise AssertionError("returned component assignment must remain auditable")

    ambiguous_source = _joined_disconnected_spheres("ambiguous-source")
    diagnostic = args.work_dir / "component-assignment-ambiguous.png"
    try:
        bounded_split(
            ambiguous_source,
            cut_id="ambiguous",
            point_mm=(0.0, 0.0, 0.0),
            normal=(1.0, 0.0, 0.0),
            target_side="positive",
            seed_points={"a": (0.25, 0.0, 0.0), "b": (0.25, 5.0, 5.0)},
            diagnostic_path=diagnostic,
        )
    except ComponentAssignmentRequired as exc:
        if exc.status != "needs_user_component_assignment":
            raise AssertionError(exc.status)
        if tuple(item.index for item in exc.candidates) != (0, 1):
            raise AssertionError("diagnostic candidates must remain numbered")
    else:
        raise AssertionError("ambiguous component assignment must stop")
    if not diagnostic.is_file() or diagnostic.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("ambiguous assignment must produce a PNG diagnostic")

    print("BOUNDED_CUT_SMOKE_OK")
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
