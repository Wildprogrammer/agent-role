import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "task_processing.py"
SPEC = importlib.util.spec_from_file_location("daily_task_processing", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_priority_suggestion_is_explainable_not_final():
    suggestion = MODULE.suggest_priority(
        impact="high", urgency="today", blocks_others=True
    )

    assert suggestion.level == "P0"
    assert "阻塞" in suggestion.reason
    assert suggestion.is_suggestion is True


def test_user_correction_replaces_suggestion_and_rejects_raw_fields():
    task = MODULE.new_task(
        task_id="task-001",
        title="策略验证",
        active_date="2026-07-13",
        suggestion=MODULE.PrioritySuggestion("P1", "影响当天交付"),
    )

    corrected = MODULE.apply_user_correction(
        task, {"priority": "P2", "module": "验证平台"}
    )

    assert corrected["priority"] == "P2"
    assert corrected["module"] == "验证平台"
    assert corrected["priority_reason"] == "用户已确认"
    with pytest.raises(ValueError, match="raw"):
        MODULE.validate_task({**corrected, "raw_input": "今日任务：策略验证"})
