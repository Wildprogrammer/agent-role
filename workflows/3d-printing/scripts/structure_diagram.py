from __future__ import annotations

import hashlib
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


class StructureDiagramError(ValueError):
    pass


def exploded_offsets(
    piece_ids: Sequence[str],
    spacing_mm: float,
) -> dict[str, tuple[float, float, float]]:
    ids = tuple(piece_ids)
    if len(ids) < 2:
        raise StructureDiagramError("at least two piece ids are required")
    if len(set(ids)) != len(ids):
        raise StructureDiagramError("piece ids must be unique")
    if spacing_mm <= 0:
        raise StructureDiagramError("explosion spacing must be positive")

    monthly_cat_ids = {
        "body",
        "head",
        "arm-left",
        "arm-right",
        "leg-left",
        "leg-right",
    }
    if set(ids) == monthly_cat_ids and len(ids) == len(monthly_cat_ids):
        return {
            "body": (0.0, 0.0, 0.0),
            "head": (0.0, 0.0, spacing_mm),
            "arm-left": (-spacing_mm, 0.0, spacing_mm * 0.1),
            "arm-right": (spacing_mm, 0.0, spacing_mm * 0.1),
            "leg-left": (-spacing_mm * 0.35, 0.0, -spacing_mm),
            "leg-right": (spacing_mm * 0.35, 0.0, -spacing_mm),
        }

    anchor = "body" if "body" in ids else ids[0]
    remaining = sorted(piece_id for piece_id in ids if piece_id != anchor)
    offsets = {anchor: (0.0, 0.0, 0.0)}
    for index, piece_id in enumerate(remaining):
        angle = math.pi / 2.0 + (2.0 * math.pi * index / len(remaining))
        offsets[piece_id] = (
            round(math.cos(angle) * spacing_mm, 8),
            0.0,
            round(math.sin(angle) * spacing_mm, 8),
        )
    return offsets


def find_cjk_font() -> Path:
    candidates = (
        os.environ.get("AGENT_ROLE_CJK_FONT"),
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/System/Library/Fonts/PingFang.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    )
    for raw in candidates:
        if raw and Path(raw).is_file():
            return Path(raw)
    raise StructureDiagramError("needs_font_support: CJK font not found")


def piece_label(piece_id: str) -> str:
    names = {
        "body": "身体（含尾巴）",
        "head": "头部",
        "arm-left": "左臂",
        "arm-right": "右臂",
        "leg-left": "左腿",
        "leg-right": "右腿",
    }
    name = names.get(piece_id, piece_id)
    return f"{name}\npiece-{piece_id}.stl"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _remove_object(obj: Any) -> None:
    import bpy

    data = getattr(obj, "data", None)
    bpy.data.objects.remove(obj, do_unlink=True)
    if data is None or getattr(data, "users", 1) != 0:
        return
    data_type = data.__class__.__name__
    if data_type == "Mesh":
        bpy.data.meshes.remove(data)
    elif data_type == "Curve":
        bpy.data.curves.remove(data)
    elif data_type == "Camera":
        bpy.data.cameras.remove(data)


def _emission_material(
    name: str,
    color: tuple[float, float, float, float],
) -> Any:
    import bpy

    material = bpy.data.materials.new(name)
    material.diffuse_color = color
    material.use_nodes = True
    nodes = material.node_tree.nodes
    nodes.clear()
    output = nodes.new("ShaderNodeOutputMaterial")
    emission = nodes.new("ShaderNodeEmission")
    emission.inputs["Color"].default_value = color
    emission.inputs["Strength"].default_value = 0.85
    material.node_tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def _assign_material(
    obj: Any,
    name: str,
    color: tuple[float, float, float, float],
) -> None:
    obj.data.materials.clear()
    obj.data.materials.append(_emission_material(name, color))


def _world_bounds(
    objects: Sequence[Any],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    import bpy
    from mathutils import Vector

    depsgraph = bpy.context.evaluated_depsgraph_get()
    points = [
        evaluated.matrix_world @ Vector(corner)
        for obj in objects
        if obj.type != "CAMERA"
        for evaluated in (obj.evaluated_get(depsgraph),)
        for corner in evaluated.bound_box
    ]
    if not points:
        raise StructureDiagramError("structure diagram has no bounded content")
    minimum = tuple(min(float(point[index]) for point in points) for index in range(3))
    maximum = tuple(max(float(point[index]) for point in points) for index in range(3))
    return minimum, maximum


def _object_center(obj: Any) -> Any:
    from mathutils import Vector

    minimum, maximum = _world_bounds((obj,))
    return Vector(tuple((minimum[index] + maximum[index]) / 2.0 for index in range(3)))


def _explosion_spacing(pieces: Mapping[str, Any]) -> float:
    minimum, maximum = _world_bounds(tuple(pieces.values()))
    return max(maximum[index] - minimum[index] for index in range(3)) * 0.72


def _copy_pieces(
    pieces: Mapping[str, Any],
    offsets: Mapping[str, tuple[float, float, float]],
) -> dict[str, Any]:
    import bpy
    from mathutils import Vector

    palette = {
        "body": (0.35, 0.72, 1.0, 1.0),
        "head": (1.0, 0.62, 0.23, 1.0),
        "arm-left": (0.45, 0.9, 0.5, 1.0),
        "arm-right": (0.95, 0.42, 0.62, 1.0),
        "leg-left": (0.74, 0.55, 1.0, 1.0),
        "leg-right": (1.0, 0.84, 0.25, 1.0),
    }
    fallback_palette = (
        (0.35, 0.72, 1.0, 1.0),
        (1.0, 0.62, 0.23, 1.0),
        (0.45, 0.9, 0.5, 1.0),
        (0.95, 0.42, 0.62, 1.0),
        (0.74, 0.55, 1.0, 1.0),
        (1.0, 0.84, 0.25, 1.0),
    )
    fallback_indexes = {
        piece_id: index for index, piece_id in enumerate(sorted(pieces))
    }
    copies: dict[str, Any] = {}
    for piece_id, source in pieces.items():
        mesh = source.data.copy()
        obj = bpy.data.objects.new(f"diagram-{piece_id}", mesh)
        obj.matrix_world = source.matrix_world.copy()
        obj.location += Vector(offsets[piece_id])
        bpy.context.collection.objects.link(obj)
        color = palette.get(
            piece_id,
            fallback_palette[fallback_indexes[piece_id] % len(fallback_palette)],
        )
        _assign_material(obj, f"diagram-material-{piece_id}", color)
        copies[piece_id] = obj
    return copies


def _add_arrow(name: str, start: Any, end: Any, scale: float) -> list[Any]:
    import bpy
    from mathutils import Vector

    direction = Vector(end) - Vector(start)
    length = direction.length
    if length <= 1e-6:
        return []
    direction.normalize()
    color = (0.95, 0.95, 0.98, 1.0)
    curve = bpy.data.curves.new(f"{name}-curve", type="CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = max(scale * 0.006, 0.15)
    curve.bevel_resolution = 3
    spline = curve.splines.new("POLY")
    spline.points.add(1)
    spline.points[0].co = (*start, 1.0)
    spline.points[1].co = (*end, 1.0)
    line = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(line)
    _assign_material(line, f"{name}-material", color)

    head_length = min(max(scale * 0.05, 0.8), length * 0.25)
    bpy.ops.mesh.primitive_cone_add(
        vertices=20,
        radius1=head_length * 0.35,
        radius2=0.0,
        depth=head_length,
        location=Vector(end) - direction * head_length / 2.0,
    )
    head = bpy.context.active_object
    head.name = f"{name}-head"
    head.rotation_euler = direction.to_track_quat("Z", "Y").to_euler()
    _assign_material(head, f"{name}-head-material", color)
    return [line, head]


def _add_text(
    name: str,
    body: str,
    location: tuple[float, float, float],
    font: Any,
    size: float,
    *,
    align_x: str = "CENTER",
) -> Any:
    import bpy

    text = bpy.data.curves.new(name, type="FONT")
    text.body = body
    text.font = font
    text.align_x = align_x
    text.align_y = "CENTER"
    text.size = size
    text.space_line = 1.15
    obj = bpy.data.objects.new(name, text)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    obj.rotation_euler = (math.pi / 2.0, 0.0, 0.0)
    _assign_material(obj, f"{name}-material", (1.0, 1.0, 1.0, 1.0))
    return obj


def render_structure_diagram(
    pieces: Mapping[str, Any],
    connectors: Sequence[Any],
    destination: Path,
) -> dict[str, object]:
    import bpy
    from mathutils import Vector

    piece_ids = tuple(pieces)
    spacing = _explosion_spacing(pieces)
    offsets = exploded_offsets(piece_ids, spacing)
    font_path = find_cjk_font()
    font = bpy.data.fonts.load(str(font_path), check_existing=True)
    visibility = {obj.name: bool(obj.hide_render) for obj in bpy.context.scene.objects}
    for obj in bpy.context.scene.objects:
        obj.hide_render = True

    temporary: list[Any] = []
    copies: dict[str, Any] = {}
    try:
        copies = _copy_pieces(pieces, offsets)
        temporary.extend(copies.values())
        for obj in copies.values():
            obj.hide_render = False
        bpy.context.view_layer.update()

        exploded_min, exploded_max = _world_bounds(tuple(copies.values()))
        y_front = exploded_min[1] - max(spacing * 0.08, 1.0)
        label_size = max(spacing * 0.045, 1.2)
        label_nudges = {
            "body": (spacing * 0.34, 0.0),
            "head": (0.0, spacing * 0.24),
            "arm-left": (-spacing * 0.22, spacing * 0.08),
            "arm-right": (spacing * 0.22, spacing * 0.08),
            "leg-left": (-spacing * 0.16, -spacing * 0.2),
            "leg-right": (spacing * 0.16, -spacing * 0.2),
        }
        for piece_id, obj in copies.items():
            exploded_center = _object_center(obj)
            original_center = _object_center(pieces[piece_id])
            if piece_id != "body":
                arrow_objects = _add_arrow(
                    f"diagram-arrow-{piece_id}",
                    exploded_center,
                    original_center,
                    spacing,
                )
                for arrow in arrow_objects:
                    arrow.hide_render = False
                temporary.extend(arrow_objects)

            connector_ids = sorted(
                connector.id
                for connector in connectors
                if connector.male_piece == piece_id
            )
            if piece_id == "body":
                connector_text = f"母孔：{len(connectors)} 组（编号见各模块）"
            else:
                connector_text = "插销：" + ", ".join(connector_ids)
            nudge_x, nudge_z = label_nudges.get(piece_id, (0.0, 0.0))
            label = _add_text(
                f"diagram-label-{piece_id}",
                f"{piece_label(piece_id)}\n{connector_text}",
                (
                    float(exploded_center.x + nudge_x),
                    y_front,
                    float(exploded_center.z + nudge_z),
                ),
                font,
                label_size,
            )
            label.hide_render = False
            temporary.append(label)

        center = Vector(
            tuple((exploded_min[index] + exploded_max[index]) / 2.0 for index in range(3))
        )
        title = _add_text(
            "diagram-title",
            "模块装配结构图",
            (
                float(center.x),
                y_front,
                float(exploded_max[2] + spacing * 0.43),
            ),
            font,
            label_size * 1.35,
        )
        title.hide_render = False
        temporary.append(title)

        bpy.context.view_layer.update()
        content_min, content_max = _world_bounds(tuple(temporary))
        center = Vector(
            tuple((content_min[index] + content_max[index]) / 2.0 for index in range(3))
        )
        diagram_width = max(content_max[0] - content_min[0], 1.0)
        diagram_height = max(content_max[2] - content_min[2], 1.0)
        camera_data = bpy.data.cameras.new("structure-diagram-camera")
        camera = bpy.data.objects.new("structure-diagram-camera", camera_data)
        bpy.context.collection.objects.link(camera)
        temporary.append(camera)
        distance = max(diagram_width, diagram_height) * 2.5
        camera.location = center + Vector((0.0, -distance, 0.0))
        camera.rotation_euler = (center - camera.location).to_track_quat("-Z", "Y").to_euler()
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = (
            max(diagram_width, diagram_height * (1800 / 1400)) * 1.1
        )

        scene = bpy.context.scene
        scene.camera = camera
        scene.render.engine = "BLENDER_EEVEE_NEXT"
        scene.render.resolution_x = 1800
        scene.render.resolution_y = 1400
        scene.render.resolution_percentage = 100
        scene.render.image_settings.file_format = "PNG"
        scene.render.film_transparent = False
        if scene.world is None:
            scene.world = bpy.data.worlds.new("structure-diagram-world")
        scene.world.color = (0.02, 0.025, 0.035)
        destination.parent.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(destination)
        bpy.ops.render.render(write_still=True)

        image = bpy.data.images.load(str(destination), check_existing=False)
        try:
            width_px, height_px = (int(image.size[0]), int(image.size[1]))
        finally:
            bpy.data.images.remove(image)
        if (width_px, height_px) != (1800, 1400):
            raise StructureDiagramError("structure diagram dimensions are invalid")
        return {
            "path": str(destination),
            "sha256": _sha256_file(destination),
            "width_px": width_px,
            "height_px": height_px,
            "piece_ids": sorted(pieces),
            "connector_ids": sorted(connector.id for connector in connectors),
            "font_path": str(font_path),
            "labels": {piece_id: piece_label(piece_id) for piece_id in pieces},
            "debug_layout": {
                "spacing_mm": spacing,
                "exploded_min": list(exploded_min),
                "exploded_max": list(exploded_max),
                "content_min": list(content_min),
                "content_max": list(content_max),
                "centers": {
                    piece_id: [float(value) for value in _object_center(obj)]
                    for piece_id, obj in copies.items()
                },
                "camera_center": [float(value) for value in center],
                "camera_ortho_scale": float(camera_data.ortho_scale),
            },
        }
    finally:
        for obj in reversed(temporary):
            if obj.name in bpy.data.objects:
                _remove_object(obj)
        for name, hidden in visibility.items():
            if name in bpy.data.objects:
                bpy.data.objects[name].hide_render = hidden
