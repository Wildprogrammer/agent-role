from __future__ import annotations

import math
import re
import time
from pathlib import Path
from typing import Any, Callable

from .airtest_runtime import (
    RuntimeFailure,
    _dependencies,
    _focus_window,
    _require_foreground,
    _uia_windows,
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


def locate_image(
    *,
    window_title_regex: str,
    template_path: Path,
    threshold: float = 0.8,
    timeout_seconds: float = 10.0,
    windows_factory: WindowFactory = _uia_windows,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    window_title_regex = _validate_window_title_regex(window_title_regex)
    template, threshold_value, timeout_value = _validate_match_request(
        template_path, threshold, timeout_seconds
    )
    window, dependencies = _bind_window(
        window_title_regex, windows_factory, dependencies_factory
    )
    target = dependencies["Template"](str(template), threshold=threshold_value)
    try:
        position = dependencies["wait"](target, timeout=timeout_value)
    except dependencies["TargetNotFoundError"]:
        return {
            "status": "not-found",
            "window": window.window_text(),
            "template": str(template),
            "threshold": threshold_value,
            "position": None,
        }
    return {
        "status": "matched",
        "window": window.window_text(),
        "template": str(template),
        "threshold": threshold_value,
        "position": [int(position[0]), int(position[1])],
    }


def click_image(
    *,
    window_title_regex: str,
    template_path: Path,
    evidence_dir: Path,
    action_id: str,
    threshold: float = 0.8,
    timeout_seconds: float = 10.0,
    settle_seconds: float = 0.5,
    windows_factory: WindowFactory = _uia_windows,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    window_title_regex = _validate_window_title_regex(window_title_regex)
    template, threshold_value, timeout_value = _validate_match_request(
        template_path, threshold, timeout_seconds
    )
    evidence, before, after = _validate_evidence_request(evidence_dir, action_id)
    settle_value = float(settle_seconds)
    if not math.isfinite(settle_value) or settle_value < 0:
        raise ContractError("settle time must be a finite nonnegative number of seconds")
    evidence.mkdir(parents=True, exist_ok=True)

    window, dependencies = _bind_window(
        window_title_regex, windows_factory, dependencies_factory
    )
    window.capture_as_image().save(before, format="PNG")
    target = dependencies["Template"](str(template), threshold=threshold_value)
    try:
        position = dependencies["wait"](target, timeout=timeout_value)
    except dependencies["TargetNotFoundError"]:
        return {
            "status": "not-found",
            "window": window.window_text(),
            "template": str(template),
            "threshold": threshold_value,
            "position": None,
            "before": str(before),
            "after": None,
        }
    _require_foreground(window)
    dependencies["touch"](position, times=1)
    if settle_value > 0:
        time.sleep(settle_value)
    window.capture_as_image().save(after, format="PNG")
    return {
        "status": "clicked",
        "window": window.window_text(),
        "template": str(template),
        "threshold": threshold_value,
        "position": [int(position[0]), int(position[1])],
        "before": str(before),
        "after": str(after),
    }
