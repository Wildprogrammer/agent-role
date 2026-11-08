from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.binding_store import (
    BindingConflictError,
    BindingStore,
    BindingStoreError,
    SessionBinding,
    default_binding_store_path,
)


def test_binding_survives_store_recreation_and_unbind(tmp_path: Path) -> None:
    path = (tmp_path / "state" / "bindings.json").resolve()
    binding = SessionBinding(
        device_id="esp32-s3-01",
        profile="research-agent",
        stored_session_id="stored-123",
        title="GitHub hotspot",
    )

    BindingStore(path).bind(binding)

    assert BindingStore(path).get("esp32-s3-01") == binding
    assert BindingStore(path).unbind("esp32-s3-01") is True
    assert BindingStore(path).get("esp32-s3-01") is None


def test_binding_update_replaces_only_the_target_device(tmp_path: Path) -> None:
    store = BindingStore((tmp_path / "bindings.json").resolve())
    store.bind(SessionBinding("device-a", "default", "session-a", "A"))
    store.bind(SessionBinding("device-b", "assistant-agent", "session-b", "B"))
    store.bind(SessionBinding("device-a", "research-agent", "session-c", "C"))

    assert store.list() == (
        SessionBinding("device-a", "research-agent", "session-c", "C", revision=2),
        SessionBinding("device-b", "assistant-agent", "session-b", "B"),
    )


def test_compare_and_swap_rejects_stale_binding(tmp_path: Path) -> None:
    store = BindingStore((tmp_path / "bindings.json").resolve())
    original = store.bind(SessionBinding("device-a", "default", "session-a", "A"))
    replacement = store.compare_and_swap(
        original,
        SessionBinding("device-a", "default", "session-b", "B"),
    )

    assert replacement.revision == 2
    with pytest.raises(BindingConflictError, match="changed"):
        store.compare_and_swap(
            original,
            SessionBinding("device-a", "default", "session-c", "C"),
        )
    assert store.get("device-a") == replacement


def test_version_one_store_migrates_revision_on_next_write(tmp_path: Path) -> None:
    path = (tmp_path / "bindings.json").resolve()
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "bindings": {
                    "device-a": {
                        "device_id": "device-a",
                        "profile": "default",
                        "stored_session_id": "session-a",
                        "title": "A",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    current = BindingStore(path).get("device-a")

    assert current is not None and current.revision == 1
    assert current.host == "hermes"

    BindingStore(path).bind(
        SessionBinding(
            "device-b",
            "assistant-agent",
            "session-b",
            "B",
            host="deepseek-harness",
        )
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == "1.2"
    assert saved["bindings"]["device-a"]["host"] == "hermes"
    assert saved["bindings"]["device-b"]["host"] == "deepseek-harness"


def test_cross_process_writers_do_not_lose_other_device_bindings(tmp_path: Path) -> None:
    path = (tmp_path / "bindings.json").resolve()
    root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(root / "src"), environment.get("PYTHONPATH", "")))
    )
    script = (
        "import sys; "
        "from pathlib import Path; "
        "from agent_workflow_hub.xiaozhi_hermes_bridge.binding_store import "
        "BindingStore, SessionBinding; "
        "store=BindingStore(Path(sys.argv[1])); device=sys.argv[2]; "
        "[(store.bind(SessionBinding(device,'default',f'{device}-{i}',device))) "
        "for i in range(5)]"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(path), f"device-{index}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        for index in range(6)
    ]

    failures = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        if process.returncode != 0:
            failures.append((process.returncode, stdout, stderr))

    assert failures == []
    bindings = BindingStore(path).list()
    assert {binding.device_id for binding in bindings} == {
        f"device-{index}" for index in range(6)
    }
    assert all(binding.revision == 5 for binding in bindings)


def test_binding_store_rejects_unknown_fields(tmp_path: Path) -> None:
    path = (tmp_path / "bindings.json").resolve()
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "bindings": {
                    "device-a": {
                        "device_id": "device-a",
                        "profile": "default",
                        "stored_session_id": "session-a",
                        "title": "A",
                        "token": "must-not-be-accepted",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(BindingStoreError, match="unknown field"):
        BindingStore(path).get("device-a")


def test_default_store_is_external_to_repository(tmp_path: Path) -> None:
    local_app_data = tmp_path / "AppData" / "Local"
    path = default_binding_store_path({"LOCALAPPDATA": str(local_app_data)})

    assert path == (
        local_app_data / "agent-workflow-hub" / "xiaozhi-hermes-bindings.json"
    ).resolve()
    repository = Path(__file__).resolve().parents[3]
    assert not path.is_relative_to(repository)
