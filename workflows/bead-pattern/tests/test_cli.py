import json
from pathlib import Path
import subprocess
import sys

from PIL import Image


SCRIPT = Path(__file__).parents[1] / "scripts" / "bead_pattern.py"


def invoke(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )


def source_png(tmp_path: Path) -> Path:
    source = tmp_path / "source.png"
    Image.new("RGBA", (4, 4), (251, 237, 86, 255)).save(source)
    return source


def error_category(result: subprocess.CompletedProcess[str]) -> str:
    return json.loads(result.stderr)["category"]


def test_plan_accept_render_requires_explicit_acceptance(tmp_path):
    source = source_png(tmp_path)
    plan = invoke(
        "plan",
        "--hub-root",
        str(tmp_path),
        "--input",
        str(source),
        "--run-id",
        "demo-1",
        "--preset",
        "24",
        "--columns",
        "4",
        "--rows",
        "4",
    )

    assert plan.returncode == 0, plan.stderr
    candidate = tmp_path / "workspace" / "workflows" / "bead-pattern" / "runs" / "demo-1" / "candidate.json"
    output = tmp_path / "workflows" / "bead-pattern" / "outputs" / "demo-1" / "pattern.png"
    assert candidate.is_file()
    assert not output.exists()
    assert json.loads(plan.stdout)["total_beads"] == 16

    before_accept = invoke("render", "--hub-root", str(tmp_path), "--run-id", "demo-1")
    assert before_accept.returncode != 0
    assert error_category(before_accept) == "needs-user-confirmation"

    accepted = invoke("accept", "--hub-root", str(tmp_path), "--run-id", "demo-1")
    assert accepted.returncode == 0, accepted.stderr
    assert not output.exists()

    rendered = invoke("render", "--hub-root", str(tmp_path), "--run-id", "demo-1")
    assert rendered.returncode == 0, rendered.stderr
    assert output.is_file()
    assert list(output.parent.iterdir()) == [output]


def test_cli_has_clear_invalid_and_existing_output_errors(tmp_path):
    source = source_png(tmp_path)
    malformed = invoke("plan", "--not-an-option")
    assert malformed.returncode != 0
    assert error_category(malformed) == "invalid-input"

    invalid = invoke(
        "plan",
        "--hub-root",
        str(tmp_path),
        "--input",
        str(source),
        "--run-id",
        "bad-grid",
        "--preset",
        "24",
        "--columns",
        "0",
        "--rows",
        "4",
    )
    assert invalid.returncode != 0
    assert error_category(invalid) == "invalid-input"

    first = invoke(
        "plan",
        "--hub-root",
        str(tmp_path),
        "--input",
        str(source),
        "--run-id",
        "unique-1",
        "--preset",
        "24",
        "--columns",
        "4",
        "--rows",
        "4",
    )
    duplicate = invoke(
        "plan",
        "--hub-root",
        str(tmp_path),
        "--input",
        str(source),
        "--run-id",
        "unique-1",
        "--preset",
        "24",
        "--columns",
        "4",
        "--rows",
        "4",
    )
    assert first.returncode == 0, first.stderr
    assert duplicate.returncode != 0
    assert error_category(duplicate) == "output-exists"
