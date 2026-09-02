from __future__ import annotations

import math
import zipfile
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence
from urllib.parse import unquote
from xml.etree import ElementTree


IDENTITY_TRANSFORM = (
    1.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
)


class ThreeMFImportError(ValueError):
    pass


@dataclass(frozen=True)
class ThreeMFMesh:
    vertices: array
    triangles: array
    vertex_count: int
    triangle_count: int


@dataclass(frozen=True)
class _Component:
    object_id: str
    transform: tuple[float, ...]


@dataclass
class _ObjectRecord:
    vertices: array
    triangles: array
    components: list[_Component]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _model_member(archive: zipfile.ZipFile) -> str:
    try:
        relationships = ElementTree.fromstring(archive.read("_rels/.rels"))
    except (KeyError, ElementTree.ParseError) as exc:
        raise ThreeMFImportError("cannot read 3MF package relationships") from exc
    for relationship in relationships:
        if _local_name(relationship.tag) != "Relationship":
            continue
        if "3dmodel" not in relationship.attrib.get("Type", "").lower():
            continue
        target = unquote(relationship.attrib.get("Target", "")).lstrip("/")
        if target in archive.namelist():
            return target
    raise ThreeMFImportError("cannot read 3MF model relationship")


def _parse_transform(raw: str | None) -> tuple[float, ...]:
    if raw is None or not raw.strip():
        return IDENTITY_TRANSFORM
    values = raw.split()
    if len(values) != 12:
        raise ThreeMFImportError("3MF transform must contain 12 numbers")
    try:
        result = tuple(float(value) for value in values)
    except ValueError as exc:
        raise ThreeMFImportError("3MF transform must contain numbers") from exc
    if not all(math.isfinite(value) for value in result):
        raise ThreeMFImportError("3MF transform must contain finite numbers")
    return result


def _compose(
    first: Sequence[float], second: Sequence[float]
) -> tuple[float, ...]:
    linear = []
    for row in range(3):
        for column in range(3):
            linear.append(
                sum(first[row * 3 + index] * second[index * 3 + column] for index in range(3))
            )
    translation = tuple(
        sum(first[9 + index] * second[index * 3 + column] for index in range(3))
        + second[9 + column]
        for column in range(3)
    )
    return (*linear, *translation)


def _transform_vertex(
    x: float, y: float, z: float, transform: Sequence[float]
) -> tuple[float, float, float]:
    return (
        x * transform[0] + y * transform[3] + z * transform[6] + transform[9],
        x * transform[1] + y * transform[4] + z * transform[7] + transform[10],
        x * transform[2] + y * transform[5] + z * transform[8] + transform[11],
    )


def _read_model(
    archive: zipfile.ZipFile, model_member: str
) -> tuple[dict[str, _ObjectRecord], list[tuple[str, tuple[float, ...]]]]:
    objects: dict[str, _ObjectRecord] = {}
    build_items: list[tuple[str, tuple[float, ...]]] = []
    current_id: str | None = None
    current: _ObjectRecord | None = None
    unit = "millimeter"

    try:
        with archive.open(model_member) as model:
            for event, element in ElementTree.iterparse(
                model, events=("start", "end")
            ):
                tag = _local_name(element.tag)
                if event == "start":
                    if tag == "model":
                        unit = element.attrib.get("unit", "millimeter")
                    elif tag == "object":
                        if current is not None:
                            raise ThreeMFImportError("nested 3MF objects are not supported")
                        current_id = element.attrib.get("id", "")
                        current = _ObjectRecord(array("d"), array("I"), [])
                    continue

                if current is not None and tag == "vertex":
                    try:
                        coordinates = tuple(
                            float(element.attrib[axis]) for axis in ("x", "y", "z")
                        )
                    except (KeyError, ValueError) as exc:
                        raise ThreeMFImportError("invalid 3MF vertex") from exc
                    if not all(math.isfinite(value) for value in coordinates):
                        raise ThreeMFImportError("3MF vertex must be finite")
                    current.vertices.extend(coordinates)
                elif current is not None and tag == "triangle":
                    try:
                        indices = tuple(
                            int(element.attrib[index]) for index in ("v1", "v2", "v3")
                        )
                    except (KeyError, ValueError, OverflowError) as exc:
                        raise ThreeMFImportError("invalid 3MF triangle") from exc
                    if any(index < 0 for index in indices):
                        raise ThreeMFImportError("3MF triangle indices must be non-negative")
                    current.triangles.extend(indices)
                elif current is not None and tag == "component":
                    current.components.append(
                        _Component(
                            object_id=element.attrib.get("objectid", ""),
                            transform=_parse_transform(element.attrib.get("transform")),
                        )
                    )
                elif tag == "object":
                    if current is None or not current_id or current_id in objects:
                        raise ThreeMFImportError("3MF object ids must be unique")
                    has_mesh = bool(current.vertices or current.triangles)
                    has_components = bool(current.components)
                    if has_mesh == has_components:
                        raise ThreeMFImportError(
                            "3MF object must contain one mesh or components"
                        )
                    if has_mesh:
                        if not current.vertices or not current.triangles:
                            raise ThreeMFImportError("3MF mesh must not be empty")
                        vertex_count = len(current.vertices) // 3
                        if len(current.vertices) % 3 or len(current.triangles) % 3:
                            raise ThreeMFImportError("invalid 3MF mesh arrays")
                        if max(current.triangles) >= vertex_count:
                            raise ThreeMFImportError(
                                "3MF triangle references an unknown vertex"
                            )
                    objects[current_id] = current
                    current_id = None
                    current = None
                elif tag == "item":
                    build_items.append(
                        (
                            element.attrib.get("objectid", ""),
                            _parse_transform(element.attrib.get("transform")),
                        )
                    )
                element.clear()
    except (KeyError, ElementTree.ParseError) as exc:
        raise ThreeMFImportError("cannot read 3MF model XML") from exc

    if unit != "millimeter":
        raise ThreeMFImportError("3MF source unit must be millimeter")
    if not build_items:
        raise ThreeMFImportError("3MF source requires at least one build item")
    return objects, build_items


def _mesh_instances(
    objects: dict[str, _ObjectRecord],
    object_id: str,
    transform: tuple[float, ...],
    ancestors: frozenset[str],
) -> Iterator[tuple[_ObjectRecord, tuple[float, ...]]]:
    if object_id not in objects:
        raise ThreeMFImportError(f"component references unknown object {object_id}")
    if object_id in ancestors:
        raise ThreeMFImportError("cyclic 3MF component graph")
    record = objects[object_id]
    if record.vertices:
        yield record, transform
        return
    next_ancestors = ancestors | {object_id}
    for component in record.components:
        yield from _mesh_instances(
            objects,
            component.object_id,
            _compose(component.transform, transform),
            next_ancestors,
        )


def load_3mf_mesh(package_path: Path) -> ThreeMFMesh:
    if not package_path.is_file():
        raise ThreeMFImportError("cannot read missing 3MF package")
    try:
        with zipfile.ZipFile(package_path) as archive:
            objects, build_items = _read_model(archive, _model_member(archive))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ThreeMFImportError(f"cannot read 3MF package: {exc}") from exc

    vertices = array("d")
    triangles = array("I")
    for object_id, build_transform in build_items:
        for record, transform in _mesh_instances(
            objects,
            object_id,
            build_transform,
            frozenset(),
        ):
            vertex_offset = len(vertices) // 3
            for index in range(0, len(record.vertices), 3):
                vertices.extend(
                    _transform_vertex(
                        record.vertices[index],
                        record.vertices[index + 1],
                        record.vertices[index + 2],
                        transform,
                    )
                )
            triangles.extend(index + vertex_offset for index in record.triangles)

    if not vertices or not triangles:
        raise ThreeMFImportError("3MF build produced no mesh")
    return ThreeMFMesh(
        vertices=vertices,
        triangles=triangles,
        vertex_count=len(vertices) // 3,
        triangle_count=len(triangles) // 3,
    )
