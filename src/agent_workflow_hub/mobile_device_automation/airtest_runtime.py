from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .agent_device_runtime import RuntimeFailure
from .models import ViewportSpec


ACTION_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _dependencies() -> dict[str, object]:
    from airtest.aircv import imread, imwrite
    from airtest.core.api import Template, device, init_device, set_current, touch

    return {
        "Template": Template,
        "device": device,
        "init_device": init_device,
        "set_current": set_current,
        "touch": touch,
        "imwrite": imwrite,
        "imread": imread,
    }


DependenciesFactory = Callable[[], Mapping[str, object]]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_positive(value: object, code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RuntimeFailure(code)
    return float(value)


def _valid_threshold(value: object) -> float:
    threshold = _finite_positive(value, "invalid_threshold")
    if threshold > 1:
        raise RuntimeFailure("invalid_threshold")
    return threshold


def _absolute_file(path: Path, *, suffixes: set[str], code: str) -> Path:
    if (
        not path.is_absolute()
        or not path.is_file()
        or path.suffix.casefold() not in suffixes
    ):
        raise RuntimeFailure(code)
    return path.resolve()


def _output_path(path: Path, *, suffix: str = ".png") -> Path:
    if not path.is_absolute() or path.suffix.casefold() != suffix:
        raise RuntimeFailure("invalid_output_path")
    fixed = path.resolve(strict=False)
    if fixed.exists():
        raise RuntimeFailure("output_exists")
    fixed.parent.mkdir(parents=True, exist_ok=True)
    return fixed


def _bind(serial: str, dependencies: Mapping[str, object]) -> object:
    if not isinstance(serial, str) or not serial or any(char in serial for char in "\r\n"):
        raise RuntimeFailure("invalid_serial")
    init_device = dependencies["init_device"]
    set_current = dependencies["set_current"]
    init_device(  # type: ignore[operator]
        platform="Android",
        uuid=serial,
        cap_method="ADBCAP",
        touch_method="ADBTOUCH",
        ori_method="ADBORI",
    )
    set_current(0)  # type: ignore[operator]
    return dependencies["device"]()  # type: ignore[operator]


def _screen_identity(screen: object, device_object: object) -> dict[str, object]:
    shape = getattr(screen, "shape", None)
    if not isinstance(shape, tuple) or len(shape) < 2:
        raise RuntimeFailure("invalid_screenshot")
    height, width = shape[:2]
    if (
        isinstance(width, bool)
        or isinstance(height, bool)
        or not isinstance(width, int)
        or not isinstance(height, int)
        or width <= 0
        or height <= 0
    ):
        raise RuntimeFailure("invalid_screenshot")
    orientation = "portrait" if height >= width else "landscape"
    display_info = getattr(device_object, "get_display_info", None)
    if not callable(display_info):
        raise RuntimeFailure("viewport_changed: device density unavailable")
    info = display_info()
    if not isinstance(info, dict):
        raise RuntimeFailure("viewport_changed: invalid display info")
    density = info.get("density")
    density_value = _finite_positive(density, "viewport_changed: invalid density")
    return {
        "width": width,
        "height": height,
        "orientation": orientation,
        "density": density_value,
    }


def _validate_viewport(
    screen: object, device_object: object, expected: ViewportSpec
) -> dict[str, object]:
    identity = _screen_identity(screen, device_object)
    if (
        identity["width"] != expected.width
        or identity["height"] != expected.height
        or identity["orientation"] != expected.orientation
        or not math.isclose(float(identity["density"]), expected.density, rel_tol=0, abs_tol=1e-6)
    ):
        raise RuntimeFailure("viewport_changed")
    return identity


def inspect_viewport(
    *,
    serial: str,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> ViewportSpec:
    dependencies = dependencies_factory()
    device_object = _bind(serial, dependencies)
    screen = device_object.snapshot()
    identity = _screen_identity(screen, device_object)
    return ViewportSpec(
        width=int(identity["width"]),
        height=int(identity["height"]),
        orientation=identity["orientation"],  # type: ignore[arg-type]
        density=float(identity["density"]),
    )


def _write_exclusive_image(
    path: Path, image: object, dependencies: Mapping[str, object]
) -> None:
    try:
        with path.open("xb"):
            pass
    except FileExistsError as exc:
        raise RuntimeFailure("output_exists") from exc
    try:
        dependencies["imwrite"](str(path), image)  # type: ignore[operator]
    except Exception:
        path.unlink(missing_ok=True)
        raise
    if not path.is_file() or path.stat().st_size == 0:
        path.unlink(missing_ok=True)
        raise RuntimeFailure("image_write_failed")


def _eligible_matches(
    screen: object,
    *,
    template_path: Path,
    threshold: float,
    viewport: ViewportSpec,
    dependencies: Mapping[str, object],
    match_policy: str = "unique",
) -> list[dict[str, object]]:
    template = dependencies["Template"](str(template_path), threshold=threshold)  # type: ignore[operator]
    raw_matches = template.match_all_in(screen)
    if raw_matches is None:
        raw_matches = []
    if not isinstance(raw_matches, list):
        raise RuntimeFailure("image_match_invalid")
    eligible: list[dict[str, object]] = []
    for raw in raw_matches:
        if not isinstance(raw, dict):
            continue
        confidence = raw.get("confidence")
        position = raw.get("result")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or float(confidence) < threshold
            or not isinstance(position, (tuple, list))
            or len(position) != 2
        ):
            continue
        x, y = position
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, (int, float))
            or not isinstance(y, (int, float))
            or not math.isfinite(x)
            or not math.isfinite(y)
            or not 0 <= x < viewport.width
            or not 0 <= y < viewport.height
        ):
            raise RuntimeFailure("image_match_out_of_bounds")
        eligible.append(
            {"position": (int(round(x)), int(round(y))), "confidence": float(confidence)}
        )
    if not eligible:
        raise RuntimeFailure("image_below_threshold")
    eligible.sort(key=lambda item: float(item["confidence"]), reverse=True)
    if match_policy not in {"best", "unique"}:
        raise RuntimeFailure("invalid_match_policy")
    if match_policy == "unique" and len(eligible) != 1:
        raise RuntimeFailure("image_not_unique")
    return eligible


def _offset_position(
    position: object,
    click_offset: tuple[int, int] | None,
    viewport: ViewportSpec,
) -> tuple[int, int]:
    if (
        not isinstance(position, (tuple, list))
        or len(position) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in position)
    ):
        raise RuntimeFailure("image_match_invalid")
    offset_x, offset_y = click_offset or (0, 0)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (offset_x, offset_y)):
        raise RuntimeFailure("invalid_click_offset")
    x = int(position[0]) + offset_x
    y = int(position[1]) + offset_y
    if not 0 <= x < viewport.width or not 0 <= y < viewport.height:
        raise RuntimeFailure("click_offset_out_of_bounds")
    return x, y


def capture_device(
    *,
    serial: str,
    expected_viewport: ViewportSpec,
    output_path: Path,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    output = _output_path(output_path)
    dependencies = dependencies_factory()
    device_object = _bind(serial, dependencies)
    screen = device_object.snapshot()
    identity = _validate_viewport(screen, device_object, expected_viewport)
    _write_exclusive_image(output, screen, dependencies)
    return {
        "status": "captured",
        "path": str(output),
        "sha256": _sha256(output),
        **identity,
    }


def crop_template(
    *,
    source_screenshot: Path,
    output_path: Path,
    crop_box: tuple[int, int, int, int],
    viewport: ViewportSpec,
    threshold: float,
    description: str,
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    source = _absolute_file(
        source_screenshot, suffixes={".png", ".jpg", ".jpeg"}, code="invalid_source_image"
    )
    output = _output_path(output_path)
    metadata_path = Path(str(output) + ".metadata.json")
    if metadata_path.exists():
        raise RuntimeFailure("output_exists")
    if (
        not isinstance(crop_box, tuple)
        or len(crop_box) != 4
        or any(isinstance(value, bool) or not isinstance(value, int) for value in crop_box)
    ):
        raise RuntimeFailure("invalid_crop")
    x, y, width, height = crop_box
    if (
        x < 0
        or y < 0
        or width <= 0
        or height <= 0
        or x + width > viewport.width
        or y + height > viewport.height
    ):
        raise RuntimeFailure("invalid_crop")
    threshold_value = _valid_threshold(threshold)
    if not isinstance(description, str) or not description.strip():
        raise RuntimeFailure("invalid_description")
    dependencies = dependencies_factory()
    image = dependencies["imread"](str(source))  # type: ignore[operator]
    shape = getattr(image, "shape", None)
    if not isinstance(shape, tuple) or tuple(shape[:2]) != (viewport.height, viewport.width):
        raise RuntimeFailure("viewport_changed")
    crop = image[y : y + height, x : x + width]
    _write_exclusive_image(output, crop, dependencies)
    metadata = {
        "schema_version": "1.0",
        "source_sha256": _sha256(source),
        "template_sha256": _sha256(output),
        "crop_box": {"x": x, "y": y, "width": width, "height": height},
        "viewport": {
            "width": viewport.width,
            "height": viewport.height,
            "orientation": viewport.orientation,
            "density": viewport.density,
        },
        "threshold": threshold_value,
        "description": description.strip(),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    try:
        with metadata_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(metadata, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
    except FileExistsError as exc:
        output.unlink(missing_ok=True)
        raise RuntimeFailure("output_exists") from exc
    return {
        "status": "cropped",
        "path": str(output),
        "sha256": metadata["template_sha256"],
        "metadata_path": str(metadata_path),
    }


def locate_image(
    *,
    serial: str,
    expected_viewport: ViewportSpec,
    template_path: Path,
    threshold: float,
    timeout_seconds: float,
    match_policy: str = "unique",
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    template = _absolute_file(
        template_path, suffixes={".png", ".jpg", ".jpeg"}, code="invalid_template"
    )
    threshold_value = _valid_threshold(threshold)
    _finite_positive(timeout_seconds, "invalid_timeout")
    dependencies = dependencies_factory()
    device_object = _bind(serial, dependencies)
    screen = device_object.snapshot()
    _validate_viewport(screen, device_object, expected_viewport)
    matches = _eligible_matches(
        screen,
        template_path=template,
        threshold=threshold_value,
        viewport=expected_viewport,
        dependencies=dependencies,
        match_policy=match_policy,
    )
    match = matches[0]
    return {
        "status": "located",
        "match_count": len(matches),
        "match_policy": match_policy,
        "position": list(match["position"]),
        "confidence": match["confidence"],
        "threshold": threshold_value,
        "template_sha256": _sha256(template),
    }


def click_image(
    *,
    serial: str,
    expected_viewport: ViewportSpec,
    template_path: Path,
    evidence_dir: Path,
    action_id: str,
    evidence_stem: str | None = None,
    threshold: float,
    timeout_seconds: float = 10.0,
    match_policy: str = "best",
    click_offset: tuple[int, int] | None = None,
    evidence_mode: str = "always",
    dependencies_factory: DependenciesFactory = _dependencies,
) -> dict[str, object]:
    template = _absolute_file(
        template_path, suffixes={".png", ".jpg", ".jpeg"}, code="invalid_template"
    )
    threshold_value = _valid_threshold(threshold)
    _finite_positive(timeout_seconds, "invalid_timeout")
    if not ACTION_ID.fullmatch(action_id):
        raise RuntimeFailure("invalid_action_id")
    stem = action_id if evidence_stem is None else evidence_stem
    if not ACTION_ID.fullmatch(stem):
        raise RuntimeFailure("invalid_evidence_stem")
    if not evidence_dir.is_absolute():
        raise RuntimeFailure("invalid_evidence_dir")
    evidence = evidence_dir.resolve(strict=False)
    if evidence_mode not in {"always", "on-failure"}:
        raise RuntimeFailure("invalid_evidence_mode")
    before_path = evidence / f"{stem}-before.png"
    pre_input_path = evidence / f"{stem}-pre-input.png"
    after_path = evidence / f"{stem}-after.png"
    if evidence_mode == "always" and any(
        path.exists() for path in (before_path, after_path)
    ):
        raise RuntimeFailure("output_exists")

    dependencies = dependencies_factory()
    device_object = _bind(serial, dependencies)

    pre_input = device_object.snapshot()
    dispatch_started = False
    try:
        _validate_viewport(pre_input, device_object, expected_viewport)
        matches = _eligible_matches(
            pre_input,
            template_path=template,
            threshold=threshold_value,
            viewport=expected_viewport,
            dependencies=dependencies,
            match_policy=match_policy,
        )
        match = matches[0]
        position = _offset_position(match["position"], click_offset, expected_viewport)
        if evidence_mode == "always":
            evidence.mkdir(parents=True, exist_ok=True)
            _write_exclusive_image(before_path, pre_input, dependencies)
        dispatch_started = True
        dependencies["touch"](position, times=1)  # type: ignore[operator]
    except Exception as exc:
        if evidence_mode == "on-failure":
            evidence.mkdir(parents=True, exist_ok=True)
            _write_exclusive_image(pre_input_path, pre_input, dependencies)
        if dispatch_started:
            raise RuntimeFailure(f"action_dispatch_unknown:{exc}") from exc
        raise

    result: dict[str, object] = {
        "status": "clicked",
        "evidence_policy": evidence_mode,
        "match_count": len(matches),
        "match_policy": match_policy,
        "position": list(position),
        "confidence": match["confidence"],
        "threshold": threshold_value,
        "template_sha256": _sha256(template),
    }
    if evidence_mode == "always":
        after = device_object.snapshot()
        _write_exclusive_image(after_path, after, dependencies)
        result.update(
            {
                "before_path": str(before_path),
                "before_sha256": _sha256(before_path),
                "after_path": str(after_path),
                "after_sha256": _sha256(after_path),
            }
        )
    return result


def click_image_verified(
    *,
    serial: str,
    expected_viewport: ViewportSpec,
    template_path: Path,
    postcondition_template_path: Path,
    evidence_dir: Path,
    action_id: str,
    threshold: float,
    postcondition_threshold: float,
    timeout_seconds: float = 10.0,
    poll_interval_seconds: float = 0.5,
    match_policy: str = "best",
    postcondition_match_policy: str = "best",
    click_offset: tuple[int, int] | None = None,
    dependencies_factory: DependenciesFactory = _dependencies,
    _bound_device: object | None = None,
) -> dict[str, object]:
    """Click one unique image and verify another, persisting screenshots only on failure."""
    template = _absolute_file(
        template_path, suffixes={".png", ".jpg", ".jpeg"}, code="invalid_template"
    )
    postcondition_template = _absolute_file(
        postcondition_template_path,
        suffixes={".png", ".jpg", ".jpeg"},
        code="invalid_template",
    )
    threshold_value = _valid_threshold(threshold)
    postcondition_threshold_value = _valid_threshold(postcondition_threshold)
    timeout_value = _finite_positive(timeout_seconds, "invalid_timeout")
    poll_interval_value = _finite_positive(poll_interval_seconds, "invalid_timeout")
    if not ACTION_ID.fullmatch(action_id):
        raise RuntimeFailure("invalid_action_id")
    if not evidence_dir.is_absolute():
        raise RuntimeFailure("invalid_evidence_dir")
    evidence = evidence_dir.resolve(strict=False)
    failure_pre_input_path = evidence / f"{action_id}-failure-pre-input.png"
    failure_postcondition_path = evidence / f"{action_id}-failure-postcondition.png"
    if failure_pre_input_path.exists() or failure_postcondition_path.exists():
        raise RuntimeFailure("output_exists")

    dependencies = dependencies_factory()
    device_object = (
        _bound_device if _bound_device is not None else _bind(serial, dependencies)
    )
    pre_input = device_object.snapshot()
    try:
        _validate_viewport(pre_input, device_object, expected_viewport)
        action_matches = _eligible_matches(
            pre_input,
            template_path=template,
            threshold=threshold_value,
            viewport=expected_viewport,
            dependencies=dependencies,
            match_policy=match_policy,
        )
        action_match = action_matches[0]
    except Exception:
        evidence.mkdir(parents=True, exist_ok=True)
        _write_exclusive_image(failure_pre_input_path, pre_input, dependencies)
        raise

    position = _offset_position(
        action_match["position"], click_offset, expected_viewport
    )
    try:
        dependencies["touch"](position, times=1)  # type: ignore[operator]
    except Exception as exc:
        evidence.mkdir(parents=True, exist_ok=True)
        _write_exclusive_image(failure_pre_input_path, pre_input, dependencies)
        raise RuntimeFailure(f"action_dispatch_unknown:{exc}") from exc
    deadline = time.monotonic() + timeout_value
    postcondition_screen: object | None = None
    try:
        while True:
            postcondition_screen = device_object.snapshot()
            _validate_viewport(postcondition_screen, device_object, expected_viewport)
            try:
                postcondition_matches = _eligible_matches(
                    postcondition_screen,
                    template_path=postcondition_template,
                    threshold=postcondition_threshold_value,
                    viewport=expected_viewport,
                    dependencies=dependencies,
                    match_policy=postcondition_match_policy,
                )
                postcondition_match = postcondition_matches[0]
                break
            except RuntimeFailure as exc:
                remaining = deadline - time.monotonic()
                if str(exc) != "image_below_threshold" or remaining <= 0:
                    raise
                time.sleep(min(poll_interval_value, remaining))
    except Exception as exc:
        evidence.mkdir(parents=True, exist_ok=True)
        _write_exclusive_image(failure_pre_input_path, pre_input, dependencies)
        if postcondition_screen is not None:
            _write_exclusive_image(
                failure_postcondition_path, postcondition_screen, dependencies
            )
        if isinstance(exc, RuntimeFailure) and str(exc) in {
            "image_below_threshold",
            "image_not_unique",
        }:
            raise RuntimeFailure(f"postcondition_failed:{exc}") from exc
        raise

    return {
        "status": "verified",
        "evidence_policy": "failure-only",
        "match_count": len(action_matches),
        "match_policy": match_policy,
        "position": list(position),
        "confidence": action_match["confidence"],
        "threshold": threshold_value,
        "template_sha256": _sha256(template),
        "postcondition_match_count": len(postcondition_matches),
        "postcondition_match_policy": postcondition_match_policy,
        "postcondition_confidence": postcondition_match["confidence"],
        "postcondition_threshold": postcondition_threshold_value,
        "postcondition_template_sha256": _sha256(postcondition_template),
    }


def assert_image_visible(**kwargs: object) -> dict[str, object]:
    result = locate_image(**kwargs)  # type: ignore[arg-type]
    return {**result, "status": "visible"}


@dataclass(frozen=True)
class AirtestRuntime:
    dependencies_factory: DependenciesFactory = _dependencies
    _verified_bindings: dict[str, tuple[Mapping[str, object], object]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def _verified_binding(self, serial: str) -> tuple[Mapping[str, object], object]:
        cached = self._verified_bindings.get(serial)
        if cached is not None:
            return cached
        self._verified_bindings.clear()
        dependencies = self.dependencies_factory()
        binding = (dependencies, _bind(serial, dependencies))
        self._verified_bindings[serial] = binding
        return binding

    def inspect_viewport(self, serial: str) -> ViewportSpec:
        _, device_object = self._verified_binding(serial)
        screen = device_object.snapshot()
        identity = _screen_identity(screen, device_object)
        return ViewportSpec(
            width=int(identity["width"]),
            height=int(identity["height"]),
            orientation=identity["orientation"],  # type: ignore[arg-type]
            density=float(identity["density"]),
        )

    def capture_device(self, **kwargs: object) -> dict[str, object]:
        return capture_device(**kwargs, dependencies_factory=self.dependencies_factory)  # type: ignore[arg-type]

    def crop_template(self, **kwargs: object) -> dict[str, object]:
        return crop_template(**kwargs, dependencies_factory=self.dependencies_factory)  # type: ignore[arg-type]

    def locate_image(self, **kwargs: object) -> dict[str, object]:
        return locate_image(**kwargs, dependencies_factory=self.dependencies_factory)  # type: ignore[arg-type]

    def click_image(self, **kwargs: object) -> dict[str, object]:
        return click_image(**kwargs, dependencies_factory=self.dependencies_factory)  # type: ignore[arg-type]

    def click_image_verified(self, **kwargs: object) -> dict[str, object]:
        serial = kwargs.get("serial")
        if not isinstance(serial, str):
            raise RuntimeFailure("invalid_serial")
        dependencies, device_object = self._verified_binding(serial)
        return click_image_verified(  # type: ignore[arg-type]
            **kwargs,
            dependencies_factory=lambda: dependencies,
            _bound_device=device_object,
        )

    def assert_image_visible(self, **kwargs: object) -> dict[str, object]:
        return assert_image_visible(**kwargs, dependencies_factory=self.dependencies_factory)
