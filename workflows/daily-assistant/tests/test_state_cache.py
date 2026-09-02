import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "state_cache.py"
SPEC = importlib.util.spec_from_file_location("daily_state_cache", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _task(task_id: str, status: str = "not_started") -> dict:
    return {
        "id": task_id,
        "title": task_id,
        "breakdown": [],
        "priority": "P1",
        "priority_reason": "影响当天交付",
        "project": "",
        "product": "",
        "module": "",
        "iteration": "",
        "status": status,
        "active_date": "2026-07-13",
        "carried_from": None,
        "updates": [],
    }


def test_no_progress_leaves_task_state_unchanged(tmp_path: Path):
    store = MODULE.StateCache(tmp_path / "state.json")
    store.upsert_task(_task("task-001"))

    assert store.task("task-001")["status"] == "not_started"
    assert store.task("task-001")["updates"] == []


def test_explicit_progress_is_the_only_way_to_mark_task_done(tmp_path: Path):
    store = MODULE.StateCache(tmp_path / "state.json")
    store.upsert_task(_task("task-001"))

    store.record_progress(
        "task-001",
        status="done",
        at="2026-07-13T10:20:00+08:00",
        summary="用户确认策略验证完成",
    )

    task = store.task("task-001")
    assert task["status"] == "done"
    assert task["updates"] == [
        {
            "at": "2026-07-13T10:20:00+08:00",
            "status": "done",
            "summary": "用户确认策略验证完成",
        }
    ]


def test_rollover_moves_only_unfinished_tasks_once_and_never_creates_report(
    tmp_path: Path,
):
    store = MODULE.StateCache(tmp_path / "state.json")
    store.upsert_task(_task("todo"))
    store.upsert_task(_task("done", "done"))

    first = store.rollover("2026-07-13")
    second = store.rollover("2026-07-13")
    data = store.load()

    assert first == ("todo",)
    assert second == ()
    assert store.task("todo")["active_date"] == "2026-07-14"
    assert store.task("todo")["carried_from"] == "2026-07-13"
    assert store.task("done")["active_date"] == "2026-07-13"
    assert "reports" not in data


def test_state_cache_refuses_raw_input_in_nested_updates(tmp_path: Path):
    store = MODULE.StateCache(tmp_path / "state.json")
    task = _task("task-001")
    task["updates"] = [
        {
            "at": "2026-07-13T10:00:00+08:00",
            "raw_text": "原始进展",
        }
    ]

    with pytest.raises(ValueError, match="raw"):
        store.upsert_task(task)
