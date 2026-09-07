from pathlib import Path
import os
import subprocess
import sys

_HUB_ROOT = Path(__file__).resolve().parents[3]
_HUB_SRC = _HUB_ROOT / "src"
_RUNTIME = _HUB_ROOT / "workspace" / "workflows" / "desktop-client-automation" / "runtime"
_RUNTIME_PYTHON = _RUNTIME / "Scripts" / "python.exe"
if _RUNTIME_PYTHON.is_file() and os.path.normcase(str(Path(sys.executable).resolve())) != os.path.normcase(
    str(_RUNTIME_PYTHON.resolve())
):
    raise SystemExit(
        subprocess.run(
            [str(_RUNTIME_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]],
            check=False,
        ).returncode
    )
if _HUB_SRC.is_dir() and str(_HUB_SRC) not in sys.path:
    sys.path.insert(0, str(_HUB_SRC))

from agent_workflow_hub.desktop_client_automation.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
