from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any


TASK_FIELDS = frozenset(
    {
        "id",
        "title",
        "breakdown",
        "priority",
        "priority_reason",
        "project",
        "product",
        "module",
        "iteration",
        "status",
        "active_date",
        "carried_from",
        "updates",
    }
)
STATUS_SECTIONS = (
    ("完成", "done"),
    ("进行中", "in_progress"),
    ("阻塞", "blocked"),
    ("待更新/结转", "not_started"),
)


def render_daily_report(
    tasks: Sequence[Mapping[str, Any]], *, day: str
) -> str:
    selected = [task for task in tasks if task.get("active_date") == day]
    lines = [f"# {day} 日报", ""]
    for heading, status in STATUS_SECTIONS:
        lines.extend([f"## {heading}", ""])
        items = [task for task in selected if task.get("status") == status]
        if not items:
            lines.extend(["- 无", ""])
            continue
        for task in sorted(items, key=lambda item: str(item.get("id", ""))):
            lines.append(_task_line(task))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_monthly_task_draft(
    tasks: Sequence[Mapping[str, Any]], *, month: str
) -> str:
    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = defaultdict(
        list
    )
    for task in tasks:
        if str(task.get("active_date", "")).startswith(month):
            key = tuple(
                _text(task.get(field)) or "未分类"
                for field in ("project", "product", "module", "iteration")
            )
            grouped[key].append(task)

    lines = [f"# {month} 月度禅道任务草案", ""]
    if not grouped:
        lines.append("无符合月份的规范化任务。")
    for key in sorted(grouped):
        project, product, module, iteration = key
        lines.extend(
            [
                f"## 项目：{project}",
                f"### 产品：{product}",
                f"#### 模块：{module}",
                f"##### 迭代：{iteration}",
                "",
            ]
        )
        for task in sorted(grouped[key], key=lambda item: str(item.get("id", ""))):
            lines.append(
                f"- {_text(task.get('title'))}（{_text(task.get('priority'))}，"
                f"{_text(task.get('status'))}）"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_import_csv(
    tasks: Sequence[Mapping[str, Any]],
    *,
    template_header: Sequence[str],
    field_mapping: Mapping[str, str],
) -> str:
    if isinstance(template_header, str) or not template_header:
        raise ValueError("template header is required")
    header = [_header_name(item) for item in template_header]
    fields = []
    for column in header:
        if column not in field_mapping:
            raise ValueError(f"template header lacks mapping: {column}")
        field = field_mapping[column]
        if field not in TASK_FIELDS:
            raise ValueError(f"mapping uses unsupported task field: {field}")
        fields.append(field)

    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(header)
    for task in tasks:
        writer.writerow([_cell(task.get(field)) for field in fields])
    return output.getvalue()


def _task_line(task: Mapping[str, Any]) -> str:
    line = f"- [{_text(task.get('priority'))}] {_text(task.get('title'))}"
    updates = task.get("updates")
    if isinstance(updates, list) and updates:
        latest = updates[-1]
        if isinstance(latest, Mapping) and _text(latest.get("summary")):
            line += f"：{_text(latest.get('summary'))}"
    return line


def _header_name(value: Any) -> str:
    text = _text(value)
    if not text:
        raise ValueError("template header contains an empty column")
    return text


def _cell(value: Any) -> str:
    if isinstance(value, list):
        return "；".join(_text(item) for item in value)
    if value is None:
        return ""
    return _text(value)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else str(value or "")
