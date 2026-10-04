from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from agent_workflow_hub.mobile_device_automation.airtest_runtime import (
    AirtestRuntime,
    RuntimeFailure,
    capture_device,
    click_image,
    crop_template,
    inspect_viewport,
    locate_image,
)
from agent_workflow_hub.mobile_device_automation.models import ViewportSpec


class FakeTemplate:
    def __init__(self, runtime, path: str, threshold: float) -> None:
        self.runtime = runtime
        self.path = path
        self.threshold = threshold

    def match_all_in(self, screen):
        return list(self.runtime.matches)


class FakeAirtest:
    def __init__(self, viewport: ViewportSpec) -> None:
        self.expected = viewport
        self.snapshot_calls = 0
        self.touches: list[tuple[int, int]] = []
        self.init_calls: list[tuple[str, str, dict[str, object]]] = []
        self.current_calls: list[int] = []
        self.matches = [{"result": (120, 240), "confidence": 0.91}]
        self.viewport_after: tuple[int, int, int] | None = None
        self.density_after: float | None = None
        self.current_shape = (viewport.height, viewport.width, 3)
        self.current_density = viewport.density
        self.source_image = np.zeros(self.current_shape, dtype=np.uint8)

    def init_device(self, *, platform: str, uuid: str, **kwargs: object):
        self.init_calls.append((platform, uuid, dict(kwargs)))
        return self

    def set_current(self, index: int) -> None:
        self.current_calls.append(index)

    def device(self):
        return self

    def snapshot(self):
        self.snapshot_calls += 1
        if self.snapshot_calls >= 2 and self.viewport_after is not None:
            self.current_shape = self.viewport_after
        if self.snapshot_calls >= 2 and self.density_after is not None:
            self.current_density = self.density_after
        return np.full(self.current_shape, self.snapshot_calls, dtype=np.uint8)

    def get_display_info(self):
        height, width = self.current_shape[:2]
        return {
            "width": width,
            "height": height,
            "orientation": "portrait" if height >= width else "landscape",
            "density": self.current_density,
        }

    def Template(self, path: str, threshold: float):
        return FakeTemplate(self, path, threshold)

    def touch(self, position, *, times: int) -> None:
        assert times == 1
        self.touches.append(tuple(position))

    def imwrite(self, path: str, image) -> None:
        Path(path).write_bytes(b"PNG" + image.tobytes())

    def imread(self, path: str):
        return self.source_image.copy()

    def dependencies(self) -> dict[str, object]:
        return {
            "Template": self.Template,
            "device": self.device,
            "init_device": self.init_device,
            "set_current": self.set_current,
            "touch": self.touch,
            "imwrite": self.imwrite,
            "imread": self.imread,
        }


@pytest.fixture
def viewport() -> ViewportSpec:
    return ViewportSpec(width=300, height=500, orientation="portrait", density=2.0)


@pytest.fixture
def fake_airtest(viewport: ViewportSpec) -> FakeAirtest:
    return FakeAirtest(viewport)


def template(tmp_path: Path) -> Path:
    path = tmp_path / "icon.png"
    path.write_bytes(b"template")
    return path.resolve()


def test_click_uses_fresh_snapshot_and_unique_match(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    result = click_image(
        serial="emulator-5554",
        expected_viewport=viewport,
        template_path=template(tmp_path),
        evidence_dir=(tmp_path / "evidence").resolve(),
        action_id="open-settings",
        threshold=0.85,
        dependencies_factory=fake_airtest.dependencies,
    )
    assert fake_airtest.snapshot_calls == 3
    assert fake_airtest.touches == [(120, 240)]
    assert result["status"] == "clicked"
    assert result["match_count"] == 1
    assert Path(result["before_path"]).name == "open-settings-before.png"
    assert Path(result["pre_input_path"]).name == "open-settings-pre-input.png"
    assert Path(result["after_path"]).name == "open-settings-after.png"
    assert result["before_sha256"] == hashlib.sha256(
        Path(result["before_path"]).read_bytes()
    ).hexdigest()
    assert result["after_sha256"] == hashlib.sha256(
        Path(result["after_path"]).read_bytes()
    ).hexdigest()
    assert fake_airtest.init_calls == [
        (
            "Android",
            "emulator-5554",
            {
                "cap_method": "ADBCAP",
                "touch_method": "ADBTOUCH",
                "ori_method": "ADBORI",
            },
        )
    ]
    assert fake_airtest.current_calls == [0]


def test_click_can_use_compact_evidence_filenames(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    result = click_image(
        serial="emulator-5554",
        expected_viewport=viewport,
        template_path=template(tmp_path),
        evidence_dir=(tmp_path / "evidence").resolve(),
        action_id="select-milk-tea-suggestion",
        evidence_stem="s002",
        threshold=0.85,
        dependencies_factory=fake_airtest.dependencies,
    )

    assert Path(result["before_path"]).name == "s002-before.png"
    assert Path(result["pre_input_path"]).name == "s002-pre-input.png"
    assert Path(result["after_path"]).name == "s002-after.png"


def test_click_refuses_multiple_matches(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    fake_airtest.matches = [
        {"result": (10, 10), "confidence": 0.91},
        {"result": (40, 40), "confidence": 0.90},
    ]
    with pytest.raises(RuntimeFailure, match="image_not_unique"):
        click_image(
            serial="emulator-5554",
            expected_viewport=viewport,
            template_path=template(tmp_path),
            evidence_dir=(tmp_path / "evidence").resolve(),
            action_id="open-settings",
            threshold=0.85,
            dependencies_factory=fake_airtest.dependencies,
        )
    assert fake_airtest.touches == []


def test_click_refuses_viewport_change_before_input(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    fake_airtest.viewport_after = (viewport.width, viewport.height, 3)
    with pytest.raises(RuntimeFailure, match="viewport_changed"):
        click_image(
            serial="emulator-5554",
            expected_viewport=viewport,
            template_path=template(tmp_path),
            evidence_dir=(tmp_path / "evidence").resolve(),
            action_id="open-settings",
            threshold=0.85,
            dependencies_factory=fake_airtest.dependencies,
        )
    assert fake_airtest.touches == []


def test_click_refuses_density_change_before_input(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    fake_airtest.density_after = 3.0
    with pytest.raises(RuntimeFailure, match="viewport_changed"):
        click_image(
            serial="emulator-5554",
            expected_viewport=viewport,
            template_path=template(tmp_path),
            evidence_dir=(tmp_path / "evidence").resolve(),
            action_id="open-settings",
            threshold=0.85,
            dependencies_factory=fake_airtest.dependencies,
        )
    assert fake_airtest.touches == []


@pytest.mark.parametrize(
    ("matches", "expected"),
    [
        ([], "image_below_threshold"),
        ([{"result": (10, 10), "confidence": 0.80}], "image_below_threshold"),
    ],
)
def test_locate_rejects_no_or_below_threshold_match(
    fake_airtest: FakeAirtest,
    viewport: ViewportSpec,
    tmp_path: Path,
    matches,
    expected: str,
) -> None:
    fake_airtest.matches = matches
    with pytest.raises(RuntimeFailure, match=expected):
        locate_image(
            serial="emulator-5554",
            expected_viewport=viewport,
            template_path=template(tmp_path),
            threshold=0.85,
            timeout_seconds=1,
            dependencies_factory=fake_airtest.dependencies,
        )


@pytest.mark.parametrize("threshold", [float("nan"), float("inf"), 0, 1.1, True])
def test_locate_rejects_invalid_threshold(
    fake_airtest: FakeAirtest,
    viewport: ViewportSpec,
    tmp_path: Path,
    threshold,
) -> None:
    with pytest.raises(RuntimeFailure, match="invalid_threshold"):
        locate_image(
            serial="emulator-5554",
            expected_viewport=viewport,
            template_path=template(tmp_path),
            threshold=threshold,
            timeout_seconds=1,
            dependencies_factory=fake_airtest.dependencies,
        )


def test_capture_preserves_wireless_serial_and_hashes_screenshot(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    output = (tmp_path / "capture.png").resolve()
    result = capture_device(
        serial="192.0.2.10:5555",
        expected_viewport=viewport,
        output_path=output,
        dependencies_factory=fake_airtest.dependencies,
    )
    assert fake_airtest.init_calls == [
        (
            "Android",
            "192.0.2.10:5555",
            {
                "cap_method": "ADBCAP",
                "touch_method": "ADBTOUCH",
                "ori_method": "ADBORI",
            },
        )
    ]
    assert result["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert result["orientation"] == "portrait"
    assert result["density"] == 2.0


def test_inspect_viewport_uses_fresh_android_framebuffer(
    fake_airtest: FakeAirtest, viewport: ViewportSpec
) -> None:
    assert inspect_viewport(
        serial="emulator-5554",
        dependencies_factory=fake_airtest.dependencies,
    ) == viewport
    assert fake_airtest.snapshot_calls == 1


def test_capture_rejects_existing_output(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    output = (tmp_path / "capture.png").resolve()
    output.write_bytes(b"stale")
    with pytest.raises(RuntimeFailure, match="output_exists"):
        capture_device(
            serial="emulator-5554",
            expected_viewport=viewport,
            output_path=output,
            dependencies_factory=fake_airtest.dependencies,
        )
    assert output.read_bytes() == b"stale"


@pytest.mark.parametrize("box", [(0, 0, 0, 10), (-1, 0, 10, 10), (290, 0, 20, 10)])
def test_crop_rejects_invalid_bounds(
    fake_airtest: FakeAirtest,
    viewport: ViewportSpec,
    tmp_path: Path,
    box,
) -> None:
    source = (tmp_path / "source.png").resolve()
    source.write_bytes(b"source")
    with pytest.raises(RuntimeFailure, match="invalid_crop"):
        crop_template(
            source_screenshot=source,
            output_path=(tmp_path / "crop.png").resolve(),
            crop_box=box,
            viewport=viewport,
            threshold=0.85,
            description="Settings icon",
            dependencies_factory=fake_airtest.dependencies,
        )


def test_crop_writes_metadata_without_serial(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    source = (tmp_path / "source.png").resolve()
    source.write_bytes(b"source")
    output = (tmp_path / "crop.png").resolve()
    result = crop_template(
        source_screenshot=source,
        output_path=output,
        crop_box=(10, 20, 30, 40),
        viewport=viewport,
        threshold=0.85,
        description="Settings icon",
        dependencies_factory=fake_airtest.dependencies,
    )
    metadata = json.loads(Path(result["metadata_path"]).read_text(encoding="utf-8"))
    assert metadata["crop_box"] == {"x": 10, "y": 20, "width": 30, "height": 40}
    assert metadata["source_sha256"] == hashlib.sha256(b"source").hexdigest()
    assert "serial" not in json.dumps(metadata).casefold()


def test_runtime_exposes_same_closed_operations(
    fake_airtest: FakeAirtest, viewport: ViewportSpec, tmp_path: Path
) -> None:
    runtime = AirtestRuntime(dependencies_factory=fake_airtest.dependencies)
    result = runtime.locate_image(
        serial="emulator-5554",
        expected_viewport=viewport,
        template_path=template(tmp_path),
        threshold=0.85,
        timeout_seconds=1,
    )
    assert result["position"] == [120, 240]
