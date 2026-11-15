"""Presentation assets and state transitions must survive packaging and UI changes."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]
STATIC = ROOT / "src" / "agent_workflow_hub" / "xiaozhi_hermes_bridge" / "static"


def test_optional_pet_manifest_is_pinned_and_default_package_has_fallback() -> None:
    import re
    import tomllib

    manifest = json.loads((Path(__file__).parents[1] / "references" /
                          "lingxi-pet-assets.json").read_text(encoding="utf-8"))
    assert manifest["asset_license"] == "CC-BY-NC-SA-4.0"
    assert manifest["version"] == "0.4.7"
    assert len({asset["file"] for asset in manifest["assets"]}) == 14
    for asset in manifest["assets"]:
        assert re.fullmatch(r"lingxi-pet-[a-z]+\.webm", asset["file"])
        assert asset["source"].startswith("https://unpkg.com/dsh-pet@0.4.7/assets/webm/")
        assert re.fullmatch(r"[0-9a-f]{64}", asset["sha256"])
        assert 0 < asset["bytes"] < 2_000_000
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["setuptools"]["package-data"]["agent_workflow_hub.xiaozhi_hermes_bridge"]
    for name in ("pet-attribution.html", "pet.js", "pet.css"):
        assert (STATIC / name).is_file()
        assert any(Path("static", name).match(pattern) for pattern in patterns)


def test_pet_frontend_state_lifecycle() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the pet presentation lifecycle tests")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("test_pet_frontend.mjs"))],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
