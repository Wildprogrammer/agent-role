from __future__ import annotations

from typing import Any

from agent_workflow_hub.desktop_client_automation.readiness import (
    evaluate_readiness,
)


def _cua(
    status: str = "ready", *, usable_check: str | None = None
) -> dict[str, Any]:
    checks = []
    if usable_check:
        checks.append(
            {
                "name": usable_check,
                "status": "pass",
                "message": "window capture is ready",
            }
        )
    return {
        "status": status,
        "path": "C:/Tools/cua-driver.exe",
        "version": "0.23.2",
        "health": {
            "schema_version": "1",
            "overall": "ok" if status == "ready" else status,
            "checks": checks,
        },
        "error": None,
    }


def _status(
    mode: str,
    *,
    os_name: str = "Windows",
    host: str = "codex",
    cua: dict[str, Any] | None = None,
    cua_mcp: str = "available",
    native: str = "unavailable",
    airtest: bool = True,
) -> str:
    result = evaluate_readiness(
        mode=mode,
        os_name=os_name,
        host=host,
        cua_probe=cua or _cua(),
        cua_mcp=cua_mcp,
        native_computer_use=native,
        airtest_ready=airtest,
    )
    return result["status"]


def test_windows_cua_and_airtest_are_full_ready() -> None:
    result = evaluate_readiness(
        mode="full",
        os_name="Windows",
        host="hermes",
        cua_probe=_cua(),
        cua_mcp="available",
        native_computer_use="unavailable",
        airtest_ready=True,
    )

    assert result == {
        "status": "ready",
        "exploration_backend": "cua-driver-mcp",
        "replay_backend": "airtest",
    }


def test_replay_does_not_require_an_exploration_backend() -> None:
    assert _status(
        "replay",
        cua=_cua("missing"),
        cua_mcp="unavailable",
    ) == "ready"


def test_missing_cua_driver_blocks_exploration() -> None:
    assert _status("explore", cua=_cua("missing")) == "needs-cua-driver"


def test_unknown_mcp_inventory_requires_a_surface_probe() -> None:
    assert _status("explore", cua_mcp="unknown") == "needs-surface-probe"


def test_unavailable_mcp_mapping_is_distinct_from_missing_binary() -> None:
    assert _status("explore", cua_mcp="unavailable") == "needs-cua-mcp"


def test_codex_native_surface_is_an_explicit_compatibility_path() -> None:
    assert _status(
        "explore",
        cua=_cua("missing"),
        cua_mcp="unavailable",
        native="available",
    ) == "ready-native-compatibility"


def test_non_codex_host_cannot_claim_codex_native_compatibility() -> None:
    assert _status(
        "explore",
        host="hermes",
        cua=_cua("missing"),
        cua_mcp="unavailable",
        native="available",
    ) == "needs-cua-driver"


def test_degraded_cua_requires_a_passed_control_or_capture_check() -> None:
    assert _status("explore", cua=_cua("degraded")) == "cua-unavailable"
    assert _status(
        "explore", cua=_cua("degraded", usable_check="screen_capture_capability")
    ) == "ready"
    assert _status(
        "explore", cua=_cua("degraded", usable_check="ax_capability")
    ) == "ready"


def test_windows_replay_requires_airtest() -> None:
    assert _status("replay", airtest=False) == "needs-airtest"
    assert _status("full", airtest=False) == "needs-airtest"


def test_macos_supports_cua_exploration_but_not_airtest_replay() -> None:
    assert _status(
        "explore",
        os_name="Darwin",
        airtest=False,
    ) == "ready"
    assert _status(
        "replay",
        os_name="Darwin",
        airtest=False,
    ) == "needs-replay-backend"
    assert _status(
        "full",
        os_name="Darwin",
        airtest=False,
    ) == "needs-replay-backend"


def test_linux_is_not_claimed_by_the_workflow() -> None:
    assert _status(
        "explore",
        os_name="Linux",
        airtest=False,
    ) == "unsupported-platform"
