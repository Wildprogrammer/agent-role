from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping


STATUSES = frozenset({"not_started", "in_progress", "done", "blocked"})
PRIORITIES = frozenset({"P0", "P1", "P2", "P3"})
FORBIDDEN_KEYS = frozenset(
    {"raw_input", "raw_text", "original_message", "source_message"}
)
TASK_KEYS = frozenset(
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
CORRECTABLE_KEYS = frozenset(
    {"title", "breakdown", "priority", "project", "product", "module", "iteration"}
)


@dataclass(frozen=True)
class PrioritySuggestion:
    level: str
    reason: str
    is_suggestion: bool = True


def suggest_priority(
    *, impact: str, urgency: str, blocks_others: bool
) -> PrioritySuggestion:
    if blocks_others and urgency == "today":
        return PrioritySuggestion("P0", "阻塞他人且需当日处理")
    if impact == "high" or urgency == "today":
        return PrioritySuggestion("P1", "影响当天交付")
    if impact == "medium":
        return PrioritySuggestion("P2", "需要规划但不阻塞当天交付")
    return PrioritySuggestion("P3", "可在可用时间处理")


def new_task(
    *, task_id: str, title: str, active_date: str, suggestion: PrioritySuggestion
) -> dict[str, Any]:
    if suggestion.level not in PRIORITIES:
        raise ValueError("priority suggestion level is invalid")
    reason = _required_string(suggestion.reason, "priority_reason")
    return validate_task(
        {
            "id": task_id,
            "title": title,
            "breakdown": [],
            "priority": suggestion.level,
            "priority_reason": reason,
            "project": "",
            "product": "",
            "module": "",
            "iteration": "",
            "status": "not_started",
            "active_date": active_date,
            "carried_from": None,
            "updates": [],
        }
    )


def apply_user_correction(
    task: Mapping[str, Any], correction: Mapping[str, Any]
) -> dict[str, Any]:
    updated = validate_task(task)
    unknown = set(correction) - CORRECTABLE_KEYS
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported correction field: {names}")
    for key in CORRECTABLE_KEYS:
        if key in correction:
            updated[key] = correction[key]
    if "priority" in correction:
        updated["priority_reason"] = "用户已确认"
    return validate_task(updated)


def validate_task(task: Mapping[str, Any]) -> dict[str, Any]:
    assert_no_forbidden_keys(task)
    keys = set(task)
    missing = TASK_KEYS - keys
    extra = keys - TASK_KEYS
    if missing:
        raise ValueError(f"task missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ValueError(f"task has unsupported fields: {', '.join(sorted(extra))}")

    normalized = {
        "id": _required_string(task["id"], "id"),
        "title": _required_string(task["title"], "title"),
        "breakdown": _string_list(task["breakdown"], "breakdown"),
        "priority": _priority(task["priority"]),
        "priority_reason": _required_string(task["priority_reason"], "priority_reason"),
        "project": _optional_string(task["project"], "project"),
        "product": _optional_string(task["product"], "product"),
        "module": _optional_string(task["module"], "module"),
        "iteration": _optional_string(task["iteration"], "iteration"),
        "status": _status(task["status"]),
        "active_date": _iso_date(task["active_date"], "active_date"),
        "carried_from": _carried_from(task["carried_from"]),
        "updates": _updates(task["updates"]),
    }
    return normalized


def assert_no_forbidden_keys(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in FORBIDDEN_KEYS:
                raise ValueError(f"raw field is not allowed: {key}")
            assert_no_forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_forbidden_keys(item)


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value.strip()


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return [_required_string(item, name) for item in value]


def _priority(value: Any) -> str:
    if value not in PRIORITIES:
        raise ValueError("priority is invalid")
    return str(value)


def _status(value: Any) -> str:
    if value not in STATUSES:
        raise ValueError("status is invalid")
    return str(value)


def _iso_date(value: Any, name: str) -> str:
    text = _required_string(value, name)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must use ISO date format") from exc
    return text


def _carried_from(value: Any) -> str | None:
    if value is None:
        return None
    return _iso_date(value, "carried_from")


def _updates(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("updates must be a list")
    normalized = []
    for update in value:
        if not isinstance(update, Mapping):
            raise ValueError("update must be a mapping")
        assert_no_forbidden_keys(update)
        if set(update) != {"at", "status", "summary"}:
            raise ValueError("update fields are invalid")
        normalized.append(
            {
                "at": _required_string(update["at"], "update.at"),
                "status": _status(update["status"]),
                "summary": _required_string(update["summary"], "update.summary"),
            }
        )
    return normalized
