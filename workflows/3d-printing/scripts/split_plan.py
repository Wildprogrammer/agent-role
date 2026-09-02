from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


class SplitPlanError(ValueError):
    pass


_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SplitPlanError(f"{label} must be an object")
    return value


def _nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SplitPlanError(f"{label} must be a non-empty string")
    return value.strip()


def _identifier(value: Any, label: str) -> str:
    identifier = _nonempty_string(value, label)
    if not _ID_PATTERN.fullmatch(identifier):
        raise SplitPlanError(f"{label} has invalid id")
    return identifier


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SplitPlanError(f"{label} must contain numbers")
    number = float(value)
    if not math.isfinite(number):
        raise SplitPlanError(f"{label} must contain finite numbers")
    return number


def _vector(value: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise SplitPlanError(f"{label} must contain three numbers")
    if len(value) != 3:
        raise SplitPlanError(f"{label} must contain three numbers")
    return tuple(_number(item, label) for item in value)  # type: ignore[return-value]


def _positive_number(value: Any, label: str) -> float:
    number = _number(value, label)
    if number <= 0:
        raise SplitPlanError(f"{label} must be positive")
    return number


def _vector_length(value: tuple[float, float, float]) -> float:
    return math.sqrt(sum(item * item for item in value))


def _cross_length(
    first: tuple[float, float, float], second: tuple[float, float, float]
) -> float:
    cross = (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )
    return _vector_length(cross)


@dataclass(frozen=True)
class PiecePlacement:
    piece_id: str
    position_mm: tuple[float, float, float]
    rotation_deg: tuple[float, float, float]


@dataclass(frozen=True)
class CutInstruction:
    id: str
    input_piece: str
    point_mm: tuple[float, float, float]
    normal: tuple[float, float, float]
    negative_piece: str
    positive_piece: str


@dataclass(frozen=True)
class ConnectorInstruction:
    id: str
    cut_id: str
    type: str
    male_piece: str
    female_piece: str
    center_mm: tuple[float, float, float]
    axis: tuple[float, float, float]
    key_direction: tuple[float, float, float]
    width_mm: float
    height_mm: float
    corner_radius_mm: float
    engagement_mm: float
    root_fillet_mm: float
    tip_chamfer_mm: float
    clearance_per_side_mm: float
    socket_bottom_clearance_mm: float
    minimum_wall_mm: float
    minimum_edge_margin_mm: float


@dataclass(frozen=True)
class PlatePlan:
    id: str
    piece_ids: tuple[str, ...]
    layout: tuple[PiecePlacement, ...] | None
    placement_policy: str


@dataclass(frozen=True)
class SplitPlan:
    reason: str
    source_model: str
    units: str
    connection_strategy: str
    assembly_filename: str | None
    structure_diagram_filename: str | None
    cuts: tuple[CutInstruction, ...]
    connectors: tuple[ConnectorInstruction, ...]
    plates: tuple[PlatePlan, ...]
    leaf_piece_ids: tuple[str, ...]
    volume_tolerance_ratio: float

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "SplitPlan":
        if raw.get("split_requested") is not True:
            raise SplitPlanError("split_requested must be true")
        reason = _nonempty_string(raw.get("reason"), "reason")
        source_model = _nonempty_string(raw.get("source_model"), "source_model")
        if raw.get("units") != "mm":
            raise SplitPlanError("units must be mm")
        connection_strategy = _nonempty_string(
            raw.get("connection_strategy"), "connection_strategy"
        )
        if connection_strategy not in {"none", "integrated-keyed-pin"}:
            raise SplitPlanError(
                "this headless cutter only supports connection_strategy none or "
                "integrated-keyed-pin"
            )
        raw_assembly_filename = raw.get("assembly_filename")
        assembly_filename: str | None = None
        if raw_assembly_filename is not None:
            assembly_filename = _nonempty_string(
                raw_assembly_filename, "assembly_filename"
            )
            if (
                Path(assembly_filename).name != assembly_filename
                or "/" in assembly_filename
                or "\\" in assembly_filename
                or Path(assembly_filename).suffix.lower() != ".3mf"
            ):
                raise SplitPlanError(
                    "assembly_filename must be a local .3mf filename"
                )
        raw_structure_diagram_filename = raw.get("structure_diagram_filename")
        structure_diagram_filename: str | None = None
        if raw_structure_diagram_filename is not None:
            structure_diagram_filename = _nonempty_string(
                raw_structure_diagram_filename,
                "structure_diagram_filename",
            )
            if (
                Path(structure_diagram_filename).name
                != structure_diagram_filename
                or "/" in structure_diagram_filename
                or "\\" in structure_diagram_filename
                or Path(structure_diagram_filename).suffix.lower() != ".png"
            ):
                raise SplitPlanError(
                    "structure_diagram_filename must be a local .png filename"
                )
        raw_cuts = raw.get("cuts")
        if not isinstance(raw_cuts, list) or not raw_cuts:
            raise SplitPlanError("cuts must be a non-empty list")
        cuts: list[CutInstruction] = []
        active = ["source"]
        seen_pieces = {"source"}
        seen_cuts: set[str] = set()
        piece_parents: dict[str, str] = {}
        cut_outputs: dict[str, tuple[str, str]] = {}
        for index, raw_cut in enumerate(raw_cuts):
            cut = _mapping(raw_cut, f"cuts[{index}]")
            cut_id = _identifier(cut.get("id"), f"cuts[{index}].id")
            if cut_id in seen_cuts:
                raise SplitPlanError("cut id must be unique")
            seen_cuts.add(cut_id)
            input_piece = _identifier(
                cut.get("input_piece"), f"cuts[{index}].input_piece"
            )
            if input_piece not in active:
                raise SplitPlanError(
                    f"input piece must be an active piece: {input_piece}"
                )
            point_mm = _vector(cut.get("point_mm"), f"cuts[{index}].point_mm")
            normal = _vector(cut.get("normal"), f"cuts[{index}].normal")
            if math.isclose(math.sqrt(sum(item * item for item in normal)), 0.0):
                raise SplitPlanError("normal must be non-zero")
            negative_piece = _identifier(
                cut.get("negative_piece"), f"cuts[{index}].negative_piece"
            )
            positive_piece = _identifier(
                cut.get("positive_piece"), f"cuts[{index}].positive_piece"
            )
            if negative_piece == positive_piece or negative_piece == "source":
                raise SplitPlanError("piece id must be unique and not source")
            if positive_piece in seen_pieces or negative_piece in seen_pieces:
                raise SplitPlanError("piece id must be unique")
            active.remove(input_piece)
            active.extend((negative_piece, positive_piece))
            seen_pieces.update((negative_piece, positive_piece))
            piece_parents[negative_piece] = input_piece
            piece_parents[positive_piece] = input_piece
            cut_outputs[cut_id] = (negative_piece, positive_piece)
            cuts.append(
                CutInstruction(
                    id=cut_id,
                    input_piece=input_piece,
                    point_mm=point_mm,
                    normal=normal,
                    negative_piece=negative_piece,
                    positive_piece=positive_piece,
                )
            )

        raw_connectors = raw.get("connectors", [])
        if not isinstance(raw_connectors, list):
            raise SplitPlanError("connectors must be a list")
        if connection_strategy == "none" and raw_connectors:
            raise SplitPlanError("connectors require an enabled connection strategy")
        if connection_strategy == "integrated-keyed-pin" and not raw_connectors:
            raise SplitPlanError("connectors must be a non-empty list")

        def is_descendant(piece_id: str, ancestor: str) -> bool:
            current = piece_id
            while True:
                if current == ancestor:
                    return True
                if current not in piece_parents:
                    return False
                current = piece_parents[current]

        connectors: list[ConnectorInstruction] = []
        seen_connectors: set[str] = set()
        for index, raw_connector in enumerate(raw_connectors):
            connector = _mapping(raw_connector, f"connectors[{index}]")
            connector_id = _identifier(
                connector.get("id"), f"connectors[{index}].id"
            )
            if connector_id in seen_connectors:
                raise SplitPlanError("connector id must be unique")
            seen_connectors.add(connector_id)
            cut_id = _identifier(
                connector.get("cut_id"), f"connectors[{index}].cut_id"
            )
            if cut_id not in cut_outputs:
                raise SplitPlanError("connector cut_id must reference a known cut")
            connector_type = _nonempty_string(
                connector.get("type"), f"connectors[{index}].type"
            )
            if connector_type != "integrated-keyed-pin":
                raise SplitPlanError(
                    "connector type must be integrated-keyed-pin"
                )
            male_piece = _identifier(
                connector.get("male_piece"), f"connectors[{index}].male_piece"
            )
            female_piece = _identifier(
                connector.get("female_piece"), f"connectors[{index}].female_piece"
            )
            if male_piece not in active:
                raise SplitPlanError("connector male_piece must be a leaf piece")
            if female_piece not in active:
                raise SplitPlanError("connector female_piece must be a leaf piece")
            if male_piece == female_piece:
                raise SplitPlanError("connector male and female pieces must be distinct")
            negative_root, positive_root = cut_outputs[cut_id]
            on_opposite_sides = (
                is_descendant(male_piece, negative_root)
                and is_descendant(female_piece, positive_root)
            ) or (
                is_descendant(male_piece, positive_root)
                and is_descendant(female_piece, negative_root)
            )
            if not on_opposite_sides:
                raise SplitPlanError(
                    "connector pieces must be on opposite sides of its cut"
                )

            center_mm = _vector(
                connector.get("center_mm"), f"connectors[{index}].center_mm"
            )
            axis = _vector(connector.get("axis"), f"connectors[{index}].axis")
            if math.isclose(_vector_length(axis), 0.0):
                raise SplitPlanError("connector axis must be non-zero")
            key_direction = _vector(
                connector.get("key_direction"),
                f"connectors[{index}].key_direction",
            )
            if math.isclose(_vector_length(key_direction), 0.0):
                raise SplitPlanError("connector key_direction must be non-zero")
            if math.isclose(_cross_length(axis, key_direction), 0.0, abs_tol=1e-9):
                raise SplitPlanError(
                    "connector axis and key_direction must not be collinear"
                )

            width_mm = _positive_number(
                connector.get("width_mm"), f"connectors[{index}].width_mm"
            )
            height_mm = _positive_number(
                connector.get("height_mm"), f"connectors[{index}].height_mm"
            )
            corner_radius_mm = _positive_number(
                connector.get("corner_radius_mm"),
                f"connectors[{index}].corner_radius_mm",
            )
            if corner_radius_mm > min(width_mm, height_mm) / 2:
                raise SplitPlanError(
                    "connector corner_radius_mm exceeds half the short side"
                )
            engagement_mm = _positive_number(
                connector.get("engagement_mm"),
                f"connectors[{index}].engagement_mm",
            )
            root_fillet_mm = _positive_number(
                connector.get("root_fillet_mm"),
                f"connectors[{index}].root_fillet_mm",
            )
            tip_chamfer_mm = _positive_number(
                connector.get("tip_chamfer_mm"),
                f"connectors[{index}].tip_chamfer_mm",
            )
            if tip_chamfer_mm >= engagement_mm:
                raise SplitPlanError(
                    "connector tip_chamfer_mm must be shorter than engagement_mm"
                )
            clearance_per_side_mm = _number(
                connector.get("clearance_per_side_mm"),
                f"connectors[{index}].clearance_per_side_mm",
            )
            if clearance_per_side_mm < 0 or clearance_per_side_mm > 1:
                raise SplitPlanError(
                    "connector clearance_per_side_mm must be between zero and one"
                )
            socket_bottom_clearance_mm = _positive_number(
                connector.get("socket_bottom_clearance_mm"),
                f"connectors[{index}].socket_bottom_clearance_mm",
            )
            minimum_wall_mm = _positive_number(
                connector.get("minimum_wall_mm"),
                f"connectors[{index}].minimum_wall_mm",
            )
            minimum_edge_margin_mm = _positive_number(
                connector.get("minimum_edge_margin_mm"),
                f"connectors[{index}].minimum_edge_margin_mm",
            )
            connectors.append(
                ConnectorInstruction(
                    id=connector_id,
                    cut_id=cut_id,
                    type=connector_type,
                    male_piece=male_piece,
                    female_piece=female_piece,
                    center_mm=center_mm,
                    axis=axis,
                    key_direction=key_direction,
                    width_mm=width_mm,
                    height_mm=height_mm,
                    corner_radius_mm=corner_radius_mm,
                    engagement_mm=engagement_mm,
                    root_fillet_mm=root_fillet_mm,
                    tip_chamfer_mm=tip_chamfer_mm,
                    clearance_per_side_mm=clearance_per_side_mm,
                    socket_bottom_clearance_mm=socket_bottom_clearance_mm,
                    minimum_wall_mm=minimum_wall_mm,
                    minimum_edge_margin_mm=minimum_edge_margin_mm,
                )
            )

        raw_plates = raw.get("plates")
        if not isinstance(raw_plates, list) or not raw_plates:
            raise SplitPlanError("plates must be a non-empty list")
        plates: list[PlatePlan] = []
        seen_plates: set[str] = set()
        mapped_pieces: list[str] = []
        for index, raw_plate in enumerate(raw_plates):
            plate = _mapping(raw_plate, f"plates[{index}]")
            plate_id = _identifier(plate.get("id"), f"plates[{index}].id")
            if plate_id in seen_plates:
                raise SplitPlanError("plate id must be unique")
            seen_plates.add(plate_id)
            raw_piece_ids = plate.get("piece_ids")
            if not isinstance(raw_piece_ids, list) or not raw_piece_ids:
                raise SplitPlanError("plate mapping must list piece_ids")
            piece_ids = tuple(
                _identifier(value, f"plates[{index}].piece_ids")
                for value in raw_piece_ids
            )
            if len(set(piece_ids)) != len(piece_ids):
                raise SplitPlanError("piece id must be unique within a plate")
            if any(piece_id not in active for piece_id in piece_ids):
                raise SplitPlanError("plate mapping contains an unknown piece")
            if any(piece_id in mapped_pieces for piece_id in piece_ids):
                raise SplitPlanError("plate mapping assigns a piece more than once")
            mapped_pieces.extend(piece_ids)

            raw_layout = plate.get("layout")
            if len(piece_ids) > 1 and not isinstance(raw_layout, list):
                raise SplitPlanError("layout is required for multiple pieces")
            layout: tuple[PiecePlacement, ...] | None = None
            placement_policy = "provider-default-single-piece"
            if raw_layout is not None:
                if not isinstance(raw_layout, list):
                    raise SplitPlanError("layout must be a list")
                placements: list[PiecePlacement] = []
                for layout_index, raw_placement in enumerate(raw_layout):
                    placement = _mapping(
                        raw_placement, f"plates[{index}].layout[{layout_index}]"
                    )
                    piece_id = _identifier(
                        placement.get("piece_id"),
                        f"plates[{index}].layout[{layout_index}].piece_id",
                    )
                    if piece_id not in piece_ids:
                        raise SplitPlanError("layout contains an unknown piece")
                    if any(item.piece_id == piece_id for item in placements):
                        raise SplitPlanError("piece id must be unique in layout")
                    placements.append(
                        PiecePlacement(
                            piece_id=piece_id,
                            position_mm=_vector(
                                placement.get("position_mm"),
                                f"plates[{index}].layout[{layout_index}].position_mm",
                            ),
                            rotation_deg=_vector(
                                placement.get("rotation_deg"),
                                f"plates[{index}].layout[{layout_index}].rotation_deg",
                            ),
                        )
                    )
                if {item.piece_id for item in placements} != set(piece_ids):
                    raise SplitPlanError("layout must map every piece on the plate")
                layout = tuple(placements)
                placement_policy = "confirmed-layout"
            plates.append(
                PlatePlan(
                    id=plate_id,
                    piece_ids=piece_ids,
                    layout=layout,
                    placement_policy=placement_policy,
                )
            )

        if set(mapped_pieces) != set(active) or len(mapped_pieces) != len(active):
            raise SplitPlanError("plate mapping must cover every leaf piece exactly once")
        tolerance = _number(
            raw.get("volume_tolerance_ratio", 0.0001),
            "volume_tolerance_ratio",
        )
        if tolerance <= 0 or tolerance >= 1:
            raise SplitPlanError("volume_tolerance_ratio must be between zero and one")
        return cls(
            reason=reason,
            source_model=source_model,
            units="mm",
            connection_strategy=connection_strategy,
            assembly_filename=assembly_filename,
            structure_diagram_filename=structure_diagram_filename,
            cuts=tuple(cuts),
            connectors=tuple(connectors),
            plates=tuple(plates),
            leaf_piece_ids=tuple(active),
            volume_tolerance_ratio=tolerance,
        )


def load_split_plan(path: Path) -> SplitPlan:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SplitPlanError(f"cannot read split plan: {exc}") from exc
    return SplitPlan.from_mapping(_mapping(raw, "split plan"))
