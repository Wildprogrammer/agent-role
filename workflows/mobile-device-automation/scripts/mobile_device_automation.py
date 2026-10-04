from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def runtime_python(hub_root: Path, os_name: str = os.name) -> Path:
    runtime = hub_root / "workspace" / "workflows" / "mobile-device-automation" / "runtime"
    return runtime / ("Scripts/python.exe" if os_name == "nt" else "bin/python")


def main() -> int:
    script = Path(__file__).resolve()
    hub_root = script.parents[3]
    private_python = runtime_python(hub_root)
    if private_python.is_file() and os.path.normcase(str(Path(sys.executable).resolve())) != os.path.normcase(
        str(private_python.resolve())
    ):
        return subprocess.run(
            [str(private_python), str(script), *sys.argv[1:]],
            shell=False,
            check=False,
        ).returncode
    hub_src = hub_root / "src"
    if hub_src.is_dir() and str(hub_src) not in sys.path:
        sys.path.insert(0, str(hub_src))
    from agent_workflow_hub.mobile_device_automation.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
