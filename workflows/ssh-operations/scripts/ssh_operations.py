from pathlib import Path
import sys

_HUB_SRC = Path(__file__).resolve().parents[3] / "src"
_RUNTIME = (
    Path(__file__).resolve().parents[3]
    / "workspace"
    / "workflows"
    / "ssh-operations"
    / "runtime"
)
_RUNTIME_SITES = (
    _RUNTIME / "Lib" / "site-packages",
    *sorted(_RUNTIME.glob("lib/python*/site-packages")),
)
for _site in _RUNTIME_SITES:
    if _site.is_dir() and str(_site) not in sys.path:
        sys.path.insert(0, str(_site))
if _HUB_SRC.is_dir() and str(_HUB_SRC) not in sys.path:
    sys.path.insert(0, str(_HUB_SRC))

from agent_workflow_hub.ssh_operations.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
