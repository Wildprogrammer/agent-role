import json
import io
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import ocr_assist


SCRIPT = SCRIPTS_DIRECTORY / "ocr_assist.py"


def _run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _success_payload(text: str) -> dict[str, object]:
    return {
        "status": "success",
        "engine": "paddleocr",
        "results": [{"input": "C:/example.png", "status": "success", "text": text}],
    }


def test_doctor_reports_missing_models_without_writing(tmp_path: Path):
    completed = _run_cli(
        "doctor",
        "--engine",
        "paddleocr",
        "--det-model",
        str(tmp_path / "det"),
        "--rec-model",
        str(tmp_path / "rec"),
        "--manifest",
        str(tmp_path / "models.json"),
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 2
    assert payload["status"] == "needs-dependency"
    assert list(tmp_path.iterdir()) == []


def test_delivery_writes_only_a_new_explicit_txt_or_md_file(tmp_path: Path):
    output = tmp_path / "ocr.md"

    assert ocr_assist.deliver(_success_payload("hello"), None) == "hello"
    assert ocr_assist.deliver(_success_payload("hello"), output) == "hello"
    assert output.read_text(encoding="utf-8") == "hello\n"

    with pytest.raises(FileExistsError):
        ocr_assist.deliver(_success_payload("again"), output)


def test_delivery_marks_an_unreliable_image_without_visual_inference():
    payload = {
        "status": "success",
        "engine": "paddleocr",
        "results": [
            {"input": "C:/example.png", "status": "success", "text": "", "notice": "无法可靠识别"}
        ],
    }

    assert ocr_assist.deliver(payload, None) == "无法可靠识别"


def test_delivery_retains_reliable_text_and_appends_a_partial_confidence_notice():
    payload = {
        "status": "success",
        "engine": "umiocr",
        "results": [
            {
                "input": "C:/example.png",
                "status": "success",
                "text": "reliable text",
                "notice": "部分文字置信度不足",
            }
        ],
    }

    assert ocr_assist.deliver(payload, None) == "reliable text\n\n部分文字置信度不足"


def test_json_output_is_utf8_bytes_independent_of_the_console_code_page(
    monkeypatch: pytest.MonkeyPatch,
):
    class FakeStdout:
        buffer = io.BytesIO()

    fake_stdout = FakeStdout()
    monkeypatch.setattr(ocr_assist.sys, "stdout", fake_stdout)

    ocr_assist._json_print({"text": "部分文字置信度不足\ufffd"})

    assert json.loads(fake_stdout.buffer.getvalue().decode("utf-8")) == {
        "text": "部分文字置信度不足\ufffd"
    }
