from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, TypedDict


SurfaceState = Literal["available", "unavailable", "unknown"]
Mode = Literal["explore", "replay", "full"]


class Readiness(TypedDict):
    status: str
    exploration_backend: str | None
    replay_backend: str | None


def _degraded_cua_is_usable(cua_probe: Mapping[str, Any]) -> bool:
    health = cua_probe.get("health")
    if not isinstance(health, Mapping):
        return False
    checks = health.get("checks")
    if not isinstance(checks, list):
        return False
    # Cua names the Windows UIA check `ax_capability`, the same canonical name
    # used for macOS AX and Linux AT-SPI.
    usable_checks = {"ax_capability", "screen_capture_capability"}
    return any(
        isinstance(check, Mapping)
        and check.get("name") in usable_checks
        and check.get("status") == "pass"
        for check in checks
    )


def _cua_runtime_is_usable(cua_probe: Mapping[str, Any]) -> bool:
    status = cua_probe.get("status")
    return status == "ready" or (
        status == "degraded" and _degraded_cua_is_usable(cua_probe)
    )


def evaluate_readiness(
    *,
    mode: Mode | str,
    os_name: str,
    host: str,
    cua_probe: Mapping[str, Any],
    cua_mcp: SurfaceState | str,
    native_computer_use: SurfaceState | str,
    airtest_ready: bool,
) -> Readiness:
    if mode not in {"explore", "replay", "full"}:
        raise ValueError("mode must be explore, replay, or full")
    if cua_mcp not in {"available", "unavailable", "unknown"}:
        raise ValueError("cua_mcp has an unsupported state")
    if native_computer_use not in {"available", "unavailable", "unknown"}:
        raise ValueError("native_computer_use has an unsupported state")
    if os_name not in {"Windows", "Darwin"}:
        return {
            "status": "unsupported-platform",
            "exploration_backend": None,
            "replay_backend": None,
        }

    replay_backend = (
        "airtest" if os_name == "Windows" and airtest_ready else None
    )
    cua_usable = _cua_runtime_is_usable(cua_probe)
    if cua_usable and cua_mcp == "available":
        exploration_backend = "cua-driver-mcp"
    elif host == "codex" and native_computer_use == "available":
        exploration_backend = "codex-native-computer-use"
    else:
        exploration_backend = None

    if mode in {"replay", "full"} and os_name == "Darwin":
        return {
            "status": "needs-replay-backend",
            "exploration_backend": exploration_backend,
            "replay_backend": None,
        }
    if mode in {"replay", "full"} and replay_backend is None:
        return {
            "status": "needs-airtest",
            "exploration_backend": exploration_backend,
            "replay_backend": None,
        }
    if mode in {"explore", "full"} and exploration_backend is None:
        cua_status = cua_probe.get("status")
        if cua_status == "missing":
            status = "needs-cua-driver"
        elif cua_usable and cua_mcp == "unknown":
            status = "needs-surface-probe"
        elif cua_usable and cua_mcp == "unavailable":
            status = "needs-cua-mcp"
        else:
            status = "cua-unavailable"
        return {
            "status": status,
            "exploration_backend": None,
            "replay_backend": replay_backend,
        }
    if (
        mode in {"explore", "full"}
        and exploration_backend == "codex-native-computer-use"
    ):
        status = "ready-native-compatibility"
    else:
        status = "ready"
    return {
        "status": status,
        "exploration_backend": exploration_backend,
        "replay_backend": replay_backend,
    }
