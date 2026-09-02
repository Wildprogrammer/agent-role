from __future__ import annotations

import dataclasses
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from bambu_package import PackageVerification
from headless_cut import CutEvidenceError, local_script_hashes, validate_cut_evidence
from standard_3mf import (
    Standard3MFVerification,
    sha256_file as standard_sha256_file,
)


class StageError(ValueError):
    pass


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(key, default)
    return getattr(item, key, default)


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: _jsonable(item) for key, item in dataclasses.asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _require_sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise StageError(f"{label} must be a SHA-256 digest")
    return value.lower()


def _png_dimensions(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or int.from_bytes(header[8:12], "big") != 13
        or header[12:16] != b"IHDR"
    ):
        raise StageError("structure diagram must be a PNG with an IHDR header")
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    if width <= 0 or height <= 0:
        raise StageError("structure diagram dimensions are invalid")
    return width, height


@dataclass
class SplitRunManifest:
    run_id: str
    status: str = "in_progress"
    split_requested: bool = False
    split_reason: str | None = None
    plan: Any | None = None
    source_model_sha256: str | None = None
    cut_script_sha256: str | None = None
    cut_evidence: Any | None = None
    geometry_validation_path: str | None = None
    geometry_validation_passed: bool = False
    delivery_target: str | None = None
    standard_stl_hashes: dict[str, str] = field(default_factory=dict)
    standard_3mf: Any | None = None
    structure_diagram: Any | None = None
    bambu_provider: Any | None = None
    plate_packages: dict[str, Any] = field(default_factory=dict)
    final_review_confirmed: bool = False
    user_review_status: str = "pending"
    printer_started: bool = False
    events: list[str] = field(default_factory=list)

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise StageError(message)

    def _failure(self, status: str, message: str) -> None:
        self.status = status
        self.events.append(f"{status}:{message}")
        raise StageError(message)

    def record_explicit_split_request(self, reason: str) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(not self.split_requested, "split request already recorded")
        self._require(
            isinstance(reason, str) and bool(reason.strip()),
            "split reason is required",
        )
        self.split_requested = True
        self.split_reason = reason.strip()
        self.events.append("split-request-confirmed")

    def confirm_split_plan(self, plan: Any, *, source_sha256: str) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(self.split_requested, "explicit split request required")
        self._require(
            hasattr(plan, "cuts")
            and hasattr(plan, "plates")
            and hasattr(plan, "leaf_piece_ids")
            and hasattr(plan, "reason")
            and hasattr(plan, "source_model")
            and bool(plan.cuts)
            and bool(plan.plates)
            and bool(plan.leaf_piece_ids),
            "validated split plan required",
        )
        self._require(plan.reason == self.split_reason, "split reason does not match plan")
        self._require(not self.plan, "split plan already recorded")
        self.plan = plan
        self.source_model_sha256 = _require_sha(source_sha256, "source_model_sha256")
        self.events.append("gate-a-split-plan-confirmed")

    def record_cut_evidence(self, evidence: Any) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(self.plan is not None, "split plan required")
        self._require(self.cut_evidence is None, "cut evidence already recorded")
        source_sha = _value(evidence, "source_model_sha256")
        if source_sha is None:
            source_sha = _value(evidence, "source_sha256_before")
        if source_sha != self.source_model_sha256:
            self._failure("needs_geometry_repair", "source hash mismatch")

        script_sha = _value(evidence, "script_sha256")
        try:
            self.cut_script_sha256 = _require_sha(script_sha, "script_sha256")
        except StageError as exc:
            self._failure("needs_geometry_repair", str(exc))
        expected_cut_ids = [cut.id for cut in self.plan.cuts]
        if list(_value(evidence, "cut_ids", [])) != expected_cut_ids:
            self._failure("needs_geometry_repair", "cut sequence mismatch")

        piece_hashes = _value(evidence, "piece_hashes", {})
        if set(piece_hashes) != set(self.plan.leaf_piece_ids):
            self._failure("needs_geometry_repair", "piece hash set mismatch")
        for piece_id, digest in piece_hashes.items():
            try:
                _require_sha(digest, f"piece hash for {piece_id}")
            except StageError as exc:
                self._failure("needs_geometry_repair", str(exc))

        validation = _value(evidence, "validation", {})
        required_flags = (
            "source_unchanged",
            "piece_count",
            "all_piece_files_nonempty",
        )
        if any(_value(validation, flag) is not True for flag in required_flags):
            self._failure("needs_geometry_repair", "cut evidence validation failed")
        try:
            geometry_summary = validate_cut_evidence(
                evidence,
                self.plan,
                source_sha256=self.source_model_sha256,
                script_sha256=self.cut_script_sha256,
            )
        except CutEvidenceError as exc:
            self._failure("needs_geometry_repair", str(exc))
        raw_structure_diagram = _value(evidence, "structure_diagram")
        structure_diagram_summary = (
            {
                key: _value(raw_structure_diagram, key)
                for key in ("path", "sha256", "width_px", "height_px")
            }
            if raw_structure_diagram is not None
            else None
        )
        self.cut_evidence = {
            "status": _value(evidence, "status"),
            "validation_level": _value(evidence, "validation_level", "full"),
            "source_model_sha256": source_sha,
            "script_sha256": self.cut_script_sha256,
            "imported_script_sha256": _jsonable(
                _value(evidence, "imported_script_sha256", {})
            ),
            "cut_ids": list(_value(evidence, "cut_ids", [])),
            "piece_hashes": dict(piece_hashes),
            "connector_evidence_sha256": _value(
                evidence, "connector_evidence_sha256"
            ),
            "structure_diagram": _jsonable(structure_diagram_summary),
            "validation": _jsonable(validation),
            "geometry_validation": geometry_summary,
        }
        self.events.append("cut-evidence-recorded")

    def record_geometry_validation(self, path: str, *, passed: bool) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(self.cut_evidence is not None, "cut evidence required")
        self._require(not self.geometry_validation_path, "geometry validation already recorded")
        if not passed:
            self.status = "needs_geometry_repair"
        self.geometry_validation_path = str(path)
        self.geometry_validation_passed = bool(passed)
        self.events.append(f"geometry-{'passed' if passed else 'failed'}")

    def confirm_bambu_provider(self, profile: Any) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(self.geometry_validation_passed, "passed geometry validation required")
        self._require(
            self.delivery_target in (None, "bambu-gcode-3mf"),
            "delivery target conflicts with Bambu package delivery",
        )
        if (
            _value(profile, "provider_id") != "app.bambu-studio"
            or _value(profile, "smoke_status") != "passed"
            or not _value(profile, "host")
            or not _value(profile, "version")
            or not _value(profile, "printer_model")
            or _value(profile, "nozzle_mm") is None
            or not _value(profile, "print_settings_id")
        ):
            self._failure("needs_provider_support", "Bambu provider smoke evidence is required")
        self._require(self.bambu_provider is None, "Bambu provider already recorded")
        self.delivery_target = "bambu-gcode-3mf"
        self.bambu_provider = _jsonable(profile)
        self.events.append("gate-b-bambu-confirmed")

    def record_standard_artifacts(
        self,
        stl_hashes: Mapping[str, str],
        result: Any,
    ) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(self.geometry_validation_passed, "passed geometry validation required")
        self._require(self.plan is not None, "split plan required")
        self._require(
            self.delivery_target in (None, "stl+standard-3mf"),
            "delivery target conflicts with standard mesh delivery",
        )
        self._require(
            self.bambu_provider is None and not self.plate_packages,
            "delivery target conflicts with Bambu package delivery",
        )

        expected_piece_ids = set(self.plan.leaf_piece_ids)
        self._require(
            set(stl_hashes) == expected_piece_ids,
            "STL hash set must match all leaf pieces",
        )
        verified_stl_hashes = {
            piece_id: _require_sha(digest, f"STL hash for {piece_id}")
            for piece_id, digest in stl_hashes.items()
        }

        self._require(
            isinstance(result, Standard3MFVerification) and result.valid is True,
            "verified standard 3MF result required",
        )
        package_path = Path(result.package_path)
        self._require(package_path.is_file(), "standard 3MF package is missing")
        self._require(
            _require_sha(result.sha256, "standard 3MF SHA-256")
            == standard_sha256_file(package_path),
            "standard 3MF hash mismatch",
        )
        self._require(
            set(result.object_names) == expected_piece_ids
            and len(result.object_names) == len(expected_piece_ids)
            and set(result.transforms) == expected_piece_ids,
            "standard 3MF object mapping must match all leaf pieces",
        )
        self._require(not result.gcode_members, "standard 3MF must not contain G-code")
        assembly_filename = getattr(self.plan, "assembly_filename", None)
        if assembly_filename:
            self._require(
                package_path.name == assembly_filename,
                "standard 3MF filename does not match the split plan",
            )

        self.delivery_target = "stl+standard-3mf"
        self.standard_stl_hashes = verified_stl_hashes
        self.standard_3mf = _jsonable(result)
        self.events.append("standard-artifacts-recorded")

    def record_stl_diagram_artifacts(
        self,
        stl_hashes: Mapping[str, str],
        diagram_path: Path,
        *,
        diagram_sha256: str,
        width_px: int,
        height_px: int,
    ) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(self.geometry_validation_passed, "passed geometry validation required")
        self._require(self.plan is not None, "split plan required")
        self._require(
            self.delivery_target in (None, "stl+structure-diagram"),
            "delivery target conflicts with STL diagram delivery",
        )
        self._require(
            self.standard_3mf is None
            and self.bambu_provider is None
            and not self.plate_packages,
            "delivery target conflicts with another artifact delivery",
        )
        self._require(
            getattr(self.plan, "assembly_filename", None) is None,
            "split plan still requires a standard 3MF",
        )
        expected_filename = getattr(
            self.plan, "structure_diagram_filename", None
        )
        self._require(expected_filename is not None, "split plan must request a diagram")

        expected_piece_ids = set(self.plan.leaf_piece_ids)
        self._require(
            set(stl_hashes) == expected_piece_ids,
            "STL hash set must match all leaf pieces",
        )
        verified_stl_hashes = {
            piece_id: _require_sha(digest, f"STL hash for {piece_id}")
            for piece_id, digest in stl_hashes.items()
        }

        path = Path(diagram_path)
        self._require(path.is_file(), "structure diagram is missing")
        self._require(
            path.name == expected_filename,
            "structure diagram filename does not match the split plan",
        )
        actual_width, actual_height = _png_dimensions(path)
        self._require(
            (width_px, height_px) == (actual_width, actual_height),
            "structure diagram dimensions mismatch",
        )
        self._require(
            actual_width >= 1600 and actual_height >= 1200,
            "structure diagram dimensions are too small",
        )
        verified_diagram_sha = _require_sha(
            diagram_sha256, "structure diagram SHA-256"
        )
        self._require(
            verified_diagram_sha == standard_sha256_file(path),
            "structure diagram hash mismatch",
        )
        evidence = _value(self.cut_evidence, "structure_diagram")
        self._require(evidence is not None, "cut evidence is missing the diagram")
        self._require(
            _value(evidence, "sha256") == verified_diagram_sha
            and _value(evidence, "width_px") == actual_width
            and _value(evidence, "height_px") == actual_height,
            "structure diagram does not match cut evidence",
        )

        self.delivery_target = "stl+structure-diagram"
        self.standard_stl_hashes = verified_stl_hashes
        self.structure_diagram = {
            "path": str(path),
            "sha256": verified_diagram_sha,
            "width_px": actual_width,
            "height_px": actual_height,
        }
        self.events.append("stl-diagram-artifacts-recorded")

    def record_plate_package(self, plate_id: str, result: Any) -> None:
        self._require(self.status == "in_progress", f"cannot modify status {self.status}")
        self._require(
            self.bambu_provider is not None,
            "Bambu provider confirmation required",
        )
        expected = {plate.id for plate in self.plan.plates}
        self._require(plate_id in expected, "unknown plate id")
        self._require(plate_id not in self.plate_packages, "duplicate plate package")
        if not isinstance(result, PackageVerification) or result.valid is not True:
            self._failure(
                "needs_provider_support",
                "provider support required: verified Bambu package result required",
            )
        package_path = Path(result.package_path)
        expected_piece_ids = tuple(self.plan.plates[[plate.id for plate in self.plan.plates].index(plate_id)].piece_ids)
        if (
            not package_path.is_file()
            or result.gcode_member != "Metadata/plate_1.gcode"
            or tuple(result.object_names) != tuple(sorted(expected_piece_ids))
            or result.printer_model != self.bambu_provider["printer_model"]
            or result.nozzle_mm != self.bambu_provider["nozzle_mm"]
            or result.print_settings_id != self.bambu_provider["print_settings_id"]
        ):
            self._failure("needs_provider_support", f"package verification mismatch for {plate_id}")
        self.plate_packages[plate_id] = _jsonable(result)
        self.events.append(f"plate-package-recorded:{plate_id}")

    def mark_needs_provider_support(self, detail: str) -> None:
        self._require(self.status != "done", "cannot modify delivered manifest")
        self.status = "needs_provider_support"
        self.events.append(f"needs_provider_support:{detail}")

    def confirm_final_review(self) -> None:
        self._require(
            self.status == "in_progress",
            f"cannot review status {self.status}; provider support required",
        )
        self._require(
            self.geometry_validation_passed,
            "passed geometry validation required",
        )
        if self.delivery_target == "stl+standard-3mf":
            expected_piece_ids = set(self.plan.leaf_piece_ids)
            if (
                self.standard_3mf is None
                or set(self.standard_stl_hashes) != expected_piece_ids
                or self.bambu_provider is not None
                or self.plate_packages
            ):
                raise StageError("standard artifacts are required")
        elif self.delivery_target == "stl+structure-diagram":
            expected_piece_ids = set(self.plan.leaf_piece_ids)
            if (
                self.structure_diagram is None
                or set(self.standard_stl_hashes) != expected_piece_ids
                or self.standard_3mf is not None
                or self.bambu_provider is not None
                or self.plate_packages
            ):
                raise StageError("STL and structure diagram artifacts are required")
        elif self.delivery_target == "bambu-gcode-3mf":
            expected = {plate.id for plate in self.plan.plates}
            if set(self.plate_packages) != expected:
                raise StageError("missing plate packages")
        elif getattr(self.plan, "assembly_filename", None):
            raise StageError("standard artifacts are required")
        else:
            raise StageError("delivery target is required")
        self.final_review_confirmed = True
        self.user_review_status = "accepted_by_user"
        self.events.append("gate-c-confirmed")

    def deliver(self) -> None:
        self._require(self.final_review_confirmed, "final review required")
        self._require(self.status == "in_progress", f"cannot deliver status {self.status}")
        self.status = "done"
        self.events.append("delivered")

    def to_dict(self) -> dict[str, Any]:
        plan_mapping = {
            plate.id: list(plate.piece_ids) for plate in self.plan.plates
        } if self.plan is not None else {}
        return {
            "run_id": self.run_id,
            "status": self.status,
            "split_requested": self.split_requested,
            "split_reason": self.split_reason,
            "source_model_sha256": self.source_model_sha256,
            "cut_script_sha256": self.cut_script_sha256,
            "plan": _jsonable(self.plan),
            "cut_evidence": _jsonable(self.cut_evidence),
            "geometry_validation_path": self.geometry_validation_path,
            "geometry_validation_passed": self.geometry_validation_passed,
            "delivery_target": self.delivery_target,
            "standard_stl_hashes": dict(self.standard_stl_hashes),
            "standard_3mf": _jsonable(self.standard_3mf),
            "structure_diagram": _jsonable(self.structure_diagram),
            "bambu_provider": _jsonable(self.bambu_provider),
            "plate_mapping": plan_mapping,
            "plate_packages": _jsonable(self.plate_packages),
            "final_review_confirmed": self.final_review_confirmed,
            "user_review_status": self.user_review_status,
            "upload": False,
            "send": False,
            "queue": False,
            "printer_started": False,
            "events": list(self.events),
        }
