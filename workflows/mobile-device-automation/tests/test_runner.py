from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from agent_workflow_hub.mobile_device_automation.agent_device_runtime import (
    AndroidDevice,
    RuntimeFailure,
)
from agent_workflow_hub.mobile_device_automation.contracts import load_request
from agent_workflow_hub.mobile_device_automation.models import (
    AssertionSpec,
    ComponentLifecycleSpec,
    ComponentRecoveryStepSpec,
    ViewportSpec,
)
from agent_workflow_hub.mobile_device_automation.renderer import compile_bundle
from agent_workflow_hub.mobile_device_automation.runner import (
    _manifest_plan_sha256,
    _materialize_selector,
    preview_bundle,
    run_bundle,
)


VIEWPORT = ViewportSpec(width=1080, height=2400, orientation="portrait", density=2.75)


def test_selector_parameters_are_materialized_with_escaped_values() -> None:
    assert _materialize_selector(
        'id="search" label="${QUERY}"', {"query": '示例"套餐'}
    ) == 'id="search" label="示例\\"套餐"'
    assert _materialize_selector(
        "id=search label=${QUERY}", {"query": "示例 套餐"}
    ) == 'id=search label="示例 套餐"'


def _make_bundle(
    tmp_path: Path,
    *,
    destructive: bool = False,
    script_body: str | None = None,
) -> Path:
    script = tmp_path / "open-settings.ad"
    command = "clear-data com.example.settings" if destructive else "open com.example.settings"
    body = script_body if script_body is not None else f"{command}\n"
    script.write_text(f"context platform=android\n{body}", encoding="utf-8")
    icon = tmp_path / "settings-icon.png"
    icon.write_bytes(b"settings-icon")
    result = tmp_path / "result-screen.png"
    result.write_bytes(b"result-screen")
    effect = "delete" if destructive else "read"
    value = {
        "schema_version": "1.0",
        "name": "settings-demo",
        "description": "Open Settings and verify the target screen.",
        "source_surface": "agent-device-cli",
        "target_platform": "android",
        "device": {
            "serial": "emulator-5554",
            "transport": "emulator",
            "viewport": {
                "width": VIEWPORT.width,
                "height": VIEWPORT.height,
                "orientation": VIEWPORT.orientation,
                "density": VIEWPORT.density,
            },
        },
        "parameters": [
            {"name": "query", "default": "demo", "sensitive": False},
            {"name": "password", "default": None, "sensitive": True},
        ],
        "steps": [
            {
                "id": "open-settings",
                "runner": "agent-device",
                "effect": effect,
                "timeout_seconds": 15,
                "script": str(script.resolve()),
                "assertion": {
                    "kind": "selector-visible",
                    "selector": "text=Settings",
                    "timeout_seconds": 10,
                },
            },
            {
                "id": "tap-settings-icon",
                "runner": "airtest",
                "effect": "read",
                "timeout_seconds": 10,
                "action": "click-image",
                "template": str(icon.resolve()),
                "threshold": 0.85,
                "assertion": {
                    "kind": "image-visible",
                    "template": str(result.resolve()),
                    "threshold": 0.85,
                    "timeout_seconds": 10,
                },
            },
        ],
        "decision": "generate-and-replay",
        "effective_path_confirmed": True,
    }
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(value), encoding="utf-8")
    request = load_request(request_path.resolve())
    return compile_bundle(request, (tmp_path / "outputs").resolve())


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return _make_bundle(tmp_path)


@pytest.fixture
def destructive_bundle(tmp_path: Path) -> Path:
    return _make_bundle(tmp_path, destructive=True)


def _make_component_bundle(tmp_path: Path, *, with_recovery: bool = True) -> Path:
    base = _make_bundle(tmp_path)
    shutil_target = tmp_path / "component-source"
    shutil_target.mkdir()
    script = shutil_target / "ensure-home.ad"
    script.write_text("context platform=android\nopen com.example.app\n", encoding="utf-8")
    recovery = shutil_target / "recovery.ad"
    recovery.write_text("context platform=android\nback\n", encoding="utf-8")
    value = manifest(base)
    request_path = tmp_path / "request.json"
    request_value = {
        "schema_version": "1.0",
        "name": "component-demo",
        "description": "Component bundle.",
        "source_surface": "agent-device-cli",
        "target_platform": "android",
        "device": value["device"],
        "parameters": [],
        "steps": [{
            "id": "ensure-home",
            "runner": "agent-device",
            "effect": "idempotent",
            "timeout_seconds": 10,
            "script": str(script.resolve()),
            "assertion": {
                "kind": "selector-visible",
                "selector": "id=home-marker",
                "timeout_seconds": 2,
            },
        }],
        "decision": "generate-only",
        "effective_path_confirmed": True,
    }
    request_path.write_text(json.dumps(request_value), encoding="utf-8")
    request = load_request(request_path.resolve())
    lifecycle = ComponentLifecycleSpec(
        app_id="example-app",
        component_id="ensure-home",
        version=1,
        occurrence_id="c001-ensure-home",
        descriptor_sha256="a" * 64,
        text_input_mode="direct-ime",
        implementation="default",
        parameter_map={},
        precondition=AssertionSpec(
            kind="selector-visible", selector="id=start-marker", template=None,
            threshold=None, timeout_seconds=1.0,
        ),
        already_complete=AssertionSpec(
            kind="selector-visible", selector="id=home-marker", template=None,
            threshold=None, timeout_seconds=1.0,
        ),
        recovery=(
            ComponentRecoveryStepSpec(
                script=recovery,
                postcondition=AssertionSpec(
                    kind="selector-visible", selector="id=recovery-marker", template=None,
                    threshold=None, timeout_seconds=2.0,
                ),
            )
            if with_recovery
            else None
        ),
    )
    request = replace(
        request,
        steps=(replace(request.steps[0], component=lifecycle),),
    )
    return compile_bundle(request, (tmp_path / "component-outputs").resolve())


@pytest.fixture
def component_bundle(tmp_path: Path) -> Path:
    return _make_component_bundle(tmp_path)


class FakeAgent:
    def __init__(self, events: list[tuple[int, str, str]]) -> None:
        self.events = events
        self.serial = "emulator-5554"
        self.closed: list[str] = []
        self.closed_state_dirs: list[Path | None] = []
        self.replay_state_dirs: list[Path | None] = []
        self.devices_calls = 0
        self.parameters: dict[str, str] = {}
        self.state_dir = Path("C:/agent-device-state")
        self.replay_error: RuntimeFailure | None = None
        self.selector_error: RuntimeFailure | None = None
        self.replay_errors: dict[str, RuntimeFailure] = {}
        self.visible: set[str] | None = None
        self.replays: list[str] = []
        self.waits: list[str] = []
        self.recovery_satisfies_precondition = True
        self.recovery_satisfies_postcondition = True
        self.action_satisfies_postcondition = True

    def devices(self):
        self.devices_calls += 1
        return (
            AndroidDevice(
                serial=self.serial,
                name="Pixel",
                kind="emulator",
                claimed_by=None,
                viewport=VIEWPORT,
            ),
        )

    def replay_ad(self, script, *, serial, session, parameters, state_dir=None):
        self.replay_state_dirs.append(state_dir)
        step_id = Path(script).stem.split("-", 1)[1]
        self.replays.append(step_id)
        if self.replay_error:
            raise self.replay_error
        if step_id in self.replay_errors:
            raise self.replay_errors[step_id]
        self.events.append((len(self.events) + 1, "agent-device", step_id))
        self.parameters = dict(parameters)
        selected_state_dir = state_dir or self.state_dir
        self.state_dir = selected_state_dir
        if self.visible is not None:
            if step_id.endswith("-recovery"):
                if self.recovery_satisfies_postcondition:
                    self.visible.add("id=recovery-marker")
                if self.recovery_satisfies_precondition:
                    self.visible.add("id=start-marker")
            elif self.action_satisfies_postcondition:
                self.visible.add("id=home-marker")
        return {"status": "replayed", "_state_dir": str(selected_state_dir)}

    def wait_selector(
        self, selector, *, serial, session, timeout_seconds, state_dir=None
    ):
        self.waits.append(selector)
        if self.selector_error:
            raise self.selector_error
        if not self.replay_state_dirs:
            self.state_dir = state_dir
        assert state_dir == self.state_dir
        if self.visible is not None and selector not in self.visible:
            raise RuntimeFailure("selector_not_found")
        return {"status": "visible"}

    def close_session(self, session, *, serial, state_dir=None):
        assert serial == self.serial
        expected = self.replay_state_dirs[-1] if self.replay_state_dirs else None
        assert state_dir in {None, self.state_dir, expected}
        self.closed.append(session)
        self.closed_state_dirs.append(state_dir)


class FakeAirtest:
    def __init__(self, events: list[tuple[int, str, str]]) -> None:
        self.events = events
        self.action_error: RuntimeFailure | None = None
        self.assertion_error: RuntimeFailure | None = None
        self.failure_file: Path | None = None
        self.click_calls: list[dict[str, object]] = []

    def click_image(self, *, action_id, evidence_dir, **kwargs):
        self.click_calls.append(
            {"action_id": action_id, "evidence_dir": evidence_dir, **kwargs}
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        before = evidence_dir / f"{action_id}-before.png"
        before.write_bytes(b"before")
        if self.failure_file is not None:
            self.failure_file = evidence_dir / "failure.png"
            self.failure_file.write_bytes(b"failure")
        if self.action_error:
            raise self.action_error
        self.events.append((len(self.events) + 1, "airtest", action_id))
        after = evidence_dir / f"{action_id}-after.png"
        after.write_bytes(b"after")
        return {
            "status": "clicked",
            "position": [120, 240],
            "confidence": 0.91,
            "before_path": str(before),
            "after_path": str(after),
        }

    def locate_image(self, **kwargs):
        if self.action_error:
            raise self.action_error
        return {"status": "located", "position": [120, 240], "confidence": 0.91}

    def assert_image_visible(self, **kwargs):
        if self.assertion_error:
            raise self.assertion_error
        return {"status": "visible", "confidence": 0.92}


@pytest.fixture
def runtime_fakes():
    events: list[tuple[int, str, str]] = []
    return FakeAgent(events), FakeAirtest(events), events


def read_result(bundle: Path) -> dict[str, object]:
    return json.loads((bundle / "run-result.json").read_text("utf-8"))


def manifest(bundle: Path) -> dict[str, object]:
    return yaml.safe_load((bundle / "workflow.yaml").read_text("utf-8"))


def test_preview_validates_and_summarizes_without_device_input(bundle: Path) -> None:
    result = preview_bundle(bundle)
    assert result["serial"] == "emulator-5554"
    assert result["transport"] == "emulator"
    assert [step["id"] for step in result["steps"]] == [
        "open-settings",
        "tap-settings-icon",
    ]
    assert result["destructive_step_ids"] == []


def test_preview_exposes_only_component_identity(component_bundle: Path) -> None:
    result = preview_bundle(component_bundle)
    assert result["steps"][0]["component"] == {
        "app_id": "example-app",
        "id": "ensure-home",
        "version": 1,
        "occurrence_id": "c001-ensure-home",
    }


def test_recovery_hash_is_bound_to_bundle_integrity(component_bundle: Path) -> None:
    recovery = component_bundle / "flows/001-ensure-home-recovery.ad"
    recovery.write_text("context platform=android\nback\nback\n", encoding="utf-8")
    with pytest.raises(RuntimeFailure, match="bundle_integrity_mismatch"):
        preview_bundle(component_bundle)


def test_component_already_complete_skips_recovery_and_action(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = {"id=home-marker"}
    fake_agent.replay_error = RuntimeFailure("must_not_replay")
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 0
    assert fake_agent.replays == []
    assert read_result(component_bundle)["steps"][0]["status"] == "already-complete"
    assert len(fake_agent.closed) == 1
    assert fake_agent.closed_state_dirs[0] is not None


def test_component_executes_action_when_precondition_is_visible(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = {"id=start-marker"}
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 0
    assert fake_agent.replays == ["ensure-home"]
    assert read_result(component_bundle)["steps"][0]["status"] == "component-verified"


def test_component_runs_one_recovery_then_action(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = set()
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 0
    assert fake_agent.replays == ["ensure-home-recovery", "ensure-home"]
    assert fake_agent.replays.count("ensure-home-recovery") == 1


def test_component_without_recovery_fails_precondition(
    tmp_path: Path, runtime_fakes
) -> None:
    component_bundle = _make_component_bundle(tmp_path, with_recovery=False)
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = set()
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 2
    assert fake_agent.replays == []
    assert read_result(component_bundle)["error_code"] == "component_precondition_failed"


def test_component_recovery_postcondition_failure_is_bounded_and_cleans_session(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = set()
    fake_agent.recovery_satisfies_postcondition = False
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 2
    assert fake_agent.replays == ["ensure-home-recovery"]
    assert read_result(component_bundle)["error_code"] == "component_recovery_failed"
    assert len(fake_agent.closed) == 1
    assert fake_agent.closed_state_dirs == [fake_agent.replay_state_dirs[0]]


def test_component_precondition_must_be_visible_after_recovery(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = set()
    fake_agent.recovery_satisfies_precondition = False
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 2
    assert fake_agent.replays == ["ensure-home-recovery"]
    assert read_result(component_bundle)["error_code"] == "component_precondition_failed"


def test_component_action_failure_is_not_retried(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = {"id=start-marker"}
    fake_agent.replay_errors["ensure-home"] = RuntimeFailure("device_offline")
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 2
    assert fake_agent.replays == ["ensure-home"]
    assert read_result(component_bundle)["error_code"] == "device_offline"


def test_component_probe_does_not_treat_infrastructure_failure_as_not_visible(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = set()
    fake_agent.selector_error = RuntimeFailure("selector_ambiguous")
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 2
    assert fake_agent.replays == []
    assert read_result(component_bundle)["error_code"] == "selector_ambiguous"


def test_component_postcondition_failure_cleans_session(
    component_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.visible = {"id=start-marker"}
    fake_agent.action_satisfies_postcondition = False
    assert run_bundle(
        component_bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest
    ) == 2
    assert read_result(component_bundle)["error_code"] == "component_postcondition_failed"
    assert len(fake_agent.closed) == 1
    assert fake_agent.closed_state_dirs == [fake_agent.replay_state_dirs[0]]


def test_mixed_steps_execute_in_manifest_order(bundle: Path, runtime_fakes) -> None:
    fake_agent, fake_airtest, events = runtime_fakes
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: runtime-only\n", encoding="utf-8")
    assert (
        run_bundle(
            bundle,
            parameters_path=parameter_file.resolve(),
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 0
    )
    assert events == [
        (1, "agent-device", "open-settings"),
        (2, "airtest", "tap-settings-icon"),
    ]
    assert manifest(bundle)["status"] == "replay-verified"


def test_runner_uses_compact_airtest_evidence_paths(bundle: Path, runtime_fakes) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: runtime-only\n", encoding="utf-8")

    assert run_bundle(
        bundle,
        parameters_path=parameter_file.resolve(),
        agent_runtime=fake_agent,
        airtest_runtime=fake_airtest,
    ) == 0

    call = fake_airtest.click_calls[0]
    assert call["action_id"] == "tap-settings-icon"
    assert call.get("evidence_stem") == "s002"
    assert Path(call["evidence_dir"]).name == "s002"


def test_runner_inspects_viewport_when_device_inventory_omits_it(
    bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    original_devices = fake_agent.devices
    selected = original_devices()[0]
    fake_agent.devices = lambda: (
        AndroidDevice(
            serial=selected.serial,
            name=selected.name,
            kind=selected.kind,
            claimed_by=None,
            viewport=None,
        ),
    )
    fake_airtest.inspect_viewport = lambda serial: VIEWPORT
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: runtime-only\n", encoding="utf-8")

    assert run_bundle(
        bundle,
        parameters_path=parameter_file.resolve(),
        agent_runtime=fake_agent,
        airtest_runtime=fake_airtest,
    ) == 0


def test_modified_asset_fails_before_device_input(bundle: Path, runtime_fakes) -> None:
    fake_agent, fake_airtest, events = runtime_fakes
    (bundle / "images/002-settings-icon.png").write_bytes(b"changed")
    assert run_bundle(bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest) == 2
    assert events == []
    assert read_result(bundle)["error_code"] == "bundle_integrity_mismatch"
    assert manifest(bundle)["status"] == "replay-failed"


@pytest.mark.parametrize("confirmed", [None, "A" * 64, "0" * 64])
def test_destructive_plan_requires_exact_lowercase_digest(
    destructive_bundle: Path, confirmed: str | None, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    assert (
        run_bundle(
            destructive_bundle,
            confirmed_plan_sha256=confirmed,
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 2
    )
    assert read_result(destructive_bundle)["error_code"] == "destructive_authorization_required"


def test_destructive_plan_accepts_exact_digest(
    destructive_bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    parameter_file = destructive_bundle / "parameters.yaml"
    parameter_file.write_text("password: runtime-only\n", encoding="utf-8")
    digest = manifest(destructive_bundle)["plan_sha256"]
    assert (
        run_bundle(
            destructive_bundle,
            parameters_path=parameter_file.resolve(),
            confirmed_plan_sha256=digest,
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 0
    )


def test_legacy_parameterized_destructive_bundle_fails_before_device_input(
    destructive_bundle: Path, runtime_fakes
) -> None:
    workflow_path = destructive_bundle / "workflow.yaml"
    workflow = yaml.safe_load(workflow_path.read_text("utf-8"))
    script_path = destructive_bundle / workflow["steps"][0]["script"]
    script_path.write_text(
        "context platform=android\nclear-data ${query}\n", encoding="utf-8"
    )
    digest = hashlib.sha256(script_path.read_bytes()).hexdigest()
    workflow["steps"][0]["source_sha256"] = digest
    workflow["plan_sha256"] = _manifest_plan_sha256(workflow)
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8"
    )
    metadata_path = destructive_bundle / "metadata.json"
    metadata = json.loads(metadata_path.read_text("utf-8"))
    for asset in metadata["assets"]:
        if asset["path"] == workflow["steps"][0]["script"]:
            asset["sha256"] = digest
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    parameter_file = destructive_bundle / "parameters.yaml"
    parameter_file.write_text("query: com.example.target\npassword: value\n", encoding="utf-8")
    fake_agent, fake_airtest, events = runtime_fakes

    assert run_bundle(
        destructive_bundle,
        parameters_path=parameter_file.resolve(),
        confirmed_plan_sha256=workflow["plan_sha256"],
        agent_runtime=fake_agent,
        airtest_runtime=fake_airtest,
    ) == 2
    assert read_result(destructive_bundle)["error_code"] == (
        "destructive_parameterization_unsupported"
    )
    assert events == []


def test_sensitive_parameters_never_enter_result(bundle: Path, runtime_fakes) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: synthetic-secret-value\n", encoding="utf-8")
    run_bundle(
        bundle,
        parameters_path=parameter_file.resolve(),
        agent_runtime=fake_agent,
        airtest_runtime=fake_airtest,
    )
    assert fake_agent.parameters["password"] == "synthetic-secret-value"
    assert "synthetic-secret-value" not in (bundle / "run-result.json").read_text("utf-8")
    assert "synthetic-secret-value" not in (bundle / "evidence/tool-results.jsonl").read_text("utf-8")


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        ("{}\n", "runtime_failed"),
        ("password: value\nunexpected: value\n", "runtime_failed"),
    ],
)
def test_missing_or_unknown_parameters_fail_before_input(
    bundle: Path, runtime_fakes, contents: str, expected: str
) -> None:
    fake_agent, fake_airtest, events = runtime_fakes
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text(contents, encoding="utf-8")
    assert (
        run_bundle(
            bundle,
            parameters_path=parameter_file.resolve(),
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 2
    )
    assert events == []
    assert read_result(bundle)["error_code"] == expected


def test_changed_runtime_serial_fails_exact_binding(bundle: Path, runtime_fakes) -> None:
    fake_agent, fake_airtest, events = runtime_fakes
    fake_agent.serial = "emulator-5556"
    assert run_bundle(bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest) == 2
    assert events == []
    assert read_result(bundle)["error_code"] == "device_not_found"


def test_selector_assertion_failure_maps_and_closes_session(
    bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.selector_error = RuntimeFailure("selector_not_found")
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: value\n", encoding="utf-8")
    assert (
        run_bundle(
            bundle,
            parameters_path=parameter_file.resolve(),
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 2
    )
    assert read_result(bundle)["error_code"] == "postcondition_failed"
    assert len(fake_agent.closed) == 1


def test_image_assertion_failure_maps_to_postcondition(
    bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_airtest.assertion_error = RuntimeFailure("image_below_threshold")
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: value\n", encoding="utf-8")
    assert (
        run_bundle(
            bundle,
            parameters_path=parameter_file.resolve(),
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 2
    )
    assert read_result(bundle)["error_code"] == "postcondition_failed"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (RuntimeFailure("agent_device_timeout"), "runtime_failed"),
        (RuntimeFailure("replay_diverged"), "replay_diverged"),
    ],
)
def test_upstream_failure_mapping_and_finally_close(
    bundle: Path, runtime_fakes, failure: RuntimeFailure, expected: str
) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_agent.replay_error = failure
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: value\n", encoding="utf-8")
    assert (
        run_bundle(
            bundle,
            parameters_path=parameter_file.resolve(),
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 2
    )
    assert read_result(bundle)["error_code"] == expected
    assert len(fake_agent.closed) == 1
    assert fake_agent.replay_state_dirs[0] is not None
    assert fake_agent.replay_state_dirs[0] == fake_agent.closed_state_dirs[0]
    assert fake_agent.replay_state_dirs[0].is_dir()


def test_runtime_unicode_parameter_without_test_ime_fails_before_device_query(
    tmp_path: Path, runtime_fakes
) -> None:
    bundle = _make_bundle(
        tmp_path,
        script_body=(
            'open com.example.settings\n'
            'fill "id=search editable=true" "${query}"\n'
        ),
    )
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("query: 示例文本\npassword: value\n", encoding="utf-8")
    fake_agent, fake_airtest, _ = runtime_fakes

    assert run_bundle(
        bundle,
        parameters_path=parameter_file.resolve(),
        agent_runtime=fake_agent,
        airtest_runtime=fake_airtest,
    ) == 2
    assert read_result(bundle)["error_code"] == "test_ime_required"
    assert fake_agent.devices_calls == 1
    assert fake_agent.replay_state_dirs == []


def test_failure_screenshot_is_retained(bundle: Path, runtime_fakes) -> None:
    fake_agent, fake_airtest, _ = runtime_fakes
    fake_airtest.action_error = RuntimeFailure("image_not_unique")
    fake_airtest.failure_file = Path("pending")
    parameter_file = bundle / "parameters.yaml"
    parameter_file.write_text("password: value\n", encoding="utf-8")
    assert (
        run_bundle(
            bundle,
            parameters_path=parameter_file.resolve(),
            agent_runtime=fake_agent,
            airtest_runtime=fake_airtest,
        )
        == 2
    )
    assert fake_airtest.failure_file is not None
    assert fake_airtest.failure_file.read_bytes() == b"failure"
    assert read_result(bundle)["error_code"] == "image_not_unique"
    assert manifest(bundle)["status"] == "replay-failed"


def test_manifest_target_mutation_invalidates_plan_digest(
    bundle: Path, runtime_fakes
) -> None:
    fake_agent, fake_airtest, events = runtime_fakes
    value = manifest(bundle)
    value["device"]["serial"] = "emulator-5556"
    (bundle / "workflow.yaml").write_text(
        yaml.safe_dump(value, sort_keys=False), encoding="utf-8"
    )
    assert run_bundle(bundle, agent_runtime=fake_agent, airtest_runtime=fake_airtest) == 2
    assert events == []
    assert read_result(bundle)["error_code"] == "bundle_integrity_mismatch"
