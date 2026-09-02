from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping


def _load_task_processing():
    name = "daily_assistant_task_processing"
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    script = Path(__file__).with_name("task_processing.py")
    spec = importlib.util.spec_from_file_location(name, script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load task processing module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TASK_PROCESSING = _load_task_processing()


class StateCache:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_state()
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self._validate_state(data)
        return data

    def upsert_task(self, task: Mapping[str, Any]) -> None:
        validated = TASK_PROCESSING.validate_task(task)
        data = self.load()
        tasks = data["tasks"]
        for index, existing in enumerate(tasks):
            if existing["id"] == validated["id"]:
                tasks[index] = validated
                self._save(data)
                return
        tasks.append(validated)
        self._save(data)

    def task(self, task_id: str) -> dict[str, Any]:
        for item in self.load()["tasks"]:
            if item["id"] == task_id:
                return deepcopy(item)
        raise KeyError(f"unknown task: {task_id}")

    def record_progress(
        self, task_id: str, *, status: str, at: str, summary: str
    ) -> None:
        if status not in TASK_PROCESSING.STATUSES:
            raise ValueError("status is invalid")
        if not isinstance(at, str) or not at.strip():
            raise ValueError("at must be a non-empty string")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary must be a non-empty string")
        data = self.load()
        for task in data["tasks"]:
            if task["id"] == task_id:
                task["status"] = status
                task["updates"].append(
                    {
                        "at": at.strip(),
                        "status": status,
                        "summary": summary.strip(),
                    }
                )
                TASK_PROCESSING.validate_task(task)
                self._save(data)
                return
        raise KeyError(f"unknown task: {task_id}")

    def rollover(self, day: str) -> tuple[str, ...]:
        current_day = self._parse_date(day)
        data = self.load()
        if any(item["date"] == day for item in data["rollovers"]):
            return ()

        next_day = (current_day + timedelta(days=1)).isoformat()
        carried_ids = []
        for task in data["tasks"]:
            if task["active_date"] == day and task["status"] != "done":
                task["active_date"] = next_day
                task["carried_from"] = day
                carried_ids.append(task["id"])
        data["rollovers"].append({"date": day, "task_ids": carried_ids})
        self._save(data)
        return tuple(carried_ids)

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {"schema_version": 1, "tasks": [], "rollovers": []}

    def _save(self, data: Mapping[str, Any]) -> None:
        self._validate_state(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    @staticmethod
    def _parse_date(value: str) -> date:
        if not isinstance(value, str):
            raise ValueError("rollover date must be an ISO date")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("rollover date must be an ISO date") from exc

    @staticmethod
    def _validate_state(data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise ValueError("state must be a mapping")
        if data.get("schema_version") != 1:
            raise ValueError("state schema version is invalid")
        tasks = data.get("tasks")
        rollovers = data.get("rollovers")
        if not isinstance(tasks, list) or not isinstance(rollovers, list):
            raise ValueError("state collections are invalid")
        task_ids = set()
        for task in tasks:
            validated = TASK_PROCESSING.validate_task(task)
            if validated["id"] in task_ids:
                raise ValueError("task ids must be unique")
            task_ids.add(validated["id"])
        rollover_dates = set()
        for rollover in rollovers:
            if not isinstance(rollover, Mapping):
                raise ValueError("rollover must be a mapping")
            day = rollover.get("date")
            StateCache._parse_date(day)
            task_ids_for_day = rollover.get("task_ids")
            if not isinstance(task_ids_for_day, list) or not all(
                isinstance(task_id, str) for task_id in task_ids_for_day
            ):
                raise ValueError("rollover task ids are invalid")
            if day in rollover_dates:
                raise ValueError("rollover dates must be unique")
            rollover_dates.add(day)
