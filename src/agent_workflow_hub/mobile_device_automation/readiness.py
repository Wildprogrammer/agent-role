from __future__ import annotations

import re

from .agent_device_runtime import AgentDeviceProbe, AndroidDevice


SUPPORTED_HOSTS = {"Windows", "Darwin", "Linux"}
MODES = {"explore", "image", "full"}


def _node_ready(version: str | None) -> bool:
    if not isinstance(version, str):
        return False
    match = re.fullmatch(r"v?([0-9]+)\.([0-9]+)(?:\.[0-9]+)?", version.strip())
    if not match:
        return False
    return (int(match.group(1)), int(match.group(2))) >= (22, 12)


def evaluate_readiness(
    *,
    os_name: str,
    mode: str,
    node_version: str | None,
    agent_probe: AgentDeviceProbe,
    airtest_ready: bool,
    devices: tuple[AndroidDevice, ...],
) -> dict[str, object]:
    if mode not in MODES:
        raise ValueError("mode must be explore, image, or full")

    status = "ready"
    semantic_required = mode in {"explore", "full"}
    image_required = mode in {"image", "full"}
    if os_name not in SUPPORTED_HOSTS:
        status = "unsupported_host"
    elif semantic_required and not _node_ready(node_version):
        status = "needs_node_22_12"
    elif semantic_required and agent_probe.status == "wrong-version":
        status = "wrong_agent_device_version"
    elif semantic_required and agent_probe.status != "ready":
        status = "needs_agent_device"
    elif not devices:
        status = "device_not_found"
    elif len(devices) > 1:
        status = "ambiguous_device"
    elif image_required and not airtest_ready:
        status = "needs_airtest"

    return {
        "status": status,
        "mode": mode,
        "host": os_name,
        "verified_host": False,
        "device_count": len(devices),
        "semantic_ready": agent_probe.status == "ready" and _node_ready(node_version),
        "image_ready": bool(airtest_ready),
    }
