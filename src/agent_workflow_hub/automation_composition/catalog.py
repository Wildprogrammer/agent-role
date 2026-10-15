from __future__ import annotations

from pathlib import Path

from .contracts import CompositionContractError, IDENTIFIER


def component_descriptor_path(
    catalog_root: Path, component_id: str, version: int
) -> Path:
    if IDENTIFIER.fullmatch(component_id) is None:
        raise CompositionContractError(
            "component_catalog_invalid: component id must be a lowercase identifier"
        )
    if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
        raise CompositionContractError(
            "component_version_mismatch: version must be a positive integer"
        )
    return (
        catalog_root
        / "components"
        / component_id
        / f"v{version:03d}"
        / "component.yaml"
    )


def resolve_inside(path: Path, root: Path, label: str) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        candidate = path.resolve(strict=False)
        candidate.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise CompositionContractError(
            f"component_asset_outside_catalog: {label} must resolve inside {root}"
        ) from exc
    return candidate
