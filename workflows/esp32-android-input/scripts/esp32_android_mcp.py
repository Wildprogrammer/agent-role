"""Serve the upstream Android MCP tools with the workflow's input route checks."""

from __future__ import annotations

import argparse
import asyncio
import os

from android_agent.mcp import BridgeError, WorkbenchBridge, create_server, local_url
from mcp.server.stdio import stdio_server


class GuardedWorkbenchBridge(WorkbenchBridge):
    def __init__(self, url: str, token: str, *, mode: str, **kwargs):
        super().__init__(url, token, **kwargs)
        self.mode = mode

    def _check_device(self, state: dict) -> None:
        if not (
            state.get("selected") is True
            and state.get("connected") is True
            and state.get("maintenance") is False
        ):
            raise BridgeError("device_not_ready", "Select an online phone and finish maintenance.")
        if state.get("task_state") not in {"idle", "completed", "failed", "stopped", "paused"}:
            raise BridgeError("workbench_busy", "Inspect android_status and pause the workbench task before taking over.")

    def _check_mode(self, state: dict) -> None:
        mode = state.get("mode")
        accepted_modes = {"ble", "usb"} if self.mode == "hid" else {self.mode}
        if mode not in accepted_modes:
            raise BridgeError("input_mode_mismatch", "Select the configured control mode in the workbench and observe again.")
        if mode in {"ble", "usb"} and state.get("hid_ready") is not True:
            raise BridgeError("hid_not_ready", "Reconnect the selected HID controller and observe again.")

    async def invoke(self, name: str, arguments: dict) -> dict:
        if name in {"android_screenshot", "android_action"}:
            state = await super().invoke("android_status", {})
            self._check_device(state)
            self._check_mode(state)
            if name == "android_action":
                action = arguments.get("action")
                if isinstance(action, dict):
                    if action.get("action") not in state.get("capabilities", ()):
                        raise BridgeError("action_not_available", "Use an action advertised by android_status.")
                    if action.get("action") == "key" and action.get("key") not in state.get("allowed_keys", ()):
                        raise BridgeError("key_not_available", "Use a key advertised by android_status.")
        # Upstream binds the observation to the selected device and control epoch.
        return await super().invoke(name, arguments)


async def serve(url: str, mode: str) -> None:
    bridge = GuardedWorkbenchBridge(url, os.getenv("ANDROID_AGENT_ACCESS_TOKEN", ""), mode=mode)
    server = create_server(bridge)
    server.instructions = (
        f"Configured input mode: {mode}. The workflow checks phone and task readiness before screenshots "
        "and validates the route before input. A failed screenshot can stop workbench activity. "
        + server.instructions
    )
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8890", help="Loopback workbench URL.")
    parser.add_argument("--mode", choices=("hid", "ble", "usb", "adb"), default="hid",
                        help="Accepted controller. hid allows BLE and USB; adb must be explicit.")
    args = parser.parse_args()
    try:
        url = local_url(args.url)
    except ValueError:
        parser.error("Use a loopback HTTP workbench URL without credentials or extra path.")
    try:
        asyncio.run(serve(url, args.mode))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
