"""Exercise the installed Xiaozhi plugin through the real private stdio bridge."""

from __future__ import annotations

import asyncio
import json

from plugins_func.functions import hermes_bridge


class VerificationConnection:
    session_id = "xiaozhi-software-verification"
    device_id = "xiaozhi-software-verification"


def _result(response):
    if response.result is None:
        raise RuntimeError(response.response or "plugin returned no result")
    return json.loads(response.result)


async def main() -> None:
    connection = VerificationConnection()
    adapter = hermes_bridge._get_adapter()
    expected_profiles = list(hermes_bridge._PROFILES)
    target_profile = expected_profiles[-1]
    try:
        listed = _result(await hermes_bridge.hermes_list_profiles(connection))
        if listed.get("profiles") != expected_profiles:
            raise RuntimeError(f"unexpected profile list: {listed.get('profiles')!r}")

        switched = _result(
            await hermes_bridge.hermes_switch_profile(connection, target_profile)
        )
        if switched.get("active_profile") != target_profile:
            raise RuntimeError("profile switch did not persist in the bridge session")

        sessions = _result(
            await hermes_bridge.hermes_list_sessions(connection, target_profile, 5)
        )
        if not isinstance(sessions.get("sessions"), list):
            raise RuntimeError("shared Hermes session list is unavailable")

        binding = _result(await hermes_bridge.hermes_show_binding(connection))
        if type(binding.get("bound")) is not bool:
            raise RuntimeError("binding state did not return a boolean")

        print(
            json.dumps(
                {
                    "status": "ok",
                    "profiles": listed["profiles"],
                    "active_profile": switched["active_profile"],
                    "shared_sessions": len(sessions["sessions"]),
                    "bound": binding["bound"],
                },
                ensure_ascii=False,
            )
        )
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
