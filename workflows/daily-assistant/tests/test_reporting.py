import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "reporting.py"
SPEC = importlib.util.spec_from_file_location("daily_reporting", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


TASKS = [
    {
        "id": "a",
        "title": "策略验证",
        "breakdown": [],
        "priority": "P1",
        "priority_reason": "影响当天交付",
        "project": "平台",
        "product": "Agent",
        "module": "策略",
        "iteration": "7月",
        "status": "done",
        "active_date": "2026-07-13",
        "carried_from": None,
        "updates": [],
        "raw_input": "今日任务：策略验证",
    },
    {
        "id": "b",
        "title": "覆盖率评估",
        "breakdown": [],
        "priority": "P2",
        "priority_reason": "需要规划",
        "project": "平台",
        "product": "Agent",
        "module": "质量",
        "iteration": "7月",
        "status": "blocked",
        "active_date": "2026-07-13",
        "carried_from": None,
        "updates": [],
    },
]


def test_daily_and_monthly_reports_use_normalized_data_only():
    daily = MODULE.render_daily_report(TASKS, day="2026-07-13")
    monthly = MODULE.render_monthly_task_draft(TASKS, month="2026-07")

    assert "完成" in daily and "阻塞" in daily
    assert "今日任务：策略验证" not in daily
    assert "项目：平台" in monthly
    assert "产品：Agent" in monthly
    assert "模块：策略" in monthly
    assert "迭代：7月" in monthly
    assert "今日任务：策略验证" not in monthly


def test_csv_requires_runtime_template_and_explicit_mapping():
    with pytest.raises(ValueError, match="template"):
        MODULE.render_import_csv(TASKS, template_header=[], field_mapping={})

    csv_text = MODULE.render_import_csv(
        TASKS,
        template_header=["任务名称", "所属项目", "优先级"],
        field_mapping={
            "任务名称": "title",
            "所属项目": "project",
            "优先级": "priority",
        },
    )

    assert csv_text.splitlines()[0] == "任务名称,所属项目,优先级"
    assert "策略验证,平台,P1" in csv_text


def test_csv_rejects_template_header_without_mapping():
    with pytest.raises(ValueError, match="mapping"):
        MODULE.render_import_csv(
            TASKS,
            template_header=["任务名称", "未知字段"],
            field_mapping={"任务名称": "title"},
        )
