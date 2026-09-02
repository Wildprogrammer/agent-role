from __future__ import annotations

import hashlib
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote
from xml.etree import ElementTree
from xml.sax.saxutils import quoteattr


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


class Standard3MFError(ValueError):
    pass


@dataclass(frozen=True)
class Standard3MFVerification:
    valid: bool
    package_path: str
    sha256: str
    object_names: tuple[str, ...]
    transforms: Mapping[str, tuple[float, ...]]
    gcode_members: tuple[str, ...]


@dataclass(frozen=True)
class MeshPayload:
    vertices: Iterable[Sequence[float]]
    triangles: Iterable[Sequence[int]]
    transform: Sequence[float] = IDENTITY_TRANSFORM


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write(handle, value: str) -> None:
    handle.write(value.encode("utf-8"))


def _finite_vector(value: Sequence[float], length: int, label: str) -> tuple[float, ...]:
    if len(value) != length:
        raise Standard3MFError(f"{label} must contain {length} values")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise Standard3MFError(f"{label} must contain finite values")
    return result


def write_standard_3mf(
    destination: Path, meshes: Mapping[str, MeshPayload]
) -> Path:
    if not meshes:
        raise Standard3MFError("standard 3MF requires at least one mesh")
    names = tuple(sorted(meshes))
    if any(not isinstance(name, str) or not name.strip() for name in names):
        raise Standard3MFError("mesh names must be non-empty strings")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.tmp")
    temporary.unlink(missing_ok=True)
    content_types = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Override PartName="/3D/3dmodel.model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>"""
    relationships = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>"""
    transforms: dict[str, tuple[float, ...]] = {}
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", relationships)
            with archive.open("3D/3dmodel.model", "w", force_zip64=True) as model:
                _write(
                    model,
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<model unit="millimeter" '
                    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">\n'
                    "<resources>\n",
                )
                for object_id, name in enumerate(names, start=1):
                    payload = meshes[name]
                    transform = _finite_vector(payload.transform, 12, "transform")
                    transforms[name] = transform
                    _write(
                        model,
                        f'<object id="{object_id}" name={quoteattr(name)} type="model">'
                        "<mesh><vertices>\n",
                    )
                    vertex_count = 0
                    for vertex in payload.vertices:
                        x_value, y_value, z_value = _finite_vector(
                            vertex, 3, f"vertex for {name}"
                        )
                        _write(
                            model,
                            f'<vertex x="{x_value:.17g}" y="{y_value:.17g}" '
                            f'z="{z_value:.17g}"/>\n',
                        )
                        vertex_count += 1
                    if vertex_count == 0:
                        raise Standard3MFError(f"empty mesh for object {name}")
                    _write(model, "</vertices><triangles>\n")
                    triangle_count = 0
                    for triangle in payload.triangles:
                        if len(triangle) != 3:
                            raise Standard3MFError(
                                f"triangle for {name} must contain three indices"
                            )
                        indices = tuple(int(value) for value in triangle)
                        if any(index < 0 or index >= vertex_count for index in indices):
                            raise Standard3MFError(
                                f"triangle for {name} references an invalid vertex"
                            )
                        _write(
                            model,
                            f'<triangle v1="{indices[0]}" v2="{indices[1]}" '
                            f'v3="{indices[2]}"/>\n',
                        )
                        triangle_count += 1
                    if triangle_count == 0:
                        raise Standard3MFError(f"empty mesh for object {name}")
                    _write(model, "</triangles></mesh></object>\n")
                _write(model, "</resources><build>\n")
                for object_id, name in enumerate(names, start=1):
                    transform = " ".join(f"{value:.17g}" for value in transforms[name])
                    _write(
                        model,
                        f'<item objectid="{object_id}" transform="{transform}"/>\n',
                    )
                _write(model, "</build></model>")
        temporary.replace(destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _model_member(archive: zipfile.ZipFile) -> str:
    try:
        relationships = ElementTree.fromstring(archive.read("_rels/.rels"))
    except (KeyError, ElementTree.ParseError) as exc:
        raise Standard3MFError("cannot read 3MF package relationships") from exc
    for relationship in relationships:
        if _local_name(relationship.tag) != "Relationship":
            continue
        if "3dmodel" not in relationship.attrib.get("Type", "").lower():
            continue
        target = unquote(relationship.attrib.get("Target", "")).lstrip("/")
        if target in archive.namelist():
            return target
    raise Standard3MFError("cannot read 3MF model relationship")


def _parse_transform(raw: str | None) -> tuple[float, ...]:
    if raw is None or not raw.strip():
        return IDENTITY_TRANSFORM
    parts = raw.split()
    if len(parts) != 12:
        raise Standard3MFError("build transform must contain 12 numbers")
    try:
        transform = tuple(float(value) for value in parts)
    except ValueError as exc:
        raise Standard3MFError("build transform must contain numbers") from exc
    if not all(math.isfinite(value) for value in transform):
        raise Standard3MFError("build transform must contain finite numbers")
    return transform


def _transforms_match(first: Sequence[float], second: Sequence[float]) -> bool:
    return len(first) == len(second) and all(
        math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
        for a, b in zip(first, second)
    )


def _stream_model_structure(
    archive: zipfile.ZipFile,
    model_member: str,
) -> tuple[str, dict[str, str], list[tuple[str, tuple[float, ...]]]]:
    unit = "millimeter"
    objects_by_id: dict[str, str] = {}
    build_items: list[tuple[str, tuple[float, ...]]] = []
    current_object: dict[str, Any] | None = None

    try:
        with archive.open(model_member) as model:
            for event, element in ElementTree.iterparse(
                model,
                events=("start", "end"),
            ):
                name = _local_name(element.tag)
                if event == "start":
                    if name == "model":
                        unit = element.attrib.get("unit", "millimeter")
                    elif name == "object":
                        if current_object is not None:
                            raise Standard3MFError("nested 3MF objects are not supported")
                        current_object = {
                            "id": element.attrib.get("id", ""),
                            "name": element.attrib.get("name", "").strip(),
                            "vertices": 0,
                            "triangles": 0,
                        }
                    elif current_object is not None and name == "vertex":
                        current_object["vertices"] += 1
                    elif current_object is not None and name == "triangle":
                        current_object["triangles"] += 1
                    continue

                if current_object is not None and name == "metadata":
                    if (
                        not current_object["name"]
                        and element.attrib.get("name", "").lower() == "name"
                        and element.text
                    ):
                        current_object["name"] = element.text.strip()
                elif name == "object":
                    if current_object is None:
                        raise Standard3MFError("cannot read 3MF object")
                    object_id = current_object["id"]
                    object_name = current_object["name"]
                    if not object_id or not object_name or object_id in objects_by_id:
                        raise Standard3MFError("object id and name must be unique")
                    if (
                        current_object["vertices"] == 0
                        or current_object["triangles"] == 0
                    ):
                        raise Standard3MFError(f"empty mesh for object {object_name}")
                    objects_by_id[object_id] = object_name
                    current_object = None
                elif name == "item":
                    build_items.append(
                        (
                            element.attrib.get("objectid", ""),
                            _parse_transform(element.attrib.get("transform")),
                        )
                    )
                element.clear()
    except (KeyError, ElementTree.ParseError) as exc:
        raise Standard3MFError("cannot read 3MF model XML") from exc

    return unit, objects_by_id, build_items


def verify_standard_3mf(
    package_path: Path,
    *,
    expected_piece_ids: Sequence[str],
    expected_transforms: Mapping[str, Sequence[float]],
    expected_sha256: str | None = None,
) -> Standard3MFVerification:
    if not package_path.is_file():
        raise Standard3MFError("cannot read missing 3MF package")
    digest = sha256_file(package_path)
    if expected_sha256 is not None and digest.lower() != expected_sha256.lower():
        raise Standard3MFError("package hash mismatch")
    expected_names = tuple(sorted(str(name) for name in expected_piece_ids))
    if len(expected_names) != len(set(expected_names)):
        raise Standard3MFError("expected object names must be unique")
    if set(expected_transforms) != set(expected_names):
        raise Standard3MFError("expected transform mapping does not match objects")

    try:
        with zipfile.ZipFile(package_path) as archive:
            members = tuple(archive.namelist())
            gcode_members = tuple(
                sorted(member for member in members if "gcode" in member.lower())
            )
            if gcode_members:
                raise Standard3MFError("standard 3MF must not contain G-code")
            model_member = _model_member(archive)
            unit, objects_by_id, build_items = _stream_model_structure(
                archive,
                model_member,
            )
    except (zipfile.BadZipFile, OSError) as exc:
        raise Standard3MFError(f"cannot read 3MF package: {exc}") from exc

    if unit != "millimeter":
        raise Standard3MFError("standard 3MF unit must be millimeter")
    object_names = tuple(sorted(objects_by_id.values()))
    if object_names != expected_names:
        raise Standard3MFError("object names do not match expected pieces")

    transforms: dict[str, tuple[float, ...]] = {}
    for object_id, transform in build_items:
        if object_id not in objects_by_id:
            raise Standard3MFError("build item references an unknown object")
        name = objects_by_id[object_id]
        if name in transforms:
            raise Standard3MFError("build contains a duplicate object")
        transforms[name] = transform
    if set(transforms) != set(expected_names):
        raise Standard3MFError("build must contain every object exactly once")
    for name, actual in transforms.items():
        if not _transforms_match(actual, expected_transforms[name]):
            raise Standard3MFError(f"build transform mismatch for {name}")

    return Standard3MFVerification(
        valid=True,
        package_path=str(package_path),
        sha256=digest,
        object_names=object_names,
        transforms=transforms,
        gcode_members=(),
    )
