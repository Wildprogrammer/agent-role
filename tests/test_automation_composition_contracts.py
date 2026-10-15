from pathlib import Path

import pytest

from agent_workflow_hub.automation_composition.catalog import (
    component_descriptor_path,
    resolve_inside,
)
from agent_workflow_hub.automation_composition.contracts import (
    CompositionContractError,
    parse_component_reference,
    validate_parameter_mapping,
)


def test_exact_component_reference_and_path(tmp_path: Path) -> None:
    root = (tmp_path / "catalog").resolve()
    root.mkdir()

    assert parse_component_reference("open-search@2") == ("open-search", 2)
    assert component_descriptor_path(root, "open-search", 2) == (
        root / "components" / "open-search" / "v002" / "component.yaml"
    )

    with pytest.raises(
        CompositionContractError, match="component_version_mismatch"
    ):
        parse_component_reference("open-search@latest")


def test_catalog_rejects_relative_absolute_and_symlink_escape(
    tmp_path: Path,
) -> None:
    root = (tmp_path / "catalog").resolve()
    root.mkdir()
    outside = (tmp_path / "outside.png").resolve()
    outside.write_bytes(b"outside")

    with pytest.raises(
        CompositionContractError, match="component_asset_outside_catalog"
    ):
        resolve_inside(root / ".." / "outside.png", root, "template")
    with pytest.raises(
        CompositionContractError, match="component_asset_outside_catalog"
    ):
        resolve_inside(outside, root, "template")

    link = root / "linked.png"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(
        CompositionContractError, match="component_asset_outside_catalog"
    ):
        resolve_inside(link, root, "template")


def test_parameter_mapping_is_exact_and_uses_known_scenario_parameters() -> None:
    assert validate_parameter_mapping(
        component_parameters=("query",),
        mapping={"query": "term"},
        scenario_parameters=("term",),
    ) == {"query": "term"}

    with pytest.raises(
        CompositionContractError, match="component_parameter_invalid"
    ):
        validate_parameter_mapping(
            component_parameters=("query",),
            mapping={},
            scenario_parameters=("term",),
        )


def test_parameter_mapping_rejects_unknown_targets_and_sorts_keys() -> None:
    assert list(
        validate_parameter_mapping(
            component_parameters=("zeta", "alpha"),
            mapping={"zeta": "z", "alpha": "a"},
            scenario_parameters=("a", "z"),
        )
    ) == ["alpha", "zeta"]

    with pytest.raises(
        CompositionContractError, match="component_parameter_invalid"
    ):
        validate_parameter_mapping(
            component_parameters=("query",),
            mapping={"query": "missing"},
            scenario_parameters=("term",),
        )
