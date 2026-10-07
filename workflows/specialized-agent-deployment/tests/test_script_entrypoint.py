from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = (
    REPOSITORY_ROOT
    / "workflows"
    / "specialized-agent-deployment"
    / "scripts"
    / "specialized_agent_deployment.py"
)


def test_wrapper_injects_checkout_src_and_delegates_to_shared_cli() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "parents[3]" in source
    assert "CHECKOUT_ROOT / \"src\"" in source
    assert "agent_workflow_hub.specialized_agent_deployment.cli" in source
    assert "subprocess" not in source


def test_wrapper_help_works_outside_checkout(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=tmp_path,
        shell=False,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    assert "preview" in result.stdout
    assert "apply" in result.stdout
    assert "verify" in result.stdout
