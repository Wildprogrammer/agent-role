from __future__ import annotations

import json
import os
import subprocess


completed = subprocess.run(
    [
        os.environ["XIAOZHI_HERMES_BRIDGE_PYTHON"],
        os.environ["XIAOZHI_HERMES_BRIDGE_SCRIPT"],
        "desktop-list-agents",
        "--config",
        os.environ["XIAOZHI_HERMES_BRIDGE_CONFIG"],
    ],
    check=True,
    capture_output=True,
    text=True,
    encoding="utf-8",
)
value = json.loads(completed.stdout)
deepseek = [
    item
    for item in value["agents"]
    if item.get("host") == "deepseek-harness" and item.get("available") is True
]
if len(deepseek) != 10:
    raise RuntimeError("managed DSH Web did not expose all ten allowed presets")
print(json.dumps({"status": "ok", "deepseek_agents": len(deepseek)}))
