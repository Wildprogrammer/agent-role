from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from component_selection import (
    ComponentAssignmentRequired,
    ComponentCandidate,
    choose_target_component,
)


class BoundedCutError(ValueError):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _remove_object(obj: Any) -> None:
    import bpy

    mesh = getattr(obj, "data", None)
    bpy.data.objects.remove(obj, do_unlink=True)
    if mesh is not None and getattr(mesh, "users", 1) == 0:
        if mesh.__class__.__name__ == "Mesh":
            bpy.data.meshes.remove(mesh)
        elif mesh.__class__.__name__ == "Curve":
            bpy.data.curves.remove(mesh)


def _face_components(obj: Any) -> tuple[tuple[int, ...], ...]:
    edge_faces: dict[tuple[int, int], list[int]] = {}
    for polygon in obj.data.polygons:
        vertices = tuple(int(value) for value in polygon.vertices)
        for index, first in enumerate(vertices):
            second = vertices[(index + 1) % len(vertices)]
            edge = tuple(sorted((first, second)))
            edge_faces.setdefault(edge, []).append(int(polygon.index))

    neighbors: dict[int, set[int]] = {
        int(polygon.index): set() for polygon in obj.data.polygons
    }
    for linked in edge_faces.values():
        for face in linked:
            neighbors[face].update(item for item in linked if item != face)

    unseen = set(neighbors)
    groups: list[tuple[int, ...]] = []
    while unseen:
        start = min(unseen)
        unseen.remove(start)
        stack = [start]
        group = {start}
        while stack:
            current = stack.pop()
            for linked in neighbors[current]:
                if linked in unseen:
                    unseen.remove(linked)
                    group.add(linked)
                    stack.append(linked)
        groups.append(tuple(sorted(group)))
    return tuple(sorted(groups, key=lambda item: (-len(item), item[0])))


def mesh_component_count(obj: Any) -> int:
    return len(_face_components(obj))


def object_volume(obj: Any) -> float:
    import bmesh

    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    mesh.normal_update()
    volume = abs(float(mesh.calc_volume(signed=True)))
    mesh.free()
    return volume


def _object_bounds(obj: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    if not points:
        raise BoundedCutError(f"mesh object is empty: {obj.name}")
    minimum = tuple(min(float(point[index]) for point in points) for index in range(3))
    maximum = tuple(max(float(point[index]) for point in points) for index in range(3))
    return minimum, maximum


def _duplicate_mesh_object(obj: Any, name: str) -> Any:
    import bpy

    mesh = obj.data.copy()
    duplicate = bpy.data.objects.new(name, mesh)
    duplicate.matrix_world = obj.matrix_world.copy()
    bpy.context.collection.objects.link(duplicate)
    return duplicate


def _extract_faces(obj: Any, face_indices: tuple[int, ...], name: str) -> Any:
    import bpy

    polygons = [obj.data.polygons[index] for index in face_indices]
    vertex_ids = sorted(
        {int(vertex) for polygon in polygons for vertex in polygon.vertices}
    )
    remap = {old: new for new, old in enumerate(vertex_ids)}
    vertices = [
        tuple(float(value) for value in (obj.matrix_world @ obj.data.vertices[index].co))
        for index in vertex_ids
    ]
    faces = [
        tuple(remap[int(index)] for index in polygon.vertices)
        for polygon in polygons
    ]
    mesh = bpy.data.meshes.new(f"{name}-mesh")
    mesh.from_pydata(vertices, (), faces)
    mesh.validate(verbose=False)
    mesh.update()
    result = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(result)
    return result


def extract_component(obj: Any, candidate: ComponentCandidate, name: str) -> Any:
    return _extract_faces(obj, candidate.face_indices, name)


def _component_bvh(obj: Any) -> tuple[Any, Any]:
    import bmesh
    from mathutils.bvhtree import BVHTree

    mesh = bmesh.new()
    mesh.from_mesh(obj.data)
    mesh.normal_update()
    return mesh, BVHTree.FromBMesh(mesh)


def _ray_hit_count(bvh: Any, point: Any, direction: Any) -> int:
    origin = point.copy()
    hits = 0
    epsilon = 1e-5
    for _index in range(4096):
        location, _normal, _face, _distance = bvh.ray_cast(
            origin,
            direction,
            1_000_000.0,
        )
        if location is None:
            break
        hits += 1
        origin = location + direction * epsilon
    return hits


def _point_inside(bvh: Any, point: tuple[float, float, float]) -> bool:
    from mathutils import Vector

    origin = Vector(point)
    directions = (
        Vector((1.0, 0.371, 0.127)).normalized(),
        Vector((0.193, 1.0, 0.417)).normalized(),
        Vector((0.283, 0.229, 1.0)).normalized(),
    )
    votes = sum(_ray_hit_count(bvh, origin, direction) % 2 for direction in directions)
    return votes >= 2


def analyze_components(
    obj: Any,
    *,
    seed_points: Mapping[str, tuple[float, float, float]],
    reference_point: tuple[float, float, float],
) -> tuple[ComponentCandidate, ...]:
    from mathutils import Vector

    candidates: list[ComponentCandidate] = []
    for index, face_indices in enumerate(_face_components(obj)):
        component = _extract_faces(obj, face_indices, f"analysis-component-{index}")
        mesh, bvh = _component_bvh(component)
        try:
            nearest = bvh.find_nearest(Vector(reference_point))
            nearest_distance = (
                float(nearest[3]) if nearest is not None else math.inf
            )
            bbox_min, bbox_max = _object_bounds(component)
            candidates.append(
                ComponentCandidate(
                    index=index,
                    face_indices=face_indices,
                    face_count=len(face_indices),
                    volume_mm3=object_volume(component),
                    bbox_min=bbox_min,
                    bbox_max=bbox_max,
                    seed_hits=tuple(
                        sorted(
                            seed_id
                            for seed_id, point in seed_points.items()
                            if _point_inside(bvh, point)
                        )
                    ),
                    nearest_distance_mm=nearest_distance,
                )
            )
        finally:
            mesh.free()
            _remove_object(component)
    return tuple(candidates)


def _bisect_target_half(
    source_obj: Any,
    point_mm: tuple[float, float, float],
    target_normal: Any,
) -> Any:
    import bmesh
    import bpy
    from mathutils import Vector

    mesh = bpy.data.meshes.new("bounded-analysis-half-mesh")
    result = bpy.data.objects.new("bounded-analysis-half", mesh)
    bpy.context.collection.objects.link(result)
    data = bmesh.new()
    data.from_mesh(source_obj.data)
    bmesh.ops.bisect_plane(
        data,
        geom=list(data.verts) + list(data.edges) + list(data.faces),
        plane_co=Vector(point_mm),
        plane_no=Vector(target_normal),
        clear_inner=True,
        clear_outer=False,
        dist=0.000001,
    )
    boundary_edges = [edge for edge in data.edges if edge.is_boundary]
    if boundary_edges:
        bmesh.ops.triangle_fill(data, edges=boundary_edges, normal=Vector(target_normal))
    bmesh.ops.recalc_face_normals(data, faces=list(data.faces))
    data.to_mesh(mesh)
    mesh.update()
    data.free()
    return result


def _apply_exact_boolean(target: Any, tool: Any, operation: str) -> None:
    import bpy

    bpy.ops.object.select_all(action="DESELECT")
    target.select_set(True)
    bpy.context.view_layer.objects.active = target
    modifier = target.modifiers.new(name=f"bounded-{operation.lower()}", type="BOOLEAN")
    modifier.operation = operation
    modifier.solver = "EXACT"
    modifier.object = tool
    bpy.ops.object.modifier_apply(modifier=modifier.name)


def _emission_material(name: str, color: tuple[float, float, float, float]) -> Any:
    import bpy

    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = 0.8
    material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def _assign_material(obj: Any, name: str, color: tuple[float, float, float, float]) -> None:
    obj.data.materials.clear()
    obj.data.materials.append(_emission_material(name, color))


def render_component_diagnostic(
    source_half: Any,
    candidates: tuple[ComponentCandidate, ...],
    seed_points: Mapping[str, tuple[float, float, float]],
    destination: Path,
) -> dict[str, object]:
    import bpy
    from mathutils import Vector

    destination.parent.mkdir(parents=True, exist_ok=True)
    existing_visibility = {
        obj.name: bool(obj.hide_render) for obj in bpy.context.scene.objects
    }
    for obj in bpy.context.scene.objects:
        obj.hide_render = True

    palette = (
        (0.15, 0.65, 1.0, 1.0),
        (1.0, 0.35, 0.15, 1.0),
        (0.35, 0.9, 0.4, 1.0),
        (0.8, 0.35, 1.0, 1.0),
    )
    temporary: list[Any] = []
    minimum = [math.inf, math.inf, math.inf]
    maximum = [-math.inf, -math.inf, -math.inf]
    try:
        for candidate in candidates:
            obj = extract_component(
                source_half,
                candidate,
                f"diagnostic-component-{candidate.index}",
            )
            temporary.append(obj)
            obj.hide_render = False
            _assign_material(
                obj,
                f"diagnostic-material-{candidate.index}",
                palette[candidate.index % len(palette)],
            )
            for axis in range(3):
                minimum[axis] = min(minimum[axis], candidate.bbox_min[axis])
                maximum[axis] = max(maximum[axis], candidate.bbox_max[axis])

            text_data = bpy.data.curves.new(
                f"diagnostic-label-{candidate.index}",
                type="FONT",
            )
            text_data.body = (
                f"#{candidate.index}  {candidate.volume_mm3:.3f} mm3\n"
                f"{candidate.face_count} faces"
            )
            text_data.align_x = "CENTER"
            text_data.align_y = "CENTER"
            text_data.size = max(
                0.5,
                max(
                    candidate.bbox_max[index] - candidate.bbox_min[index]
                    for index in range(3)
                )
                * 0.08,
            )
            label = bpy.data.objects.new(
                f"diagnostic-label-{candidate.index}",
                text_data,
            )
            bpy.context.collection.objects.link(label)
            label.location = (
                (candidate.bbox_min[0] + candidate.bbox_max[0]) / 2.0,
                minimum[1] - 0.2,
                (candidate.bbox_min[2] + candidate.bbox_max[2]) / 2.0,
            )
            label.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
            label.hide_render = False
            _assign_material(label, f"diagnostic-label-material-{candidate.index}", (1, 1, 1, 1))
            temporary.append(label)

        for seed_id, point in seed_points.items():
            bpy.ops.mesh.primitive_uv_sphere_add(
                segments=16,
                ring_count=8,
                radius=max((maximum[0] - minimum[0]) * 0.015, 0.1),
                location=point,
            )
            marker = bpy.context.active_object
            marker.name = f"diagnostic-seed-{seed_id}"
            marker.hide_render = False
            _assign_material(marker, f"diagnostic-seed-material-{seed_id}", (1.0, 0.9, 0.0, 1.0))
            temporary.append(marker)

        center = Vector(tuple((minimum[i] + maximum[i]) / 2.0 for i in range(3)))
        dimensions = [maximum[index] - minimum[index] for index in range(3)]
        scale = max(dimensions[0], dimensions[2], 1.0)
        distance = max(max(dimensions) * 3.0, 10.0)
        camera_data = bpy.data.cameras.new("diagnostic-camera")
        camera = bpy.data.objects.new("diagnostic-camera", camera_data)
        bpy.context.collection.objects.link(camera)
        temporary.append(camera)
        camera.hide_render = False
        camera.location = center + Vector((0.0, -distance, 0.0))
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = scale * 1.4

        scene = bpy.context.scene
        scene.camera = camera
        scene.render.engine = "BLENDER_EEVEE_NEXT"
        scene.render.resolution_x = 1200
        scene.render.resolution_y = 900
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.film_transparent = False
        if scene.world is None:
            scene.world = bpy.data.worlds.new("diagnostic-world")
        scene.world.color = (0.015, 0.015, 0.02)
        scene.render.filepath = str(destination)
        bpy.ops.render.render(write_still=True)
    finally:
        for obj in reversed(temporary):
            if obj.name in bpy.data.objects:
                _remove_object(obj)
        for name, hidden in existing_visibility.items():
            if name in bpy.data.objects:
                bpy.data.objects[name].hide_render = hidden
    return {
        "path": str(destination),
        "sha256": _sha256_file(destination),
        "candidate_ids": [item.index for item in candidates],
        "width_px": 1200,
        "height_px": 900,
    }


def bounded_split(
    source_obj: Any,
    *,
    cut_id: str,
    point_mm: tuple[float, float, float],
    normal: tuple[float, float, float],
    target_side: str,
    seed_points: Mapping[str, tuple[float, float, float]],
    diagnostic_path: Path | None = None,
) -> tuple[Any, Any, dict[str, object]]:
    from mathutils import Vector

    if target_side not in {"negative", "positive"}:
        raise BoundedCutError("target_side must be negative or positive")
    normal_vector = Vector(normal)
    if normal_vector.length <= 1e-12:
        raise BoundedCutError("cut normal must be non-zero")
    side_normal = normal_vector.normalized()
    if target_side == "negative":
        side_normal.negate()

    temporary: list[Any] = []
    results: list[Any] = []
    try:
        temporary_half = _bisect_target_half(source_obj, point_mm, side_normal)
        temporary.append(temporary_half)
        reference = Vector(point_mm) + side_normal * 0.05
        candidates = analyze_components(
            temporary_half,
            seed_points=seed_points,
            reference_point=tuple(float(value) for value in reference),
        )
        try:
            selected = choose_target_component(
                cut_id,
                candidates,
                seed_ids=tuple(sorted(seed_points)),
            )
        except ComponentAssignmentRequired as exc:
            if diagnostic_path is not None:
                evidence = render_component_diagnostic(
                    temporary_half,
                    candidates,
                    seed_points,
                    diagnostic_path,
                )
                exc.diagnostic_path = evidence["path"]
                exc.diagnostic_sha256 = evidence["sha256"]
            raise

        selected_obj = extract_component(
            temporary_half,
            selected,
            f"{cut_id}-selected",
        )
        temporary.append(selected_obj)
        target_result = _duplicate_mesh_object(selected_obj, f"{cut_id}-target")
        remainder_result = _bisect_target_half(
            source_obj,
            point_mm,
            -side_normal,
        )
        remainder_result.name = f"{cut_id}-remainder"
        results.extend((target_result, remainder_result))
        returned_components: list[dict[str, object]] = []
        for candidate in candidates:
            if candidate.index == selected.index:
                continue
            returned = extract_component(
                temporary_half,
                candidate,
                f"{cut_id}-return-{candidate.index}",
            )
            try:
                returned_volume = object_volume(returned)
                _apply_exact_boolean(remainder_result, returned, "UNION")
                returned_components.append(
                    {
                        "candidate_index": candidate.index,
                        "volume_mm3": returned_volume,
                        "returned_to": "remainder",
                    }
                )
            finally:
                _remove_object(returned)
        if (
            not target_result.data.polygons
            or object_volume(target_result) <= 0.0
            or mesh_component_count(target_result) != 1
        ):
            raise ComponentAssignmentRequired(cut_id, candidates)
        if not remainder_result.data.polygons or object_volume(remainder_result) <= 0.0:
            raise BoundedCutError("bounded split produced an empty remainder")
        negative, positive = (
            (target_result, remainder_result)
            if target_side == "negative"
            else (remainder_result, target_result)
        )
        record = {
            "cut_id": cut_id,
            "target_side": target_side,
            "candidate_count": len(candidates),
            "selected_component": selected.index,
            "candidates": [item.to_dict() for item in candidates],
            "seed_points": {
                key: [float(value) for value in point]
                for key, point in sorted(seed_points.items())
            },
            "selected_bbox_min": list(selected.bbox_min),
            "selected_bbox_max": list(selected.bbox_max),
            "return_solver": "EXACT",
            "returned_components": returned_components,
            "operations": [
                "BISECT_SELECTED",
                "BISECT_OPPOSITE",
                "RETURN_TO_REMAINDER",
            ],
        }
        results.clear()
        return negative, positive, record
    finally:
        for obj in reversed(temporary + results):
            if obj.name in __import__("bpy").data.objects:
                _remove_object(obj)
