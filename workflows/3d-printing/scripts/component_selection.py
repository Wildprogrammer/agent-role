from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ComponentCandidate:
    index: int
    face_indices: tuple[int, ...]
    face_count: int
    volume_mm3: float
    bbox_min: tuple[float, float, float]
    bbox_max: tuple[float, float, float]
    seed_hits: tuple[str, ...]
    nearest_distance_mm: float

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "face_count": self.face_count,
            "volume_mm3": self.volume_mm3,
            "bbox_min": list(self.bbox_min),
            "bbox_max": list(self.bbox_max),
            "seed_hits": list(self.seed_hits),
            "nearest_distance_mm": self.nearest_distance_mm,
        }


class ComponentAssignmentRequired(ValueError):
    status = "needs_user_component_assignment"

    def __init__(
        self,
        cut_id: str,
        candidates: tuple[ComponentCandidate, ...],
    ) -> None:
        super().__init__(f"component assignment is ambiguous for {cut_id}")
        self.cut_id = cut_id
        self.candidates = candidates


def choose_target_component(
    cut_id: str,
    candidates: tuple[ComponentCandidate, ...],
    *,
    seed_ids: tuple[str, ...],
    distance_tolerance_mm: float = 0.02,
    dominance_ratio: float = 10.0,
) -> ComponentCandidate:
    if not candidates:
        raise ComponentAssignmentRequired(cut_id, candidates)
    if seed_ids:
        required = set(seed_ids)
        matching = [
            candidate
            for candidate in candidates
            if required <= set(candidate.seed_hits)
        ]
        if len(matching) == 1:
            return matching[0]
        raise ComponentAssignmentRequired(cut_id, candidates)

    ranked = sorted(candidates, key=lambda item: item.nearest_distance_mm)
    if len(ranked) == 1:
        return ranked[0]
    winner, runner_up = ranked[:2]
    if (
        runner_up.nearest_distance_mm - winner.nearest_distance_mm
        > distance_tolerance_mm
        and winner.volume_mm3 >= runner_up.volume_mm3 * dominance_ratio
    ):
        return winner
    raise ComponentAssignmentRequired(cut_id, candidates)
