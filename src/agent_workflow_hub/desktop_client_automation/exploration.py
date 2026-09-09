from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any, Callable

from .airtest_runtime import (
    RuntimeFailure,
    _primary_display_crop,
    _dependencies,
    _focus_window,
    _require_desktop_foreground,
    _require_foreground,
    _uia_windows,
    _wait_for_unique_desktop_match,
)
from .contracts import ContractError


WindowFactory = Callable[..., list[Any]]
DependenciesFactory = Callable[[], dict[str, Any]]
ACTION_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def _validate_window_title_regex(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("window title regex must be a nonblank valid expression")
    try:
        re.compile(value)
    except re.error as exc:
        raise ContractError(f"window title regex is invalid: {exc}") from exc
    return value


def _validate_match_request(
    template_path: Path,
    threshold: float,
    timeout_seconds: float,
) -> tuple[Path, float, float]:
    template = Path(template_path)
    if (
        not template.is_absolute()
        or not template.is_file()
        or template.suffix.casefold() not in IMAGE_SUFFIXES
    ):
        raise ContractError("template must be an existing absolute PNG or JPEG path")
    threshold_value = float(threshold)
    if not math.isfinite(threshold_value) or not 0 < threshold_value <= 1:
        raise ContractError("threshold must be a finite number greater than 0 and at most 1")
    timeout_value = float(timeout_seconds)
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        raise ContractError("timeout must be a finite positive number of seconds")
    return template, threshold_value, timeout_value


def _validate_evidence_request(
    evidence_dir: Path,
    action_id: str,
) -> tuple[Path, Path, Path]:
    evidence = Path(evidence_dir)
    if not evidence.is_absolute():
        raise ContractError("evidence directory must be an absolute path")
    if not ACTION_ID.fullmatch(action_id):
        raise ContractError(
            "action id must start with a lowercase letter and contain only "
            "lowercase letters, digits, underscores, or hyphens"
        )
    if evidence.exists() and not evidence.is_dir():
        raise ContractError("evidence directory path is not a directory")
    before = evidence / f"{action_id}-before.png"
    after = evidence / f"{action_id}-after.png"
    collisions = [path for path in (before, after) if path.exists()]
    if collisions:
        raise ContractError(f"evidence output already exists: {collisions[0]}")
    return evidence, before, after


def _bind_window(
    window_title_regex: str,
    windows_factory: WindowFactory,
    dependencies_factory: DependenciesFactory,
) -> tuple[Any, dict[str, Any]]:
    windows = windows_factory(title_re=window_title_regex, visible_only=True)
    if len(windows) != 1:
        raise RuntimeFailure(
            f"image exploration requires exactly one matching window; found {len(windows)}"
        )
    window = windows[0]
    _focus_window(window)
    dependencies = dependencies_factory()
    dependencies["init_device"](platform="Windows", uuid=str(window.handle))
    dependencies["set_current"](0)
    return window, dependencies


def _bind_target(
    *,
    desktop: bool,
    display_id: str | None,
    window_title_regex: str | None,
    windows_factory: WindowFactory,
    dependencies_factory: DependenciesFactory,
) -> tuple[Any | None, dict[str, Any], str]:
    if desktop:
        if window_title_regex is not None or display_id != "primary":
            raise ContractError(
                "desktop target requires display_id primary and no window regex"
            )
        dependencies = dependencies_factory()
        dependencies["init_device"](platform="Windows")
        dependencies["set_current"](0)
        _require_desktop_foreground(dependencies["win32gui"])
        return None, dependencies, "desktop:primary"
    if display_id is not None:
        raise ContractError("window target must omit display_id")
    regex = _validate_window_title_regex(window_title_regex or "")
    window, dependencies = _bind_window(
        regex, windows_factory, dependencies_factory
    )
    return window, dependencies, window.window_text()


def _target_field(window: Any | None, target: str) -> dict[str, str]:
    return {"target": target} if window is None else {"window": target}


def _save_primary_desktop(
    dependencies: dict[str, Any],
    output: Path,
    crop: tuple[int, int, int, int] | None = None,
) -> None:
    current = dependencies["device"]()
    screen = current.snapshot()
    image, _, _ = _primary_display_crop(current, screen)
    if crop is not None:
        x, y, width, height = crop
        if x + width > image.shape[1] or y + height > image.shape[0]:
            raise ContractError("capture crop exceeds the primary display")
        image = image[y:y + height, x:x + width]
    dependencies["aircv"].imwrite(str(output), image)


def locate_image(
    *,
    window_title_regex: str | None = None,
    desktop: bool = False,
    display_id: str | None = None,
    template_path: Path,
    threshold: float = 0.8,
    timeout_seconds: float = 10.0,
    windows_factory: WindowFactory = _uia_windows,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    template, threshold_value, timeout_value = _validate_match_request(
        template_path, threshold, timeout_seconds
    )
    window, dependencies, target_name = _bind_target(
        desktop=desktop,
        display_id=display_id,
        window_title_regex=window_title_regex,
        windows_factory=windows_factory,
        dependencies_factory=dependencies_factory,
    )
    target = dependencies["Template"](str(template), threshold=threshold_value)
    try:
        position = (
            _wait_for_unique_desktop_match(dependencies, target, timeout_value)
            if window is None
            else dependencies["wait"](target, timeout=timeout_value)
        )
    except dependencies["TargetNotFoundError"]:
        return {
            "status": "not-found",
            **_target_field(window, target_name),
            "template": str(template),
            "threshold": threshold_value,
            "position": None,
        }
    return {
        "status": "matched",
        **_target_field(window, target_name),
        "template": str(template),
        "threshold": threshold_value,
        "position": [int(position[0]), int(position[1])],
    }


def click_image(
    *,
    window_title_regex: str | None = None,
    desktop: bool = False,
    display_id: str | None = None,
    template_path: Path,
    evidence_dir: Path,
    action_id: str,
    threshold: float = 0.8,
    timeout_seconds: float = 10.0,
    settle_seconds: float = 0.5,
    windows_factory: WindowFactory = _uia_windows,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    template, threshold_value, timeout_value = _validate_match_request(
        template_path, threshold, timeout_seconds
    )
    evidence, before, after = _validate_evidence_request(evidence_dir, action_id)
    settle_value = float(settle_seconds)
    if not math.isfinite(settle_value) or settle_value < 0:
        raise ContractError("settle time must be a finite nonnegative number of seconds")
    evidence.mkdir(parents=True, exist_ok=True)

    window, dependencies, target_name = _bind_target(
        desktop=desktop,
        display_id=display_id,
        window_title_regex=window_title_regex,
        windows_factory=windows_factory,
        dependencies_factory=dependencies_factory,
    )
    if window is None:
        _save_primary_desktop(dependencies, before)
    else:
        window.capture_as_image().save(before, format="PNG")
    target = dependencies["Template"](str(template), threshold=threshold_value)
    try:
        position = (
            _wait_for_unique_desktop_match(dependencies, target, timeout_value)
            if window is None
            else dependencies["wait"](target, timeout=timeout_value)
        )
    except dependencies["TargetNotFoundError"]:
        return {
            "status": "not-found",
            **_target_field(window, target_name),
            "template": str(template),
            "threshold": threshold_value,
            "position": None,
            "before": str(before),
            "after": None,
        }
    if window is None:
        _require_desktop_foreground(dependencies["win32gui"])
    else:
        _require_foreground(window)
    dependencies["touch"](position, times=1)
    if settle_value > 0:
        time.sleep(settle_value)
    if window is None:
        _save_primary_desktop(dependencies, after)
    else:
        window.capture_as_image().save(after, format="PNG")
    return {
        "status": "clicked",
        **_target_field(window, target_name),
        "template": str(template),
        "threshold": threshold_value,
        "position": [int(position[0]), int(position[1])],
        "before": str(before),
        "after": str(after),
    }


def capture_target(
    *,
    output: Path,
    desktop: bool = False,
    display_id: str | None = None,
    window_title_regex: str | None = None,
    crop: tuple[int, int, int, int] | None = None,
    windows_factory: WindowFactory = _uia_windows,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    output = Path(output)
    if not output.is_absolute() or output.suffix.casefold() != ".png":
        raise ContractError("capture output must be an absolute PNG path")
    if output.exists():
        raise ContractError("capture output already exists")
    if crop is not None:
        x, y, width, height = crop
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise ContractError("capture crop coordinates and size are invalid")

    if desktop:
        if window_title_regex is not None or display_id != "primary":
            raise ContractError(
                "desktop target requires display_id primary and no window regex"
            )
        dependencies = dependencies_factory()
        dependencies["init_device"](platform="Windows")
        dependencies["set_current"](0)
        _require_desktop_foreground(dependencies["win32gui"])
        output.parent.mkdir(parents=True, exist_ok=True)
        _save_primary_desktop(dependencies, output, crop)
        target_name = "desktop:primary"
        window = None
    else:
        if display_id is not None:
            raise ContractError("window target must omit display_id")
        regex = _validate_window_title_regex(window_title_regex or "")
        windows = windows_factory(title_re=regex, visible_only=True)
        if len(windows) != 1:
            raise ContractError(
                f"capture requires exactly one matching window; found {len(windows)}"
            )
        window = windows[0]
        _focus_window(window)
        image = window.capture_as_image()
        if crop is not None:
            x, y, width, height = crop
            if x + width > image.width or y + height > image.height:
                raise ContractError("capture crop exceeds the target window")
            image = image.crop((x, y, x + width, y + height))
        output.parent.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG")
        target_name = window.window_text()

    return {
        "status": "captured",
        **_target_field(window, target_name),
        "path": str(output),
        "output": str(output),
    }
