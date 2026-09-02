from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import traceback
from array import array
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any


SCRIPT_VERSION = "2.3.0"
VALIDATION_LEVELS = ("light", "full")
LIGHT_DEFERRED_CHECKS = (
    "full_self_intersection",
    "exhaustive_wall_thickness",
    "printability_and_slicer_import",
)
SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_IMPORT_FILES = (
    "bounded_cut.py",
    "component_selection.py",
    "connector_geometry.py",
    "split_plan.py",
    "standard_3mf.py",
    "structure_diagram.py",
    "three_mf_import.py",
)
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from connector_geometry import (
    ConnectorEvidenceError,
    connector_frame,
    nominal_pin_volume,
    socket_dimensions,
    validate_connector_evidence,
)
from bounded_cut import (
    ComponentAssignmentRequired,
    analyze_components,
    bounded_split,
    mesh_component_count,
)
from split_plan import CutInstruction, ConnectorInstruction, SplitPlan, load_split_plan
from standard_3mf import (
    IDENTITY_TRANSFORM,
    MeshPayload,
    Standard3MFVerification,
    verify_standard_3mf,
    write_standard_3mf,
)
from structure_diagram import render_structure_diagram
from three_mf_import import ThreeMFImportError, load_3mf_mesh


class CutEvidenceError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def local_script_hashes() -> dict[str, str]:
    return {
        filename: sha256_file(SCRIPT_DIR / filename)
        for filename in LOCAL_IMPORT_FILES
    }


def build_blender_cut_command(
    blender_path: Path,
    script_path: Path,
    source_path: Path,
    plan_path: Path,
    output_dir: Path,
) -> tuple[str, ...]:
    return (
        str(blender_path.resolve()),
        "--background",
        "--python",
        str(script_path.resolve()),
        "--",
        "--source",
        str(source_path.resolve()),
        "--plan",
        str(plan_path.resolve()),
        "--output-dir",
        str(output_dir.resolve()),
    )


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CutEvidenceError(f"{label} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise CutEvidenceError(f"{label} must be a SHA-256 digest") from exc
    return value.lower()


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CutEvidenceError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise CutEvidenceError(f"{label} must be finite")
    return result


def validate_cut_evidence(
    evidence: Mapping[str, Any],
    plan: SplitPlan,
    *,
    source_sha256: str,
    script_sha256: str,
) -> dict[str, Any]:
    validation_level = _value(evidence, "validation_level", "full")
    if validation_level not in VALIDATION_LEVELS:
        raise CutEvidenceError("validation_level must be light or full")
    if _value(evidence, "source_model_sha256") != source_sha256:
        raise CutEvidenceError("source hash mismatch")
    if _value(evidence, "script_sha256") != script_sha256:
        raise CutEvidenceError("script hash mismatch")
    imported_hashes = _value(evidence, "imported_script_sha256")
    if (
        not isinstance(imported_hashes, Mapping)
        or dict(imported_hashes) != local_script_hashes()
    ):
        raise CutEvidenceError("imported script hash mismatch")
    expected_cut_ids = [cut.id for cut in plan.cuts]
    if list(_value(evidence, "cut_ids", [])) != expected_cut_ids:
        raise CutEvidenceError("cut sequence mismatch")

    raw_pieces = _value(evidence, "pieces")
    if not isinstance(raw_pieces, Sequence) or isinstance(raw_pieces, (str, bytes)):
        raise CutEvidenceError("pieces must be a list")
    if len(raw_pieces) != len(plan.leaf_piece_ids):
        raise CutEvidenceError("piece count mismatch")
    seen: set[str] = set()
    piece_hashes: dict[str, str] = {}
    volume_sum = 0.0
    for index, raw_piece in enumerate(raw_pieces):
        piece = _value(raw_piece, "piece_id")
        if piece in seen or piece not in plan.leaf_piece_ids:
            raise CutEvidenceError(f"piece mapping mismatch at index {index}")
        seen.add(piece)
        piece_hashes[piece] = _require_digest(
            _value(raw_piece, "sha256"), f"piece hash for {piece}"
        )
        stats = _value(raw_piece, "stats")
        if not isinstance(stats, Mapping):
            raise CutEvidenceError(f"stats missing for {piece}")
        if _value(stats, "connected_components") != 1:
            raise CutEvidenceError(f"connected components failed for {piece}")
        component_records = _value(stats, "components")
        if (
            not isinstance(component_records, Sequence)
            or isinstance(component_records, (str, bytes))
            or len(component_records) != 1
        ):
            raise CutEvidenceError(f"connected components evidence failed for {piece}")
        if _value(stats, "boundary_edges") != 0:
            raise CutEvidenceError(f"boundary edges found for {piece}")
        if _value(stats, "non_manifold_edges") != 0:
            raise CutEvidenceError(f"non-manifold edges found for {piece}")
        intersections = _value(stats, "self_intersections")
        if validation_level == "full":
            if intersections == "not_evaluated" or intersections != 0:
                raise CutEvidenceError(
                    f"self-intersections not evaluated for {piece}"
                )
        elif intersections not in (0, "not_evaluated"):
            raise CutEvidenceError(f"self-intersections found for {piece}")
        wall = _value(stats, "minimum_wall_mm")
        if validation_level == "full":
            if wall == "not_evaluated":
                raise CutEvidenceError(f"minimum wall not evaluated for {piece}")
            if _number(wall, f"minimum wall for {piece}") <= 0:
                raise CutEvidenceError(f"minimum wall failed for {piece}")
        elif wall != "not_evaluated" and _number(
            wall, f"minimum wall for {piece}"
        ) <= 0:
            raise CutEvidenceError(f"minimum wall failed for {piece}")
        volume_sum += _number(_value(stats, "volume_mm3"), f"volume for {piece}")
    if seen != set(plan.leaf_piece_ids):
        raise CutEvidenceError("piece mapping mismatch")

    source_volume = _number(
        _value(evidence, "source_volume_mm3"), "source volume"
    )
    reported_sum = _number(_value(evidence, "volume_sum_mm3", volume_sum), "volume")
    if source_volume <= 0:
        raise CutEvidenceError("source volume must be positive")
    connector_count = 0
    expected_volume_delta = 0.0
    raw_connector_evidence = _value(evidence, "connector_evidence")
    if plan.connectors:
        if not isinstance(raw_connector_evidence, Mapping):
            raise CutEvidenceError("connector evidence missing")
        try:
            connector_summary = validate_connector_evidence(
                raw_connector_evidence, plan
            )
        except ConnectorEvidenceError as exc:
            raise CutEvidenceError(str(exc)) from exc
        connector_count = connector_summary["connector_count"]
        expected_volume_delta = connector_summary[
            "measured_net_volume_delta_mm3"
        ]
    elif raw_connector_evidence not in (None, {}):
        raise CutEvidenceError("connector evidence is not allowed for this plan")

    expected_volume = source_volume + expected_volume_delta
    relative_error = abs(reported_sum - expected_volume) / source_volume
    if relative_error > plan.volume_tolerance_ratio:
        raise CutEvidenceError("volume tolerance exceeded")
    if abs(reported_sum - volume_sum) / source_volume > 1e-6:
        raise CutEvidenceError("reported volume sum does not match piece volumes")

    source_path = _value(evidence, "source_model")
    if source_path:
        source_file = Path(
            os.path.expanduser(os.path.expandvars(str(source_path)))
        )
        if not source_file.is_file() or sha256_file(source_file) != source_sha256:
            raise CutEvidenceError("source file hash mismatch")
    for raw_piece in raw_pieces:
        piece_path = _value(raw_piece, "path")
        if piece_path:
            piece_file = Path(str(piece_path))
            piece_id = _value(raw_piece, "piece_id")
            expected_hash = piece_hashes[piece_id]
            if not piece_file.is_file() or sha256_file(piece_file) != expected_hash:
                raise CutEvidenceError(f"piece file hash mismatch for {piece_id}")

    validation = _value(evidence, "validation", {})
    for flag in ("source_unchanged", "piece_count", "all_piece_files_nonempty"):
        if _value(validation, flag) is not True:
            raise CutEvidenceError(f"validation flag failed: {flag}")
    return {
        "validation_level": validation_level,
        "deferred_checks": (
            list(LIGHT_DEFERRED_CHECKS)
            if validation_level == "light"
            else []
        ),
        "piece_count": len(seen),
        "piece_hashes": piece_hashes,
        "connector_count": connector_count,
        "expected_volume_delta_mm3": expected_volume_delta,
        "volume_relative_error": relative_error,
    }


def _mesh_stats(obj: Any, validation_level: str = "full") -> dict[str, Any]:
    import bmesh

    if validation_level not in VALIDATION_LEVELS:
        raise CutEvidenceError("validation_level must be light or full")

    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    mesh.normal_update()
    volume = abs(mesh.calc_volume(signed=True))
    if validation_level == "full":
        from mathutils.bvhtree import BVHTree

        bvh = BVHTree.FromBMesh(mesh)
        face_vertices = {
            face.index: {vertex.index for vertex in face.verts}
            for face in mesh.faces
        }
        overlap_pairs = bvh.overlap(bvh)
        self_intersections: int | str = sum(
            1
            for first, second in overlap_pairs
            if first != second
            and face_vertices.get(first, set()).isdisjoint(
                face_vertices.get(second, set())
            )
        ) // 2

        wall_distances: list[float] = []
        for face in mesh.faces:
            center = face.calc_center_median()
            normal = face.normal.normalized()
            epsilon = 1e-5
            hit_location, _hit_normal, hit_index, distance = bvh.ray_cast(
                center - normal * epsilon,
                -normal,
            )
            if (
                hit_location is not None
                and hit_index != face.index
                and distance > epsilon
            ):
                wall_distances.append(float(distance + epsilon))
        minimum_wall: float | str = (
            min(wall_distances) if wall_distances else "not_evaluated"
        )
        component_records = analyze_components(
            obj,
            seed_points={},
            reference_point=(0.0, 0.0, 0.0),
        )
        components = [item.to_dict() for item in component_records]
        component_count = len(component_records)
    else:
        self_intersections = "not_evaluated"
        minimum_wall = "not_evaluated"
        component_count = mesh_component_count(obj)
        components = [
            {
                "index": index,
                "face_count": len(mesh.faces) if component_count == 1 else None,
                "volume_mm3": volume if component_count == 1 else None,
            }
            for index in range(component_count)
        ]
    result = {
        "vertices": len(mesh.verts),
        "edges": len(mesh.edges),
        "polygons": len(mesh.faces),
        "boundary_edges": sum(edge.is_boundary for edge in mesh.edges),
        "non_manifold_edges": sum(not edge.is_manifold for edge in mesh.edges),
        "volume_mm3": volume,
        "dimensions_mm": [float(value) for value in obj.dimensions],
        "self_intersections": self_intersections,
        "minimum_wall_mm": minimum_wall,
        "connected_components": component_count,
        "components": components,
    }
    mesh.free()
    return result


def _cut_piece(
    source_obj: Any,
    piece_id: str,
    *,
    point_mm: tuple[float, float, float],
    normal: tuple[float, float, float],
    keep_positive: bool,
) -> Any:
    import bmesh
    import bpy
    from mathutils import Vector

    mesh = bpy.data.meshes.new(piece_id)
    piece = bpy.data.objects.new(piece_id, mesh)
    bpy.context.collection.objects.link(piece)
    bmesh_data = bmesh.new()
    bmesh_data.from_mesh(source_obj.data)
    bmesh.ops.bisect_plane(
        bmesh_data,
        geom=list(bmesh_data.verts) + list(bmesh_data.edges) + list(bmesh_data.faces),
        plane_co=Vector(point_mm),
        plane_no=Vector(normal),
        clear_inner=keep_positive,
        clear_outer=not keep_positive,
        dist=0.000001,
    )
    boundary_edges = [edge for edge in bmesh_data.edges if edge.is_boundary]
    if boundary_edges:
        bmesh.ops.triangle_fill(
            bmesh_data,
            edges=boundary_edges,
            normal=Vector(normal),
        )
    bmesh.ops.recalc_face_normals(bmesh_data, faces=list(bmesh_data.faces))
    bmesh_data.to_mesh(mesh)
    mesh.update()
    bmesh_data.free()
    return piece


def _object_volume(obj: Any) -> float:
    import bmesh

    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    mesh.normal_update()
    result = abs(float(mesh.calc_volume(signed=True)))
    mesh.free()
    return result


def _piece_descends_from(plan: SplitPlan, piece_id: str, ancestor: str) -> bool:
    parents = {
        output: cut.input_piece
        for cut in plan.cuts
        for output in (cut.negative_piece, cut.positive_piece)
    }
    current = piece_id
    while True:
        if current == ancestor:
            return True
        if current not in parents:
            return False
        current = parents[current]


def _cut_target_side(plan: SplitPlan, cut: CutInstruction) -> str:
    connectors = [item for item in plan.connectors if item.cut_id == cut.id]
    if not connectors:
        return "positive"
    sides = set()
    for connector in connectors:
        if _piece_descends_from(plan, connector.male_piece, cut.positive_piece):
            sides.add("positive")
        elif _piece_descends_from(plan, connector.male_piece, cut.negative_piece):
            sides.add("negative")
        else:
            raise CutEvidenceError(
                f"male connector branch is not produced by cut {cut.id}"
            )
    if len(sides) != 1:
        raise CutEvidenceError(f"male connectors disagree for cut {cut.id}")
    return sides.pop()


def _connector_seed_points(
    plan: SplitPlan,
    cut: CutInstruction,
) -> dict[str, tuple[float, float, float]]:
    seeds: dict[str, tuple[float, float, float]] = {}
    for connector in plan.connectors:
        if connector.cut_id != cut.id:
            continue
        length = math.sqrt(sum(value * value for value in connector.axis))
        if length <= 1e-12:
            raise CutEvidenceError(f"connector axis is zero for {connector.id}")
        inward = max(0.05, connector.root_fillet_mm / 2.0)
        seeds[connector.id] = tuple(
            round(
                connector.center_mm[index]
                - connector.axis[index] / length * inward,
                12,
            )
            for index in range(3)
        )
    return seeds


def _rounded_rectangle_points(
    width: float, height: float, radius: float, *, corner_segments: int = 4
) -> list[tuple[float, float]]:
    half_width = width / 2.0
    half_height = height / 2.0
    radius = min(radius, half_width, half_height)
    centers = (
        (half_width - radius, half_height - radius, 0.0),
        (-half_width + radius, half_height - radius, math.pi / 2.0),
        (-half_width + radius, -half_height + radius, math.pi),
        (half_width - radius, -half_height + radius, 3.0 * math.pi / 2.0),
    )
    points: list[tuple[float, float]] = []
    for center_x, center_y, start_angle in centers:
        for step in range(corner_segments + 1):
            angle = start_angle + (math.pi / 2.0) * step / corner_segments
            point = (
                center_x + radius * math.cos(angle),
                center_y + radius * math.sin(angle),
            )
            if not points or any(
                not math.isclose(a, b, abs_tol=1e-9)
                for a, b in zip(points[-1], point)
            ):
                points.append(point)
    if len(points) > 1 and all(
        math.isclose(a, b, abs_tol=1e-9) for a, b in zip(points[0], points[-1])
    ):
        points.pop()
    return points


def _profile_solid(
    name: str,
    connector: ConnectorInstruction,
    rings: Sequence[tuple[float, float, float, float]],
) -> Any:
    import bpy
    from mathutils import Vector

    frame = connector_frame(connector.axis, connector.key_direction)
    center = Vector(connector.center_mm)
    axis = Vector(frame.axis)
    key = Vector(frame.key)
    side = Vector(frame.side)
    ring_points = [
        _rounded_rectangle_points(width, height, radius)
        for _distance, width, height, radius in rings
    ]
    point_count = len(ring_points[0])
    if any(len(points) != point_count for points in ring_points):
        raise CutEvidenceError("connector profile rings must have equal resolution")

    vertices: list[tuple[float, float, float]] = []
    for (distance, _width, _height, _radius), points in zip(rings, ring_points):
        for x_value, y_value in points:
            point = center + axis * distance + key * x_value + side * y_value
            vertices.append(tuple(float(value) for value in point))

    faces: list[tuple[int, ...]] = []
    faces.append(tuple(reversed(range(point_count))))
    for ring_index in range(len(rings) - 1):
        first = ring_index * point_count
        second = (ring_index + 1) * point_count
        for index in range(point_count):
            next_index = (index + 1) % point_count
            faces.append(
                (
                    first + index,
                    first + next_index,
                    second + next_index,
                    second + index,
                )
            )
    final_start = (len(rings) - 1) * point_count
    faces.append(tuple(final_start + index for index in range(point_count)))

    mesh = bpy.data.meshes.new(f"{name}-mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.validate(verbose=False)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def _pin_tool(connector: ConnectorInstruction) -> Any:
    root = connector.root_fillet_mm
    tip = connector.tip_chamfer_mm
    tip_width = max(connector.width_mm - 2.0 * tip, 0.1)
    tip_height = max(connector.height_mm - 2.0 * tip, 0.1)
    tip_radius = min(connector.corner_radius_mm, tip_width / 2.0, tip_height / 2.0)
    return _profile_solid(
        f"connector-{connector.id}-pin",
        connector,
        (
            (
                -root,
                connector.width_mm + 2.0 * root,
                connector.height_mm + 2.0 * root,
                connector.corner_radius_mm + root,
            ),
            (0.0, connector.width_mm, connector.height_mm, connector.corner_radius_mm),
            (
                connector.engagement_mm - tip,
                connector.width_mm,
                connector.height_mm,
                connector.corner_radius_mm,
            ),
            (connector.engagement_mm, tip_width, tip_height, tip_radius),
        ),
    )


def _socket_tool(connector: ConnectorInstruction) -> Any:
    width, height, depth = socket_dimensions(connector)
    radius = min(
        connector.corner_radius_mm + connector.clearance_per_side_mm,
        width / 2.0,
        height / 2.0,
    )
    overlap = min(0.05, connector.socket_bottom_clearance_mm / 2.0)
    return _profile_solid(
        f"connector-{connector.id}-socket",
        connector,
        (
            (-overlap, width, height, radius),
            (depth, width, height, radius),
        ),
    )


def _apply_boolean(target: Any, tool: Any, operation: str) -> None:
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    modifier = target.modifiers.new(name=f"connector-{operation.lower()}", type="BOOLEAN")
    modifier.operation = operation
    modifier.solver = "EXACT"
    modifier.object = tool
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def _socket_clearances(
    female_obj: Any, connector: ConnectorInstruction
) -> tuple[float, float]:
    import bmesh
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    frame = connector_frame(connector.axis, connector.key_direction)
    center = Vector(connector.center_mm)
    axis = Vector(frame.axis)
    key = Vector(frame.key)
    side = Vector(frame.side)
    width, height, depth = socket_dimensions(connector)
    epsilon = 0.02

    mesh = bmesh.new()
    mesh.from_mesh(female_obj.data)
    mesh.normal_update()
    bvh = BVHTree.FromBMesh(mesh)

    def clearance_at(sample_depth: float) -> list[float]:
        values: list[float] = []
        for direction, extent in (
            (key, width / 2.0),
            (-key, width / 2.0),
            (side, height / 2.0),
            (-side, height / 2.0),
        ):
            start = center + axis * sample_depth + direction * (extent + epsilon)
            hit, _normal, _index, distance = bvh.ray_cast(start, direction)
            if hit is not None and distance is not None and distance > 0:
                values.append(float(distance))
        return values

    edge_values = clearance_at(min(0.1, depth / 4.0))
    wall_values = edge_values + clearance_at(depth / 2.0)
    mesh.free()
    if not edge_values or not wall_values:
        return 0.0, 0.0
    return min(wall_values), min(edge_values)


def _apply_connector(
    pieces: Mapping[str, Any], connector: ConnectorInstruction
) -> dict[str, Any]:
    import bpy

    male = pieces[connector.male_piece]
    female = pieces[connector.female_piece]
    male_before = _object_volume(male)
    female_before = _object_volume(female)

    pin = _pin_tool(connector)
    _apply_boolean(male, pin, "UNION")
    bpy.data.objects.remove(pin, do_unlink=True)
    male_after = _object_volume(male)

    socket = _socket_tool(connector)
    _apply_boolean(female, socket, "DIFFERENCE")
    bpy.data.objects.remove(socket, do_unlink=True)
    female_after = _object_volume(female)
    minimum_wall, minimum_edge = _socket_clearances(female, connector)

    return {
        "id": connector.id,
        "cut_id": connector.cut_id,
        "male_piece": connector.male_piece,
        "female_piece": connector.female_piece,
        "solver": "EXACT",
        "union_applied": male_after > male_before,
        "difference_applied": female_after < female_before,
        "male_volume_before_mm3": male_before,
        "male_volume_after_mm3": male_after,
        "female_volume_before_mm3": female_before,
        "female_volume_after_mm3": female_after,
        "theoretical_pin_volume_mm3": nominal_pin_volume(connector),
        "measured_added_volume_mm3": male_after - male_before,
        "measured_removed_volume_mm3": female_before - female_after,
        "effective_length_mm": connector.engagement_mm,
        "socket_depth_mm": connector.engagement_mm
        + connector.socket_bottom_clearance_mm,
        "minimum_wall_mm": minimum_wall,
        "minimum_edge_margin_mm": minimum_edge,
    }


def _export_stl(obj: Any, destination: Path) -> None:
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.wm.stl_export(
        filepath=str(destination),
        export_selected_objects=True,
        ascii_format=False,
    )


def _assembly_transforms(plan: SplitPlan) -> dict[str, tuple[float, ...]]:
    assembly_plates = [plate for plate in plan.plates if plate.id == "assembly"]
    if len(assembly_plates) != 1 or len(plan.plates) != 1:
        raise CutEvidenceError(
            "standard assembly requires one logical assembly plate"
        )
    assembly = assembly_plates[0]
    if assembly.layout is None or set(assembly.piece_ids) != set(plan.leaf_piece_ids):
        raise CutEvidenceError(
            "standard assembly layout must cover every leaf piece"
        )
    transforms: dict[str, tuple[float, ...]] = {}
    for placement in assembly.layout:
        if any(
            not math.isclose(value, 0.0, abs_tol=1e-9)
            for value in (*placement.position_mm, *placement.rotation_deg)
        ):
            raise CutEvidenceError(
                "standard assembly requires identity layout for world-space pieces"
            )
        transforms[placement.piece_id] = IDENTITY_TRANSFORM
    return transforms


def _export_standard_3mf(
    pieces: Mapping[str, Any],
    destination: Path,
    expected_transforms: Mapping[str, Sequence[float]],
) -> Standard3MFVerification:
    if set(pieces) != set(expected_transforms):
        raise CutEvidenceError("standard assembly piece mapping mismatch")

    def payload_for(obj: Any, transform: Sequence[float]) -> MeshPayload:
        obj.data.calc_loop_triangles()
        return MeshPayload(
            vertices=(
                tuple(float(value) for value in vertex.co)
                for vertex in obj.data.vertices
            ),
            triangles=(
                tuple(int(index) for index in triangle.vertices)
                for triangle in obj.data.loop_triangles
            ),
            transform=transform,
        )

    payloads: dict[str, MeshPayload] = {}
    for piece_id in sorted(pieces):
        obj = pieces[piece_id]
        obj.name = piece_id
        obj.data.name = piece_id
        payloads[piece_id] = payload_for(obj, expected_transforms[piece_id])
    write_standard_3mf(
        destination,
        payloads,
    )
    return verify_standard_3mf(
        destination,
        expected_piece_ids=tuple(sorted(pieces)),
        expected_transforms=expected_transforms,
    )


def _import_3mf_source(source: Path) -> Any:
    import bpy

    try:
        payload = load_3mf_mesh(source)
    except ThreeMFImportError as exc:
        raise CutEvidenceError(str(exc)) from exc

    mesh = bpy.data.meshes.new(source.stem)
    mesh.vertices.add(payload.vertex_count)
    mesh.vertices.foreach_set("co", payload.vertices)
    loop_count = payload.triangle_count * 3
    mesh.loops.add(loop_count)
    mesh.loops.foreach_set("vertex_index", payload.triangles)
    mesh.polygons.add(payload.triangle_count)
    mesh.polygons.foreach_set("loop_start", array("I", range(0, loop_count, 3)))
    mesh.polygons.foreach_set(
        "loop_total", array("I", (3,)) * payload.triangle_count
    )
    mesh.update(calc_edges=True)

    obj = bpy.data.objects.new(source.stem, mesh)
    bpy.context.collection.objects.link(obj)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    return obj


def _import_source(source: Path) -> Any:
    """Import one supported mesh source and return a single mesh object."""
    import bpy

    suffix = source.suffix.lower()
    if suffix == ".stl":
        bpy.ops.wm.stl_import(filepath=str(source))
    elif suffix == ".obj":
        bpy.ops.wm.obj_import(filepath=str(source))
    elif suffix == ".3mf":
        return _import_3mf_source(source)
    else:
        raise CutEvidenceError(f"unsupported source mesh format: {suffix}")

    objects = [obj for obj in bpy.context.selected_objects if obj.type == "MESH"]
    if not objects:
        raise CutEvidenceError("source import produced no mesh object")
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    if len(objects) > 1:
        bpy.ops.object.join()
    return bpy.context.view_layer.objects.active


def _run_blender(
    source: Path,
    plan: SplitPlan,
    output_dir: Path,
    *,
    validation_level: str = "light",
) -> dict[str, Any]:
    import bpy

    bpy.ops.wm.read_factory_settings(use_empty=True)
    source_obj = _import_source(source)
    bpy.ops.object.select_all(action="DESELECT")
    source_obj.select_set(True)
    bpy.context.view_layer.objects.active = source_obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    source_stats = _mesh_stats(source_obj, validation_level)
    source_sha = sha256_file(source)
    pieces: dict[str, Any] = {"source": source_obj}
    cut_records: list[dict[str, Any]] = []

    for cut in plan.cuts:
        target = pieces.pop(cut.input_piece)
        diagnostic_path = output_dir / "diagnostics" / f"component-assignment-{cut.id}.png"
        try:
            negative, positive, component_assignment = bounded_split(
                target,
                cut_id=cut.id,
                point_mm=cut.point_mm,
                normal=cut.normal,
                target_side=_cut_target_side(plan, cut),
                seed_points=_connector_seed_points(plan, cut),
                diagnostic_path=diagnostic_path,
            )
        except ComponentAssignmentRequired as exc:
            output_dir.mkdir(parents=True, exist_ok=True)
            failure = {
                "status": exc.status,
                "script_version": SCRIPT_VERSION,
                "script_sha256": sha256_file(Path(__file__).resolve()),
                "imported_script_sha256": local_script_hashes(),
                "source_model": str(source),
                "source_model_sha256": source_sha,
                "cut_ids": [item.id for item in plan.cuts],
                "failed_cut_id": cut.id,
                "candidates": [item.to_dict() for item in exc.candidates],
                "diagnostic_path": getattr(exc, "diagnostic_path", None),
                "diagnostic_sha256": getattr(exc, "diagnostic_sha256", None),
                "source_stats": source_stats,
                "validation": {
                    "source_unchanged": sha256_file(source) == source_sha,
                    "geometry_evidence_passed": False,
                },
            }
            (output_dir / "cut-evidence.json").write_text(
                json.dumps(failure, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            raise CutEvidenceError(str(exc)) from exc
        negative.name = cut.negative_piece
        positive.name = cut.positive_piece
        pieces[cut.negative_piece] = negative
        pieces[cut.positive_piece] = positive
        cut_records.append(
            {
                "id": cut.id,
                "input_piece": cut.input_piece,
                "point_mm": list(cut.point_mm),
                "normal": list(cut.normal),
                "negative_piece": cut.negative_piece,
                "positive_piece": cut.positive_piece,
                "component_assignment": component_assignment,
            }
        )
        bpy.data.objects.remove(target, do_unlink=True)

    pieces_dir = output_dir / "pieces"
    pieces_dir.mkdir(parents=True, exist_ok=True)
    connector_records = [
        _apply_connector(pieces, connector) for connector in plan.connectors
    ]
    connector_evidence: dict[str, Any] | None = None
    connector_evidence_path: Path | None = None
    if connector_records:
        connector_evidence = {
            "status": "validated",
            "connectors": connector_records,
            "measured_net_volume_delta_mm3": sum(
                record["measured_added_volume_mm3"]
                - record["measured_removed_volume_mm3"]
                for record in connector_records
            ),
        }
        try:
            validate_connector_evidence(connector_evidence, plan)
        except ConnectorEvidenceError as exc:
            connector_evidence["status"] = "needs_geometry_repair"
            connector_evidence["validation_error"] = str(exc)
            connector_evidence_path = output_dir / "connector-evidence.json"
            connector_evidence_path.write_text(
                json.dumps(connector_evidence, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            raise CutEvidenceError(str(exc)) from exc
        connector_evidence_path = output_dir / "connector-evidence.json"
        connector_evidence_path.write_text(
            json.dumps(connector_evidence, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    piece_records = []
    for piece_id in plan.leaf_piece_ids:
        obj = pieces[piece_id]
        filename = (
            f"{piece_id}.stl"
            if piece_id.startswith("piece-")
            else f"piece-{piece_id}.stl"
        )
        destination = pieces_dir / filename
        _export_stl(obj, destination)
        piece_records.append(
            {
                "piece_id": piece_id,
                "path": str(destination),
                "sha256": sha256_file(destination),
                "stats": _mesh_stats(obj, validation_level),
            }
        )
    standard_3mf: Standard3MFVerification | None = None
    if plan.assembly_filename is not None:
        standard_3mf = _export_standard_3mf(
            {piece_id: pieces[piece_id] for piece_id in plan.leaf_piece_ids},
            output_dir / plan.assembly_filename,
            _assembly_transforms(plan),
        )
    structure_diagram: dict[str, object] | None = None
    if plan.structure_diagram_filename is not None:
        structure_diagram = render_structure_diagram(
            {piece_id: pieces[piece_id] for piece_id in plan.leaf_piece_ids},
            plan.connectors,
            output_dir / plan.structure_diagram_filename,
        )
    source_sha_after = sha256_file(source)
    volume_sum = sum(item["stats"]["volume_mm3"] for item in piece_records)
    evidence = {
        "status": "generated_needs_full_validation",
        "validation_level": validation_level,
        "automated_checks": [
            "source_unchanged",
            "files_nonempty",
            "piece_count_and_names",
            "connected_components",
            "boundary_and_non_manifold_edges",
            "connector_volume_change",
            "connector_local_clearance_samples",
            "artifact_hashes",
        ],
        "deferred_to_user": (
            list(LIGHT_DEFERRED_CHECKS)
            if validation_level == "light"
            else []
        ),
        "script_version": SCRIPT_VERSION,
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "imported_script_sha256": local_script_hashes(),
        "blender_version": ".".join(str(value) for value in bpy.app.version),
        "source_model": str(source),
        "source_model_sha256": source_sha,
        "source_sha256_before": source_sha,
        "source_sha256_after": source_sha_after,
        "split_requested": True,
        "cuts": cut_records,
        "cut_ids": [cut.id for cut in plan.cuts],
        "source_stats": source_stats,
        "source_volume_mm3": source_stats["volume_mm3"],
        "pieces": piece_records,
        "connector_evidence": connector_evidence,
        "connector_evidence_path": (
            str(connector_evidence_path) if connector_evidence_path else None
        ),
        "connector_evidence_sha256": (
            sha256_file(connector_evidence_path) if connector_evidence_path else None
        ),
        "standard_3mf": asdict(standard_3mf) if standard_3mf else None,
        "structure_diagram": structure_diagram,
        "piece_hashes": {
            item["piece_id"]: item["sha256"] for item in piece_records
        },
        "volume_sum_mm3": volume_sum,
        "validation": {
            "source_unchanged": source_sha == source_sha_after,
            "piece_count": len(piece_records) == len(plan.leaf_piece_ids),
            "all_piece_files_nonempty": all(
                Path(item["path"]).stat().st_size > 0 for item in piece_records
            ),
        },
    }
    try:
        summary = validate_cut_evidence(
            evidence,
            plan,
            source_sha256=source_sha,
            script_sha256=evidence["script_sha256"],
        )
    except CutEvidenceError as exc:
        evidence["validation"]["geometry_evidence_passed"] = False
        evidence["validation_error"] = str(exc)
    else:
        evidence["validation"]["geometry_evidence_passed"] = True
        evidence["volume_relative_error"] = summary["volume_relative_error"]
        evidence["status"] = (
            "generated_for_user_review"
            if validation_level == "light"
            else "validated"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "cut-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return evidence


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audited Blender background mesh cut")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--validation-level",
        choices=VALIDATION_LEVELS,
        default="light",
    )
    payload = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    return parser.parse_args(payload)


def _source_matches_plan(plan_path: Path, source: Path, declared: str) -> bool:
    declared_path = Path(os.path.expanduser(os.path.expandvars(declared)))
    candidates = []
    if declared_path.is_absolute():
        candidates.append(declared_path.resolve())
    else:
        candidates.extend(
            (
                (Path.cwd() / declared_path).resolve(),
                (plan_path.parent / declared_path).resolve(),
            )
        )
    return source.resolve() in candidates


def main() -> int:
    args = _parse_args()
    source = args.source.resolve(strict=True)
    plan_path = args.plan.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    plan = load_split_plan(plan_path)
    if source.suffix.lower() not in {".stl", ".3mf", ".obj"}:
        raise SystemExit("unsupported source mesh format")
    if not _source_matches_plan(plan_path, source, plan.source_model):
        raise SystemExit("source path does not match split plan source_model")
    evidence = _run_blender(
        source,
        plan,
        output_dir,
        validation_level=args.validation_level,
    )
    print(json.dumps(evidence, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        _exit_code = main()
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    raise SystemExit(_exit_code)
