from __future__ import annotations

import sys
from pathlib import Path


CHECKOUT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = CHECKOUT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from agent_workflow_hub.specialized_agent_deployment.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
