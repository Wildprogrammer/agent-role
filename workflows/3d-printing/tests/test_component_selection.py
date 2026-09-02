import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from component_selection import (  # noqa: E402
    ComponentAssignmentRequired,
    ComponentCandidate,
    choose_target_component,
)


def candidate(index, volume, distance, hits=()):
    return ComponentCandidate(
        index=index,
        face_indices=(index,),
        face_count=1,
        volume_mm3=volume,
        bbox_min=(0.0, 0.0, 0.0),
        bbox_max=(1.0, 1.0, 1.0),
        seed_hits=tuple(hits),
        nearest_distance_mm=distance,
    )


def test_all_seed_points_must_select_one_component():
    chosen = choose_target_component(
        "arm-left-cut",
        (candidate(0, 100.0, 0.0, ("pin",)), candidate(1, 2.0, 5.0)),
        seed_ids=("pin",),
    )

    assert chosen.index == 0


def test_conflicting_seed_hits_require_user_assignment():
    candidates = (
        candidate(0, 100.0, 0.0, ("a",)),
        candidate(1, 90.0, 0.0, ("b",)),
    )

    with pytest.raises(ComponentAssignmentRequired) as error:
        choose_target_component("head-cut", candidates, seed_ids=("a", "b"))

    assert error.value.status == "needs_user_component_assignment"
    assert error.value.cut_id == "head-cut"
    assert error.value.candidates == candidates


def test_nearest_candidate_needs_tenfold_volume_advantage_without_seed():
    chosen = choose_target_component(
        "plain-cut",
        (candidate(0, 100.0, 0.01), candidate(1, 9.0, 1.0)),
        seed_ids=(),
    )

    assert chosen.index == 0


def test_small_distance_margin_or_weak_volume_advantage_is_ambiguous():
    with pytest.raises(ComponentAssignmentRequired):
        choose_target_component(
            "plain-cut",
            (candidate(0, 20.0, 0.01), candidate(1, 9.0, 1.0)),
            seed_ids=(),
        )
    with pytest.raises(ComponentAssignmentRequired):
        choose_target_component(
            "plain-cut",
            (candidate(0, 100.0, 0.01), candidate(1, 9.0, 0.02)),
            seed_ids=(),
        )


def test_candidate_serialization_preserves_every_component_measurement():
    item = ComponentCandidate(
        index=4,
        face_indices=(1, 2, 3),
        face_count=3,
        volume_mm3=2.5,
        bbox_min=(1.0, 2.0, 3.0),
        bbox_max=(4.0, 5.0, 6.0),
        seed_hits=("pin-a",),
        nearest_distance_mm=0.125,
    )

    assert item.to_dict() == {
        "index": 4,
        "face_count": 3,
        "volume_mm3": 2.5,
        "bbox_min": [1.0, 2.0, 3.0],
        "bbox_max": [4.0, 5.0, 6.0],
        "seed_hits": ["pin-a"],
        "nearest_distance_mm": 0.125,
    }
