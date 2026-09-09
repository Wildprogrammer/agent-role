from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from agent_workflow_hub.desktop_client_automation import exploration
from agent_workflow_hub.desktop_client_automation.airtest_runtime import RuntimeFailure
from agent_workflow_hub.desktop_client_automation.contracts import ContractError
from agent_workflow_hub.desktop_client_automation.exploration import (
    click_image,
    locate_image,
)


class FakeWindow:
    handle = 31415

    def __init__(self, events: list[object], *, active: bool = True) -> None:
        self.events = events
        self.active = active
        self.capture_count = 0

    def set_focus(self) -> None:
        self.events.append("focus")

    def window_text(self) -> str:
        return "BambuStudio"

    def is_active(self) -> bool:
        return self.active

    def capture_as_image(self) -> Image.Image:
        self.capture_count += 1
        self.events.append(("capture", self.capture_count))
        color = "red" if self.capture_count == 1 else "green"
        return Image.new("RGB", (24, 16), color)


def write_template(path: Path) -> Path:
    Image.new("RGB", (8, 8), "blue").save(path, format="PNG")
    return path.resolve()


def fake_dependencies(
    events: list[object],
    position: tuple[int, int],
    *,
    found: bool = True,
) -> dict[str, Any]:
    class TargetNotFoundError(Exception):
        pass

    def template(path: str, threshold: float) -> tuple[str, str, float]:
        events.append(("template", path, threshold))
        return ("template", path, threshold)

    def init_device(*, platform: str, uuid: str | None = None) -> None:
        events.append(("init_device", platform, uuid))

    def set_current(index: int) -> None:
        events.append(("set_current", index))

    def wait(target: object, *, timeout: float) -> tuple[int, int]:
        events.append(("wait", target, timeout))
        if not found:
            raise TargetNotFoundError("missing")
        return position

    def touch(
        point: tuple[int, int], *, times: int = 1, right_click: bool = False,
    ) -> None:
        if right_click:
            events.append(("touch", point, times, right_click))
        else:
            events.append(("touch", point, times))

    class Device:
        monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}
        main_monitor = {"left": 0, "top": 0, "width": 1920, "height": 1080}

        def snapshot(self) -> object:
            events.append("desktop-capture")
            return np.zeros((1080, 1920, 3), dtype=np.uint8)

    class Aircv:
        @staticmethod
        def imwrite(path: str, image: object) -> None:
            events.append(("imwrite", path, getattr(image, "shape")))
            Image.fromarray(image).save(path, format="PNG")  # type: ignore[arg-type]

    return {
        "Template": template,
        "TargetNotFoundError": TargetNotFoundError,
        "init_device": init_device,
        "set_current": set_current,
        "wait": wait,
        "touch": touch,
        "device": lambda: Device(),
        "aircv": Aircv,
        "win32gui": object(),
    }


def test_locate_image_uses_primary_desktop_without_window_binding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    template = write_template(tmp_path / "icon.png")
    dependencies = fake_dependencies(events, (0, 0))
    monkeypatch.setattr(exploration, "_require_desktop_foreground", lambda _: None)
    monkeypatch.setattr(
        exploration,
        "_wait_for_unique_desktop_match",
        lambda *args, **kwargs: (42, 84),
    )

    result = locate_image(
        desktop=True,
        display_id="primary",
        window_title_regex=None,
        template_path=template,
        threshold=0.85,
        timeout_seconds=2,
        dependencies_factory=lambda: dependencies,
    )

    assert ("init_device", "Windows", None) in events
    assert result["target"] == "desktop:primary"
    assert result["position"] == [42, 84]


def test_click_image_on_desktop_records_evidence_and_one_touch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    template = write_template(tmp_path / "icon.png")
    evidence = (tmp_path / "evidence").resolve()
    dependencies = fake_dependencies(events, (0, 0))
    monkeypatch.setattr(exploration, "_require_desktop_foreground", lambda _: None)
    monkeypatch.setattr(
        exploration,
        "_wait_for_unique_desktop_match",
        lambda *args, **kwargs: (42, 84),
    )

    result = click_image(
        desktop=True,
        display_id="primary",
        window_title_regex=None,
        template_path=template,
        threshold=0.85,
        timeout_seconds=2,
        evidence_dir=evidence,
        action_id="click-icon",
        settle_seconds=0,
        dependencies_factory=lambda: dependencies,
    )

    assert result["status"] == "clicked"
    assert ("touch", (42, 84), 1) in events
    assert Path(str(result["before"])).is_file()
    assert Path(str(result["after"])).is_file()


def test_desktop_click_not_found_keeps_before_and_never_touches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    template = write_template(tmp_path / "missing.png")
    dependencies = fake_dependencies(events, (0, 0))
    missing = dependencies["TargetNotFoundError"]
    monkeypatch.setattr(exploration, "_require_desktop_foreground", lambda _: None)
    monkeypatch.setattr(
        exploration,
        "_wait_for_unique_desktop_match",
        lambda *args, **kwargs: (_ for _ in ()).throw(missing("missing")),
    )

    result = click_image(
        desktop=True,
        display_id="primary",
        window_title_regex=None,
        template_path=template,
        evidence_dir=(tmp_path / "evidence").resolve(),
        action_id="missing-icon",
        settle_seconds=0,
        dependencies_factory=lambda: dependencies,
    )

    assert result["status"] == "not-found"
    assert result["after"] is None
    assert not any(
        isinstance(event, tuple) and event[0] == "touch" for event in events
    )


def test_desktop_ambiguous_match_never_touches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    template = write_template(tmp_path / "ambiguous.png")
    dependencies = fake_dependencies(events, (0, 0))
    monkeypatch.setattr(exploration, "_require_desktop_foreground", lambda _: None)
    monkeypatch.setattr(
        exploration,
        "_wait_for_unique_desktop_match",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeFailure("ambiguous-match")
        ),
    )

    with pytest.raises(RuntimeFailure, match="ambiguous-match"):
        click_image(
            desktop=True,
            display_id="primary",
            window_title_regex=None,
            template_path=template,
            evidence_dir=(tmp_path / "evidence").resolve(),
            action_id="ambiguous-icon",
            settle_seconds=0,
            dependencies_factory=lambda: dependencies,
        )

    assert not any(
        isinstance(event, tuple) and event[0] == "touch" for event in events
    )


def test_capture_target_saves_primary_desktop_png(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    dependencies = fake_dependencies(events, (0, 0))
    output = (tmp_path / "desktop.png").resolve()
    monkeypatch.setattr(exploration, "_require_desktop_foreground", lambda _: None)

    result = exploration.capture_target(
        output=output,
        desktop=True,
        display_id="primary",
        dependencies_factory=lambda: dependencies,
    )

    assert result["target"] == "desktop:primary"
    assert result["output"] == str(output)
    assert output.is_file()
    assert ("init_device", "Windows", None) in events
    with Image.open(output) as image:
        assert image.size == (1920, 1080)


def test_desktop_click_rechecks_foreground_after_matching(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[object] = []
    checks = 0
    template = write_template(tmp_path / "icon.png")
    evidence = (tmp_path / "evidence").resolve()
    dependencies = fake_dependencies(events, (0, 0))

    def require_foreground(_: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeFailure("desktop-not-foreground")

    monkeypatch.setattr(exploration, "_require_desktop_foreground", require_foreground)
    monkeypatch.setattr(
        exploration,
        "_wait_for_unique_desktop_match",
        lambda *args, **kwargs: (42, 84),
    )

    with pytest.raises(RuntimeFailure, match="desktop-not-foreground"):
        click_image(
            desktop=True,
            display_id="primary",
            window_title_regex=None,
            template_path=template,
            evidence_dir=evidence,
            action_id="click-icon",
            settle_seconds=0,
            dependencies_factory=lambda: dependencies,
        )

    assert checks == 2
    assert (evidence / "click-icon-before.png").is_file()
    assert not (evidence / "click-icon-after.png").exists()
    assert not any(
        isinstance(event, tuple) and event[0] == "touch" for event in events
    )


def test_locate_image_returns_fresh_airtest_match(tmp_path: Path) -> None:
    events: list[object] = []
    window = FakeWindow(events)
    template = write_template(tmp_path / "online-model.png")

    result = locate_image(
        window_title_regex="BambuStudio",
        template_path=template,
        threshold=0.91,
        timeout_seconds=3,
        windows_factory=lambda **criteria: [window],
        dependencies_factory=lambda: fake_dependencies(events, (115, 283)),
    )

    assert result == {
        "status": "matched",
        "window": "BambuStudio",
        "template": str(template),
        "threshold": 0.91,
        "position": [115, 283],
    }
    assert events == [
        "focus",
        ("init_device", "Windows", "31415"),
        ("set_current", 0),
        ("template", str(template), 0.91),
        ("wait", ("template", str(template), 0.91), 3.0),
    ]


def test_click_image_captures_evidence_and_clicks_once(tmp_path: Path) -> None:
    events: list[object] = []
    window = FakeWindow(events)
    template = write_template(tmp_path / "favorite.png")
    evidence_dir = (tmp_path / "evidence").resolve()

    result = click_image(
        window_title_regex="BambuStudio",
        template_path=template,
        evidence_dir=evidence_dir,
        action_id="open-favorite",
        threshold=0.88,
        timeout_seconds=5,
        settle_seconds=0,
        windows_factory=lambda **criteria: [window],
        dependencies_factory=lambda: fake_dependencies(events, (2195, 162)),
    )

    before = evidence_dir / "open-favorite-before.png"
    after = evidence_dir / "open-favorite-after.png"
    assert result == {
        "status": "clicked",
        "window": "BambuStudio",
        "template": str(template),
        "threshold": 0.88,
        "position": [2195, 162],
        "before": str(before),
        "after": str(after),
    }
    assert before.is_file()
    assert after.is_file()
    assert events == [
        "focus",
        ("init_device", "Windows", "31415"),
        ("set_current", 0),
        ("capture", 1),
        ("template", str(template), 0.88),
        ("wait", ("template", str(template), 0.88), 5.0),
        ("touch", (2195, 162), 1),
        ("capture", 2),
    ]


def test_locate_image_reports_not_found_without_coordinates(tmp_path: Path) -> None:
    events: list[object] = []
    window = FakeWindow(events)
    template = write_template(tmp_path / "missing.png")

    result = locate_image(
        window_title_regex="BambuStudio",
        template_path=template,
        windows_factory=lambda **criteria: [window],
        dependencies_factory=lambda: fake_dependencies(events, (0, 0), found=False),
    )

    assert result["status"] == "not-found"
    assert result["position"] is None
    assert not any(event[0] == "touch" for event in events if isinstance(event, tuple))


def test_click_image_not_found_keeps_before_and_never_touches(tmp_path: Path) -> None:
    events: list[object] = []
    window = FakeWindow(events)
    template = write_template(tmp_path / "missing.png")
    evidence_dir = (tmp_path / "evidence").resolve()

    result = click_image(
        window_title_regex="BambuStudio",
        template_path=template,
        evidence_dir=evidence_dir,
        action_id="missing-target",
        settle_seconds=0,
        windows_factory=lambda **criteria: [window],
        dependencies_factory=lambda: fake_dependencies(events, (0, 0), found=False),
    )

    before = evidence_dir / "missing-target-before.png"
    assert result == {
        "status": "not-found",
        "window": "BambuStudio",
        "template": str(template),
        "threshold": 0.8,
        "position": None,
        "before": str(before),
        "after": None,
    }
    assert before.is_file()
    assert not (evidence_dir / "missing-target-after.png").exists()
    assert not any(event[0] == "touch" for event in events if isinstance(event, tuple))


def test_click_image_rechecks_foreground_after_matching_before_touch(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    window = FakeWindow(events)
    template = write_template(tmp_path / "target.png")
    evidence_dir = (tmp_path / "evidence").resolve()
    dependencies = fake_dependencies(events, (100, 200))
    wait = dependencies["wait"]

    def wait_then_lose_focus(target: object, *, timeout: float) -> tuple[int, int]:
        position = wait(target, timeout=timeout)
        window.active = False
        return position

    dependencies["wait"] = wait_then_lose_focus

    with pytest.raises(RuntimeFailure, match="foreground"):
        click_image(
            window_title_regex="BambuStudio",
            template_path=template,
            evidence_dir=evidence_dir,
            action_id="focus-race",
            settle_seconds=0,
            windows_factory=lambda **criteria: [window],
            dependencies_factory=lambda: dependencies,
        )

    assert (evidence_dir / "focus-race-before.png").is_file()
    assert not any(event[0] == "touch" for event in events if isinstance(event, tuple))


@pytest.mark.parametrize("windows", [[], [object(), object()]])
def test_image_exploration_requires_one_visible_window(
    tmp_path: Path, windows: list[object]
) -> None:
    template = write_template(tmp_path / "target.png")

    with pytest.raises(RuntimeFailure, match="exactly one matching window"):
        locate_image(
            window_title_regex="BambuStudio",
            template_path=template,
            windows_factory=lambda **criteria: windows,
            dependencies_factory=lambda: pytest.fail("dependencies must not load"),
        )


def test_image_exploration_rejects_window_that_did_not_become_foreground(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    template = write_template(tmp_path / "target.png")
    window = FakeWindow(events, active=False)

    with pytest.raises(RuntimeFailure, match="foreground"):
        locate_image(
            window_title_regex="BambuStudio",
            template_path=template,
            windows_factory=lambda **criteria: [window],
            dependencies_factory=lambda: pytest.fail("dependencies must not load"),
        )

    assert events == ["focus"]


@pytest.mark.parametrize(
    "template_path",
    [Path("relative.png"), Path("C:/definitely-missing-template.png")],
)
def test_locate_image_rejects_invalid_template_before_ui(
    template_path: Path,
) -> None:
    with pytest.raises(ContractError, match="template"):
        locate_image(
            window_title_regex="BambuStudio",
            template_path=template_path,
            windows_factory=lambda **criteria: pytest.fail("UI must not be touched"),
        )


@pytest.mark.parametrize("window_title_regex", ["", "["])
def test_locate_image_rejects_invalid_window_regex_before_ui(
    tmp_path: Path, window_title_regex: str
) -> None:
    template = write_template(tmp_path / "target.png")

    with pytest.raises(ContractError, match="window title regex"):
        locate_image(
            window_title_regex=window_title_regex,
            template_path=template,
            windows_factory=lambda **criteria: pytest.fail("UI must not be touched"),
        )


@pytest.mark.parametrize("threshold", [0, -0.1, 1.01, float("nan")])
def test_locate_image_rejects_invalid_threshold_before_ui(
    tmp_path: Path, threshold: float
) -> None:
    template = write_template(tmp_path / "target.png")

    with pytest.raises(ContractError, match="threshold"):
        locate_image(
            window_title_regex="BambuStudio",
            template_path=template,
            threshold=threshold,
            windows_factory=lambda **criteria: pytest.fail("UI must not be touched"),
        )


@pytest.mark.parametrize("timeout_seconds", [0, -1])
def test_locate_image_rejects_nonpositive_timeout_before_ui(
    tmp_path: Path, timeout_seconds: float
) -> None:
    template = write_template(tmp_path / "target.png")

    with pytest.raises(ContractError, match="timeout"):
        locate_image(
            window_title_regex="BambuStudio",
            template_path=template,
            timeout_seconds=timeout_seconds,
            windows_factory=lambda **criteria: pytest.fail("UI must not be touched"),
        )


@pytest.mark.parametrize("action_id", ["", "Open-Favorite", "has space", "-leading"])
def test_click_image_rejects_invalid_action_id_before_ui(
    tmp_path: Path, action_id: str
) -> None:
    template = write_template(tmp_path / "target.png")

    with pytest.raises(ContractError, match="action id"):
        click_image(
            window_title_regex="BambuStudio",
            template_path=template,
            evidence_dir=(tmp_path / "evidence").resolve(),
            action_id=action_id,
            windows_factory=lambda **criteria: pytest.fail("UI must not be touched"),
        )


def test_click_image_rejects_relative_evidence_directory_before_ui(
    tmp_path: Path,
) -> None:
    template = write_template(tmp_path / "target.png")

    with pytest.raises(ContractError, match="evidence directory"):
        click_image(
            window_title_regex="BambuStudio",
            template_path=template,
            evidence_dir=Path("relative-evidence"),
            action_id="open-favorite",
            windows_factory=lambda **criteria: pytest.fail("UI must not be touched"),
        )


@pytest.mark.parametrize(
    "collision_name",
    ["open-favorite-before.png", "open-favorite-after.png"],
)
def test_click_image_rejects_evidence_collisions_before_ui(
    tmp_path: Path, collision_name: str
) -> None:
    template = write_template(tmp_path / "target.png")
    evidence_dir = (tmp_path / "evidence").resolve()
    evidence_dir.mkdir()
    (evidence_dir / collision_name).write_bytes(b"occupied")

    with pytest.raises(ContractError, match="already exists"):
        click_image(
            window_title_regex="BambuStudio",
            template_path=template,
            evidence_dir=evidence_dir,
            action_id="open-favorite",
            windows_factory=lambda **criteria: pytest.fail("UI must not be touched"),
        )
