from __future__ import annotations

import pytest

from agent_workflow_hub.mobile_device_automation.agent_device_runtime import (
    AgentDeviceProbe,
    AndroidDevice,
)
from agent_workflow_hub.mobile_device_automation.readiness import evaluate_readiness


def _probe(status: str = "ready") -> AgentDeviceProbe:
    return AgentDeviceProbe(status=status, version="0.21.19" if status == "ready" else None)


def _device(serial: str = "emulator-5554") -> AndroidDevice:
    return AndroidDevice(serial=serial, name="Pixel", kind="emulator", claimed_by=None)


@pytest.mark.parametrize("os_name", ["Windows", "Darwin", "Linux"])
def test_supported_hosts_can_be_structurally_ready(os_name: str) -> None:
    result = evaluate_readiness(
        os_name=os_name,
        mode="full",
        node_version="22.12.0",
        agent_probe=_probe(),
        airtest_ready=True,
        devices=(_device(),),
    )
    assert result["status"] == "ready"
    assert result["verified_host"] is False


def test_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="mode"):
        evaluate_readiness(
            os_name="Windows",
            mode="everything",
            node_version="22.12.0",
            agent_probe=_probe(),
            airtest_ready=True,
            devices=(_device(),),
        )


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"os_name": "FreeBSD"}, "unsupported_host"),
        ({"node_version": None}, "needs_node_22_12"),
        ({"node_version": "22.11.9"}, "needs_node_22_12"),
        ({"agent_probe": _probe("missing")}, "needs_agent_device"),
        ({"agent_probe": _probe("wrong-version")}, "wrong_agent_device_version"),
        ({"devices": ()}, "device_not_found"),
        ({"devices": (_device("one"), _device("two"))}, "ambiguous_device"),
        ({"airtest_ready": False}, "needs_airtest"),
    ],
)
def test_full_readiness_returns_stable_status(changes, expected: str) -> None:
    values = {
        "os_name": "Windows",
        "mode": "full",
        "node_version": "22.12.0",
        "agent_probe": _probe(),
        "airtest_ready": True,
        "devices": (_device(),),
    }
    values.update(changes)
    assert evaluate_readiness(**values)["status"] == expected


def test_image_mode_does_not_require_node_or_agent_device() -> None:
    result = evaluate_readiness(
        os_name="Linux",
        mode="image",
        node_version=None,
        agent_probe=_probe("missing"),
        airtest_ready=True,
        devices=(_device(),),
    )
    assert result["status"] == "ready"


def test_explore_mode_does_not_require_airtest() -> None:
    result = evaluate_readiness(
        os_name="Windows",
        mode="explore",
        node_version="23.0.0",
        agent_probe=_probe(),
        airtest_ready=False,
        devices=(_device(),),
    )
    assert result["status"] == "ready"
