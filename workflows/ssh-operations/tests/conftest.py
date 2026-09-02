from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
_RUNTIME_SITE = _ROOT / "workspace" / "workflows" / "ssh-operations" / "runtime" / "Lib" / "site-packages"
if _RUNTIME_SITE.is_dir() and str(_RUNTIME_SITE) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_SITE))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
