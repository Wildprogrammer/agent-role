from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile


class BambuPackageError(ValueError):
    pass


@dataclass(frozen=True)
class BambuProfile:
    printer_model: str
    nozzle_mm: float
    print_settings_id: str
    build_volume_mm: tuple[float, float]


@dataclass(frozen=True)
class PackageVerification:
    valid: bool
    package_path: str
    gcode_member: str
    gcode_sha256: str
    gcode_md5: str
    object_names: tuple[str, ...]
    bbox_all: tuple[float, float, float, float]
    printer_model: str
    nozzle_mm: float
    print_settings_id: str


def _header(text: str, key: str) -> str:
    match = re.search(
        rf"^;\s*{re.escape(key)}\s*=\s*(.+?)\s*$",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        raise BambuPackageError(f"missing G-code header: {key}")
    return match.group(1)


def _float(value: Any, label: str) -> float:
    if isinstance(value, bool) or value is None:
        raise BambuPackageError(f"{label} must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise BambuPackageError(f"{label} must be numeric") from exc
    if not math.isfinite(result):
        raise BambuPackageError(f"{label} must be finite")
    return result


def verify_single_plate_package(
    package: Path,
    *,
    expected_piece_ids: tuple[str, ...],
    profile: BambuProfile,
) -> PackageVerification:
    if not package.name.lower().endswith(".gcode.3mf"):
        raise BambuPackageError("package must use .gcode.3mf format")
    try:
        archive = ZipFile(package)
    except (OSError, BadZipFile) as exc:
        raise BambuPackageError(f"cannot read package: {exc}") from exc
    with archive:
        archive_names = archive.namelist()
        gcode_members = sorted(
            name
            for name in archive_names
            if re.fullmatch(r"Metadata/plate_\d+\.gcode", name)
        )
        if gcode_members != ["Metadata/plate_1.gcode"]:
            raise BambuPackageError("package must contain exactly one plate_1.gcode")
        gcode_member = gcode_members[0]
        md5_member = "Metadata/plate_1.gcode.md5"
        json_member = "Metadata/plate_1.json"
        if archive_names.count(md5_member) != 1:
            raise BambuPackageError("missing gcode md5 sidecar")
        if archive_names.count(json_member) != 1:
            raise BambuPackageError("missing plate metadata json")
        gcode = archive.read(gcode_member)
        try:
            declared_md5 = archive.read(md5_member).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise BambuPackageError("invalid gcode md5 sidecar") from exc
        actual_md5 = hashlib.md5(gcode).hexdigest().upper()
        if declared_md5.upper() != actual_md5:
            raise BambuPackageError("G-code MD5 mismatch")

        try:
            metadata = json.loads(archive.read(json_member).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BambuPackageError("invalid plate metadata json") from exc
        text = gcode.decode("utf-8", errors="replace")
        printer_model = _header(text, "printer_model")
        nozzle_mm = _float(_header(text, "nozzle_diameter"), "G-code nozzle")
        print_settings_id = _header(text, "print_settings_id")
        if printer_model != profile.printer_model:
            raise BambuPackageError("printer_model mismatch")
        if abs(nozzle_mm - profile.nozzle_mm) > 1e-6:
            raise BambuPackageError("nozzle mismatch")
        if print_settings_id != profile.print_settings_id:
            raise BambuPackageError("print_settings_id mismatch")

        metadata_nozzle = _float(
            metadata.get("nozzle_diameter"), "metadata nozzle"
        )
        if abs(metadata_nozzle - profile.nozzle_mm) > 1e-6:
            raise BambuPackageError("metadata nozzle mismatch")
        raw_bbox = metadata.get("bbox_all")
        if (
            not isinstance(raw_bbox, list)
            or len(raw_bbox) != 4
            or any(isinstance(value, bool) for value in raw_bbox)
        ):
            raise BambuPackageError("bbox_all must contain four numbers")
        bbox = tuple(_float(value, "bbox_all") for value in raw_bbox)
        min_x, min_y, max_x, max_y = bbox
        if (
            min_x < 0
            or min_y < 0
            or max_x <= min_x
            or max_y <= min_y
            or max_x > profile.build_volume_mm[0]
            or max_y > profile.build_volume_mm[1]
        ):
            raise BambuPackageError("bbox_all exceeds build volume")
        objects = metadata.get("bbox_objects")
        if not isinstance(objects, list):
            raise BambuPackageError("bbox_objects is missing")
        object_names = tuple(
            sorted(
                item.get("name")
                for item in objects
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            )
        )
        if object_names != tuple(sorted(expected_piece_ids)):
            raise BambuPackageError("object mapping mismatch")

    return PackageVerification(
        valid=True,
        package_path=str(package),
        gcode_member=gcode_member,
        gcode_sha256=hashlib.sha256(gcode).hexdigest(),
        gcode_md5=actual_md5,
        object_names=object_names,
        bbox_all=bbox,
        printer_model=printer_model,
        nozzle_mm=nozzle_mm,
        print_settings_id=print_settings_id,
    )
