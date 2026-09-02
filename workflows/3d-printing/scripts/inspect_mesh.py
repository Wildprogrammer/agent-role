from __future__ import annotations

import argparse
import json
import math
import os
import sys
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


SCRIPT_VERSION = "1.2.0"
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from connector_geometry import connector_frame, socket_dimensions
from headless_cut import _cut_piece, _import_source, sha256_file
from split_plan import ConnectorInstruction, SplitPlan, load_split_plan


VIEW_IDS = ("front", "back", "left", "right", "top", "bottom")


class InspectionEvidenceError(ValueError):
    pass


def build_blender_inspection_command(
    blender_path: Path,
    script_path: Path,
    source_path: Path,
    output_dir: Path,
    *,
    candidate_plan: Path | None = None,
) -> tuple[str, ...]:
    command = [
        str(blender_path.resolve()),
        "--background",
        "--python",
        str(script_path.resolve()),
        "--",
        "--source",
        str(source_path.resolve()),
        "--output-dir",
        str(output_dir.resolve()),
    ]
    if candidate_plan is not None:
        command.extend(("--candidate-plan", str(candidate_plan.resolve())))
    return tuple(command)


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InspectionEvidenceError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise InspectionEvidenceError(f"{label} must be finite")
    return result


def _vector(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InspectionEvidenceError(f"{label} must contain three numbers")
    if len(value) != 3:
        raise InspectionEvidenceError(f"{label} must contain three numbers")
    return tuple(_number(item, label) for item in value)  # type: ignore[return-value]


def validate_inspection_evidence(
    evidence: Mapping[str, Any],
    *,
    source_sha256: str,
    candidate_plan: SplitPlan | None = None,
) -> dict[str, Any]:
    for key in (
        "source_model_sha256",
        "source_sha256_before",
        "source_sha256_after",
    ):
        if _value(evidence, key) != source_sha256:
            raise InspectionEvidenceError("source hash changed during inspection")
    if _value(evidence, "units") != "mm":
        raise InspectionEvidenceError("inspection units must be mm")
    bounds = _value(evidence, "bounds")
    if not isinstance(bounds, Mapping):
        raise InspectionEvidenceError("bounds are required")
    _vector(_value(bounds, "min_mm"), "minimum bounds")
    _vector(_value(bounds, "max_mm"), "maximum bounds")
    dimensions = _vector(_value(bounds, "dimensions_mm"), "dimensions")
    if any(value <= 0 for value in dimensions):
        raise InspectionEvidenceError("dimensions must be positive")

    views = _value(evidence, "views")
    if not isinstance(views, Mapping) or set(views) != set(VIEW_IDS):
        raise InspectionEvidenceError("six views are required")
    for view_id, raw_path in views.items():
        path = Path(str(raw_path))
        if not path.is_file() or path.stat().st_size <= 0:
            raise InspectionEvidenceError(f"view is missing or empty: {view_id}")

    raw_cuts = _value(evidence, "candidate_cuts", [])
    raw_connectors = _value(evidence, "candidate_connectors", [])
    if not isinstance(raw_cuts, Sequence) or isinstance(raw_cuts, (str, bytes)):
        raise InspectionEvidenceError("candidate cuts must be a list")
    if not isinstance(raw_connectors, Sequence) or isinstance(
        raw_connectors, (str, bytes)
    ):
        raise InspectionEvidenceError("candidate connectors must be a list")

    ready = False
    if candidate_plan is not None:
        expected_cuts = {cut.id: cut for cut in candidate_plan.cuts}
        actual_cuts = {_value(item, "id"): item for item in raw_cuts}
        if len(actual_cuts) != len(raw_cuts) or set(actual_cuts) != set(expected_cuts):
            raise InspectionEvidenceError("candidate cut set does not match plan")
        for cut_id, cut_record in actual_cuts.items():
            if _number(_value(cut_record, "section_width_mm"), "section width") <= 0:
                raise InspectionEvidenceError(f"candidate cut {cut_id} has no section")
            if _number(_value(cut_record, "section_height_mm"), "section height") <= 0:
                raise InspectionEvidenceError(f"candidate cut {cut_id} has no section")

        expected_connectors = {
            connector.id: connector for connector in candidate_plan.connectors
        }
        actual_connectors = {
            _value(item, "id"): item for item in raw_connectors
        }
        if (
            len(actual_connectors) != len(raw_connectors)
            or set(actual_connectors) != set(expected_connectors)
        ):
            raise InspectionEvidenceError(
                "candidate connector set does not match plan"
            )
        unsafe_messages: list[str] = []
        for connector_id, record in actual_connectors.items():
            connector = expected_connectors[connector_id]
            if _value(record, "cut_id") != connector.cut_id:
                raise InspectionEvidenceError(
                    f"candidate connector cut mismatch: {connector_id}"
                )
            if _value(record, "section_outline_inside") is not True:
                unsafe_messages.append(f"section outline failed for {connector_id}")
            edge_margin = _number(
                _value(record, "minimum_edge_margin_mm"), "edge margin"
            )
            if edge_margin + 1e-6 < connector.minimum_edge_margin_mm:
                unsafe_messages.append(f"edge margin failed for {connector_id}")
            wall = _number(
                _value(record, "estimated_minimum_wall_mm"), "wall"
            )
            if wall + 1e-6 < connector.minimum_wall_mm:
                unsafe_messages.append(f"wall failed for {connector_id}")
            required_depth = connector.engagement_mm + connector.socket_bottom_clearance_mm
            reported_required = _number(
                _value(record, "required_socket_depth_mm"), "required socket depth"
            )
            if not math.isclose(
                reported_required, required_depth, rel_tol=1e-6, abs_tol=1e-6
            ):
                raise InspectionEvidenceError(
                    f"required socket depth mismatch for {connector_id}"
                )
            available_depth = _number(
                _value(record, "available_depth_mm"), "available depth"
            )
            if available_depth + 1e-6 < required_depth:
                unsafe_messages.append(f"depth failed for {connector_id}")
        status = _value(evidence, "status")
        if unsafe_messages:
            if status != "needs_geometry_redesign":
                raise InspectionEvidenceError("; ".join(unsafe_messages))
        elif status != "ready_for_gate_a":
            raise InspectionEvidenceError(
                "safe candidate inspection must be ready_for_gate_a"
            )
        else:
            ready = True
    elif raw_cuts or raw_connectors:
        raise InspectionEvidenceError("candidate plan is required for candidate evidence")

    return {
        "view_count": len(views),
        "candidate_cut_count": len(raw_cuts),
        "candidate_connector_count": len(raw_connectors),
        "ready_for_gate_a": ready,
    }


def _bounds(obj: Any) -> dict[str, list[float]]:
    from mathutils import Vector

    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    minimum = [min(point[index] for point in points) for index in range(3)]
    maximum = [max(point[index] for point in points) for index in range(3)]
    return {
        "min_mm": [float(value) for value in minimum],
        "max_mm": [float(value) for value in maximum],
        "dimensions_mm": [float(b - a) for a, b in zip(minimum, maximum)],
    }


def _plane_basis(normal: Sequence[float]):
    axis = tuple(float(value) for value in normal)
    reference = (0.0, 0.0, 1.0)
    axis_length = math.sqrt(sum(value * value for value in axis))
    normalized = tuple(value / axis_length for value in axis)
    if abs(sum(a * b for a, b in zip(normalized, reference))) > 0.9:
        reference = (1.0, 0.0, 0.0)
    return connector_frame(normalized, reference)


def _section_measurement(obj: Any, cut: Any) -> dict[str, Any]:
    import bmesh
    from mathutils import Vector

    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    result = bmesh.ops.bisect_plane(
        mesh,
        geom=list(mesh.verts) + list(mesh.edges) + list(mesh.faces),
        plane_co=Vector(cut.point_mm),
        plane_no=Vector(cut.normal),
        clear_inner=False,
        clear_outer=False,
        dist=0.000001,
    )
    cut_vertices = [
        item for item in result.get("geom_cut", []) if isinstance(item, bmesh.types.BMVert)
    ]
    cut_vertex_set = set(cut_vertices)
    cut_edges = [
        item
        for item in result.get("geom_cut", [])
        if isinstance(item, bmesh.types.BMEdge)
        and all(vertex in cut_vertex_set for vertex in item.verts)
    ]
    if not cut_vertices:
        mesh.free()
        return {
            "id": cut.id,
            "point_mm": list(cut.point_mm),
            "normal": list(cut.normal),
            "section_width_mm": 0.0,
            "section_height_mm": 0.0,
            "segments": [],
        }
    frame = _plane_basis(cut.normal)
    origin = Vector(cut.point_mm)
    key = Vector(frame.key)
    side = Vector(frame.side)
    projections = [
        ((vertex.co - origin).dot(key), (vertex.co - origin).dot(side))
        for vertex in cut_vertices
    ]
    segments = [
        [tuple(float(value) for value in edge.verts[0].co), tuple(float(value) for value in edge.verts[1].co)]
        for edge in cut_edges
    ]
    record = {
        "id": cut.id,
        "point_mm": list(cut.point_mm),
        "normal": list(cut.normal),
        "section_width_mm": float(
            max(value[0] for value in projections) - min(value[0] for value in projections)
        ),
        "section_height_mm": float(
            max(value[1] for value in projections) - min(value[1] for value in projections)
        ),
        "segments": segments,
    }
    mesh.free()
    return record


def _point_segment_distance(point: Any, first: Any, second: Any) -> float:
    segment = second - first
    length_squared = segment.length_squared
    if math.isclose(length_squared, 0.0, abs_tol=1e-12):
        return float((point - first).length)
    ratio = max(0.0, min(1.0, (point - first).dot(segment) / length_squared))
    return float((point - (first + segment * ratio)).length)


def _point_segment_distance_2d(
    point: tuple[float, float],
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    segment = (second[0] - first[0], second[1] - first[1])
    length_squared = segment[0] * segment[0] + segment[1] * segment[1]
    if math.isclose(length_squared, 0.0, abs_tol=1e-12):
        return math.hypot(point[0] - first[0], point[1] - first[1])
    ratio = max(
        0.0,
        min(
            1.0,
            (
                (point[0] - first[0]) * segment[0]
                + (point[1] - first[1]) * segment[1]
            )
            / length_squared,
        ),
    )
    nearest = (first[0] + segment[0] * ratio, first[1] + segment[1] * ratio)
    return math.hypot(point[0] - nearest[0], point[1] - nearest[1])


def _point_inside_section_2d(
    point: tuple[float, float],
    segments: Sequence[tuple[tuple[float, float], tuple[float, float]]],
) -> bool:
    if any(
        _point_segment_distance_2d(point, first, second) <= 1e-6
        for first, second in segments
    ):
        return True
    crossings = 0
    for first, second in segments:
        if (first[1] > point[1]) == (second[1] > point[1]):
            continue
        x_crossing = first[0] + (point[1] - first[1]) * (
            second[0] - first[0]
        ) / (second[1] - first[1])
        if x_crossing > point[0]:
            crossings += 1
    return crossings % 2 == 1


def _segment_components_2d(
    segments: Sequence[tuple[tuple[float, float], tuple[float, float]]],
) -> list[list[tuple[tuple[float, float], tuple[float, float]]]]:
    point_to_segments: dict[tuple[float, float], list[int]] = {}
    for index, segment in enumerate(segments):
        for point in segment:
            key = (round(point[0], 6), round(point[1], 6))
            point_to_segments.setdefault(key, []).append(index)

    adjacency: list[set[int]] = [set() for _segment in segments]
    for indexes in point_to_segments.values():
        for index in indexes:
            adjacency[index].update(indexes)

    remaining = set(range(len(segments)))
    components = []
    while remaining:
        pending = [remaining.pop()]
        indexes = []
        while pending:
            index = pending.pop()
            indexes.append(index)
            neighbours = adjacency[index] & remaining
            remaining.difference_update(neighbours)
            pending.extend(neighbours)
        components.append([segments[index] for index in indexes])
    return components


def _connector_section_fit(
    connector: ConnectorInstruction,
    section: Mapping[str, Any],
    *,
    search_step_mm: float = 0.5,
) -> dict[str, Any]:
    frame = connector_frame(connector.axis, connector.key_direction)
    origin = tuple(float(value) for value in connector.center_mm)

    def project(point: Sequence[float]) -> tuple[float, float]:
        delta = tuple(
            float(value) - origin[index] for index, value in enumerate(point)
        )
        return (
            sum(delta[index] * frame.key[index] for index in range(3)),
            sum(delta[index] * frame.side[index] for index in range(3)),
        )

    segments = [
        (project(first), project(second))
        for first, second in _value(section, "segments", [])
    ]
    if not segments:
        return {
            "section_outline_inside": False,
            "suggested_center_mm": None,
            "suggested_minimum_edge_margin_mm": 0.0,
        }

    width, height, _depth = socket_dimensions(connector)
    half_width = width / 2.0
    half_height = height / 2.0
    outline_offsets = (
        (0.0, 0.0),
        (half_width, 0.0),
        (-half_width, 0.0),
        (0.0, half_height),
        (0.0, -half_height),
        (half_width, half_height),
        (half_width, -half_height),
        (-half_width, half_height),
        (-half_width, -half_height),
    )

    def outline(center: tuple[float, float]) -> tuple[tuple[float, float], ...]:
        return tuple(
            (center[0] + offset[0], center[1] + offset[1])
            for offset in outline_offsets
        )

    def fits(
        center: tuple[float, float],
        target_segments: Sequence[
            tuple[tuple[float, float], tuple[float, float]]
        ],
    ) -> bool:
        return all(
            _point_inside_section_2d(point, target_segments)
            for point in outline(center)
        )

    def margin(
        center: tuple[float, float],
        target_segments: Sequence[
            tuple[tuple[float, float], tuple[float, float]]
        ],
    ) -> float:
        return min(
            _point_segment_distance_2d(point, first, second)
            for point in outline(center)
            for first, second in target_segments
        )

    current_inside = fits((0.0, 0.0), segments)
    current_margin = margin((0.0, 0.0), segments)
    best_center: tuple[float, float] | None = None
    best_margin = -1.0
    if (
        current_inside
        and current_margin + 1e-6 >= connector.minimum_edge_margin_mm
    ):
        best_center = (0.0, 0.0)
        best_margin = current_margin
    else:
        viable_components = []
        for component in _segment_components_2d(segments):
            points = [point for segment in component for point in segment]
            minimum = (
                min(point[0] for point in points),
                min(point[1] for point in points),
            )
            maximum = (
                max(point[0] for point in points),
                max(point[1] for point in points),
            )
            span = (maximum[0] - minimum[0], maximum[1] - minimum[1])
            if span[0] + 1e-6 < width or span[1] + 1e-6 < height:
                continue
            distance = math.hypot(
                max(minimum[0], 0.0, -maximum[0]),
                max(minimum[1], 0.0, -maximum[1]),
            )
            viable_components.append((distance, component, minimum, maximum))

        local_options = []
        for _distance, component, minimum, maximum in sorted(
            viable_components, key=lambda item: item[0]
        )[:8]:
            span = (maximum[0] - minimum[0], maximum[1] - minimum[1])
            step = max(search_step_mm, max(span) / 20.0)
            local_best = None
            local_margin = -1.0
            local_offset_squared = math.inf
            u_value = math.ceil(minimum[0] / step) * step
            while u_value <= maximum[0] + 1e-9:
                v_value = math.ceil(minimum[1] / step) * step
                while v_value <= maximum[1] + 1e-9:
                    candidate = (u_value, v_value)
                    if fits(candidate, component):
                        candidate_margin = margin(candidate, component)
                        offset_squared = u_value * u_value + v_value * v_value
                        if (
                            candidate_margin > local_margin + 1e-9
                            or (
                                math.isclose(
                                    candidate_margin, local_margin, abs_tol=1e-9
                                )
                                and offset_squared < local_offset_squared
                            )
                        ):
                            local_best = candidate
                            local_margin = candidate_margin
                            local_offset_squared = offset_squared
                    v_value += step
                u_value += step
            if local_best is not None:
                local_options.append(
                    (local_margin, local_offset_squared, local_best)
                )

        for candidate_margin, _distance, candidate in sorted(
            local_options, key=lambda item: (-item[0], item[1])
        ):
            if fits(candidate, segments):
                best_center = candidate
                best_margin = candidate_margin
                break
        if best_center is None and current_inside:
            best_center = (0.0, 0.0)
            best_margin = current_margin

    suggested = None
    if best_center is not None:
        suggested = [
            origin[index]
            + frame.key[index] * best_center[0]
            + frame.side[index] * best_center[1]
            for index in range(3)
        ]
    return {
        "section_outline_inside": current_inside,
        "suggested_center_mm": suggested,
        "suggested_minimum_edge_margin_mm": max(best_margin, 0.0),
    }


def _connector_measurement(
    obj: Any,
    connector: ConnectorInstruction,
    section: Mapping[str, Any],
) -> dict[str, Any]:
    import bmesh
    from mathutils import Vector
    from mathutils.bvhtree import BVHTree

    frame = connector_frame(connector.axis, connector.key_direction)
    center = Vector(connector.center_mm)
    axis = Vector(frame.axis)
    key = Vector(frame.key)
    side = Vector(frame.side)
    socket_width, socket_height, socket_depth = socket_dimensions(connector)
    outline = [
        center + key * (socket_width / 2.0),
        center - key * (socket_width / 2.0),
        center + side * (socket_height / 2.0),
        center - side * (socket_height / 2.0),
    ]
    segments = [
        (Vector(first), Vector(second))
        for first, second in _value(section, "segments", [])
    ]
    edge_margin = 0.0
    if segments:
        edge_margin = min(
            _point_segment_distance(point, first, second)
            for point in outline
            for first, second in segments
        )

    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    mesh.normal_update()
    bvh = BVHTree.FromBMesh(mesh)
    epsilon = 0.02
    hit, _normal, _index, available_depth = bvh.ray_cast(
        center + axis * epsilon, axis
    )
    available = float(available_depth) if hit is not None and available_depth else 0.0

    wall_values: list[float] = []
    sample_depth = min(socket_depth / 2.0, max(available / 2.0, epsilon))
    for direction, extent in (
        (key, socket_width / 2.0),
        (-key, socket_width / 2.0),
        (side, socket_height / 2.0),
        (-side, socket_height / 2.0),
    ):
        start = center + axis * sample_depth + direction * (extent + epsilon)
        wall_hit, _wall_normal, _wall_index, wall_distance = bvh.ray_cast(start, direction)
        if wall_hit is not None and wall_distance is not None and wall_distance > 0:
            wall_values.append(float(wall_distance))
    mesh.free()
    minimum_wall = min(wall_values) if wall_values else 0.0
    return {
        "id": connector.id,
        "cut_id": connector.cut_id,
        "center_mm": list(connector.center_mm),
        "axis": list(connector.axis),
        "minimum_edge_margin_mm": edge_margin,
        "estimated_minimum_wall_mm": minimum_wall,
        "available_depth_mm": available,
        "required_socket_depth_mm": socket_depth,
        **_connector_section_fit(connector, section),
    }


def _remove_temporary_object(obj: Any) -> None:
    import bpy

    mesh = obj.data
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _measure_candidate_sequence(
    source_obj: Any,
    plan: SplitPlan,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    pieces: dict[str, Any] = {"source": source_obj}
    temporary: list[Any] = []
    cut_records: list[dict[str, Any]] = []
    completed = False
    try:
        for cut in plan.cuts:
            target = pieces.pop(cut.input_piece)
            cut_records.append(_section_measurement(target, cut))
            negative = _cut_piece(
                target,
                cut.negative_piece,
                point_mm=cut.point_mm,
                normal=cut.normal,
                keep_positive=False,
            )
            temporary.append(negative)
            positive = _cut_piece(
                target,
                cut.positive_piece,
                point_mm=cut.point_mm,
                normal=cut.normal,
                keep_positive=True,
            )
            temporary.append(positive)
            pieces[cut.negative_piece] = negative
            pieces[cut.positive_piece] = positive
            if target is not source_obj:
                temporary.remove(target)
                _remove_temporary_object(target)

        by_cut = {record["id"]: record for record in cut_records}
        connector_records = [
            _connector_measurement(
                pieces[connector.female_piece],
                connector,
                by_cut[connector.cut_id],
            )
            for connector in plan.connectors
        ]
        completed = True
        return cut_records, connector_records, pieces
    finally:
        if not completed:
            for obj in tuple(temporary):
                _remove_temporary_object(obj)


def _emission_material(name: str, color: tuple[float, ...]) -> Any:
    import bpy

    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = 0.8
    material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def _assign_emission_material(
    obj: Any, name: str, color: tuple[float, ...]
) -> None:
    obj.data.materials.clear()
    obj.data.materials.append(_emission_material(name, color))


def _add_line_loop(name: str, points: Sequence[Any], color: tuple[float, ...]) -> None:
    import bpy

    curve = bpy.data.curves.new(name, type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = 0.25
    curve.bevel_resolution = 2
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for target, point in zip(spline.points, points):
        target.co = (*point, 1.0)
    spline.use_cyclic_u = True
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    _assign_emission_material(obj, f"{name}-material", color)


def _add_overlays(plan: SplitPlan, cut_records: Mapping[str, Mapping[str, Any]]) -> None:
    from mathutils import Vector

    for cut in plan.cuts:
        record = cut_records[cut.id]
        frame = _plane_basis(cut.normal)
        center = Vector(cut.point_mm)
        key = Vector(frame.key)
        side = Vector(frame.side)
        half_width = max(float(record["section_width_mm"]) / 2.0, 1.0)
        half_height = max(float(record["section_height_mm"]) / 2.0, 1.0)
        points = (
            center + key * half_width + side * half_height,
            center - key * half_width + side * half_height,
            center - key * half_width - side * half_height,
            center + key * half_width - side * half_height,
        )
        _add_line_loop(f"cut-{cut.id}", points, (1.0, 0.05, 0.05, 1.0))
    for connector in plan.connectors:
        frame = connector_frame(connector.axis, connector.key_direction)
        center = Vector(connector.center_mm)
        axis = Vector(frame.axis)
        end = center + axis * connector.engagement_mm
        _add_line_loop(
            f"connector-{connector.id}",
            (center, end, end + Vector(frame.key) * 0.01),
            (1.0, 0.8, 0.0, 1.0),
        )


def _render_views(obj: Any, output_dir: Path, bounds: Mapping[str, Any]) -> dict[str, str]:
    import bpy
    from mathutils import Vector

    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE_NEXT"
    scene.render.resolution_x = 1024
    scene.render.resolution_y = 1024
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.film_transparent = False
    if scene.world is None:
        scene.world = bpy.data.worlds.new("inspection-world")
    scene.world.color = (0.02, 0.02, 0.02)

    minimum = Vector(bounds["min_mm"])
    maximum = Vector(bounds["max_mm"])
    center = (minimum + maximum) / 2.0
    max_dimension = max(bounds["dimensions_mm"])
    distance = max(max_dimension * 2.5, 10.0)

    camera_data = bpy.data.cameras.new("inspection-camera")
    camera = bpy.data.objects.new("inspection-camera", camera_data)
    bpy.context.collection.objects.link(camera)
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = max_dimension * 1.25
    scene.camera = camera

    light_data = bpy.data.lights.new("inspection-light", type="AREA")
    light_data.energy = 1200.0
    light_data.shape = "DISK"
    light_data.size = max_dimension * 1.5
    light = bpy.data.objects.new("inspection-light", light_data)
    bpy.context.collection.objects.link(light)

    directions = {
        "front": Vector((0.0, -1.0, 0.0)),
        "back": Vector((0.0, 1.0, 0.0)),
        "left": Vector((-1.0, 0.0, 0.0)),
        "right": Vector((1.0, 0.0, 0.0)),
        "top": Vector((0.0, 0.0, 1.0)),
        "bottom": Vector((0.0, 0.0, -1.0)),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    views: dict[str, str] = {}
    for view_id, direction in directions.items():
        camera.location = center + direction * distance
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        light.location = center + direction * (distance * 0.7)
        light.rotation_euler = (center - light.location).to_track_quat("-Z", "Y").to_euler()
        destination = output_dir / f"{view_id}.png"
        scene.render.filepath = str(destination)
        bpy.ops.render.render(write_still=True)
        views[view_id] = str(destination)
    return views


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only six-view mesh inspection")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--candidate-plan", type=Path)
    payload = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else sys.argv[1:]
    return parser.parse_args(payload)


def _run_inspection(
    source: Path, output_dir: Path, candidate_plan: SplitPlan | None
) -> dict[str, Any]:
    import bpy

    source_sha = sha256_file(source)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    source_obj = _import_source(source)
    bpy.ops.object.select_all(action="DESELECT")
    source_obj.select_set(True)
    bpy.context.view_layer.objects.active = source_obj
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bounds = _bounds(source_obj)

    cut_records: list[dict[str, Any]] = []
    connector_records: list[dict[str, Any]] = []
    candidate_pieces: dict[str, Any] = {}
    if candidate_plan is not None:
        cut_records, connector_records, candidate_pieces = _measure_candidate_sequence(
            source_obj,
            candidate_plan,
        )
        source_obj.hide_render = True
        palette = (
            (0.30, 0.70, 1.00, 1.0),
            (1.00, 0.55, 0.20, 1.0),
            (0.45, 0.90, 0.45, 1.0),
            (0.95, 0.40, 0.65, 1.0),
            (0.90, 0.80, 0.25, 1.0),
            (0.65, 0.50, 1.00, 1.0),
        )
        for index, (piece_id, piece) in enumerate(sorted(candidate_pieces.items())):
            _assign_emission_material(piece, f"piece-{piece_id}", palette[index % len(palette)])
        by_cut = {record["id"]: record for record in cut_records}
        _add_overlays(candidate_plan, by_cut)
    else:
        _assign_emission_material(
            source_obj,
            "inspection-material",
            (0.55, 0.72, 0.92, 1.0),
        )

    try:
        views = _render_views(source_obj, output_dir / "inspection", bounds)
    finally:
        source_obj.hide_render = False
        for piece in tuple(candidate_pieces.values()):
            _remove_temporary_object(piece)
    unsafe = False
    if candidate_plan is not None:
        by_id = {connector.id: connector for connector in candidate_plan.connectors}
        for record in connector_records:
            connector = by_id[record["id"]]
            unsafe = unsafe or (
                record["section_outline_inside"] is not True
                or record["minimum_edge_margin_mm"]
                < connector.minimum_edge_margin_mm
                or record["estimated_minimum_wall_mm"] < connector.minimum_wall_mm
                or record["available_depth_mm"]
                < connector.engagement_mm + connector.socket_bottom_clearance_mm
            )
        unsafe = unsafe or any(
            record["section_width_mm"] <= 0 or record["section_height_mm"] <= 0
            for record in cut_records
        )

    evidence = {
        "status": (
            "needs_geometry_redesign"
            if unsafe
            else "ready_for_gate_a"
            if candidate_plan is not None
            else "ready_for_review"
        ),
        "script_version": SCRIPT_VERSION,
        "script_sha256": sha256_file(Path(__file__).resolve()),
        "blender_version": ".".join(str(value) for value in bpy.app.version),
        "source_model": str(source),
        "source_model_sha256": source_sha,
        "source_sha256_before": source_sha,
        "source_sha256_after": sha256_file(source),
        "units": "mm",
        "bounds": bounds,
        "views": views,
        "candidate_cuts": [
            {key: value for key, value in record.items() if key != "segments"}
            for record in cut_records
        ],
        "candidate_connectors": connector_records,
    }
    validate_inspection_evidence(
        evidence,
        source_sha256=source_sha,
        candidate_plan=candidate_plan,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "inspection.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return evidence


def main() -> int:
    args = _parse_args()
    source = args.source.resolve(strict=True)
    output_dir = args.output_dir.resolve()
    candidate_plan = (
        load_split_plan(args.candidate_plan.resolve(strict=True))
        if args.candidate_plan
        else None
    )
    evidence = _run_inspection(source, output_dir, candidate_plan)
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
