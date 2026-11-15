"""Install the optional, attributed dsh-pet videos into the Gateway static directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.request import urlopen


MANIFEST = Path(__file__).resolve().parents[1] / "references" / "lingxi-pet-assets.json"
STATIC = Path(__file__).resolve().parents[3] / "src/agent_workflow_hub/xiaozhi_hermes_bridge/static"
SOURCE = "https://unpkg.com/dsh-pet@0.4.7/assets/webm/"


def install(destination: Path) -> None:
    manifest_data = MANIFEST.read_bytes()
    manifest = json.loads(manifest_data)
    if manifest["version"] != "0.4.7" or manifest["asset_license"] != "CC-BY-NC-SA-4.0":
        raise ValueError("unexpected asset manifest version or license")
    with tempfile.TemporaryDirectory(dir=destination) as directory:
        staging = Path(directory)
        for asset in manifest["assets"]:
            filename, source, size = asset["file"], asset["source"], asset["bytes"]
            if not re.fullmatch(r"lingxi-pet-[a-z]+[.]webm", filename):
                raise ValueError(f"invalid clip filename: {filename}")
            if not source.startswith(SOURCE) or not 0 < size < 2_000_000:
                raise ValueError(f"invalid clip source or size: {filename}")
            with urlopen(source, timeout=30) as response:
                if not response.geturl().startswith(SOURCE):
                    raise ValueError(f"unexpected clip redirect: {filename}")
                data = response.read(size + 1)
            if len(data) != size or hashlib.sha256(data).hexdigest() != asset["sha256"]:
                raise ValueError(f"clip failed length or SHA-256 validation: {filename}")
            (staging / filename).write_bytes(data)
        for clip in staging.iterdir():
            os.replace(clip, destination / clip.name)
        marker = staging / MANIFEST.name
        marker.write_bytes(manifest_data)
        os.replace(marker, destination / marker.name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-dir", type=Path, default=STATIC, help="Gateway static directory")
    args = parser.parse_args()
    install(args.asset_dir.resolve(strict=True))
    print("Installed dsh-pet 0.4.7 clips (CC BY-NC-SA 4.0). Reload the companion or phone page.")


if __name__ == "__main__":
    main()
