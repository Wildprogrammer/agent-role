from __future__ import annotations

from pathlib import Path
import sys


_HUB_SRC = Path(__file__).resolve().parents[3] / "src"
if _HUB_SRC.is_dir() and str(_HUB_SRC) not in sys.path:
    sys.path.insert(0, str(_HUB_SRC))

from agent_workflow_hub.knowledge_support_agent.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
