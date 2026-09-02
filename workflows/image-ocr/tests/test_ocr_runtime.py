import json
from pathlib import Path
from subprocess import CompletedProcess
import sys

import pytest


SCRIPTS_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import ocr_assist
import umiocr_worker


def _ready_config(tmp_path: Path) -> ocr_assist.PaddleConfig:
    det = tmp_path / "det"
    rec = tmp_path / "rec"
    det.mkdir()
    rec.mkdir()
    (det / "model.bin").write_bytes(b"det")
    (rec / "model.bin").write_bytes(b"rec")
    manifest = tmp_path / "models.json"
    ocr_assist.write_manifest(manifest, {"det": det, "rec": rec})
    return ocr_assist.load_paddle_config(
        det,
        rec,
        None,
        manifest,
        Path(sys.executable),
    )


def test_existing_absolute_images_preserves_order_and_rejects_unsupported_files(
    tmp_path: Path,
):
    first = tmp_path / "first.png"
    second = tmp_path / "second.jpg"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    assert ocr_assist.existing_absolute_images([str(first), str(second)]) == [
        first,
        second,
    ]

    with pytest.raises(ocr_assist.WorkflowError, match="supported image"):
        ocr_assist.existing_absolute_images([str(tmp_path / "note.pdf")])


def test_models_are_bound_to_an_explicit_hash_manifest(tmp_path: Path):
    config = _ready_config(tmp_path)
    assert config.det_model == tmp_path / "det"

    (tmp_path / "rec" / "model.bin").write_bytes(b"changed")
    with pytest.raises(ocr_assist.WorkflowError, match="manifest digest"):
        ocr_assist.load_paddle_config(
            tmp_path / "det",
            tmp_path / "rec",
            None,
            tmp_path / "models.json",
            Path(sys.executable),
        )


def test_new_output_path_requires_a_new_absolute_txt_or_md_file(tmp_path: Path):
    target = tmp_path / "result.txt"
    assert ocr_assist.new_output_path(str(target)) == target
    target.write_text("old", encoding="utf-8")

    with pytest.raises(FileExistsError):
        ocr_assist.new_output_path(str(target))

    with pytest.raises(ocr_assist.WorkflowError, match=".txt or .md"):
        ocr_assist.new_output_path(str(tmp_path / "result.json"))


def test_paddle_worker_subprocess_sets_offline_environment_and_removes_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    image = tmp_path / "input.png"
    image.write_bytes(b"image")
    config = _ready_config(tmp_path)
    observed: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> CompletedProcess[str]:
        observed["arguments"] = arguments
        observed["environment"] = kwargs["env"]
        observed["request"] = Path(arguments[-1])
        return CompletedProcess(arguments, 0, '{"status":"success","text":"hello","reliable":true}', "")

    monkeypatch.setattr(ocr_assist.subprocess, "run", fake_run)

    assert ocr_assist.run_paddle_worker(image, config, "ch") == {
        "status": "success",
        "text": "hello",
        "reliable": True,
    }
    assert observed["environment"]["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] == "True"
    assert not observed["request"].exists()


def test_umiocr_worker_uses_direct_local_json_engine_and_removes_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    image = tmp_path / "input.png"
    executable = tmp_path / "PaddleOCR-json.exe"
    image.write_bytes(b"image")
    executable.write_bytes(b"engine")
    observed: dict[str, object] = {}

    def fake_run(arguments: list[str], **kwargs: object) -> CompletedProcess[str]:
        observed["arguments"] = arguments
        observed["request"] = Path(arguments[-1])
        return CompletedProcess(
            arguments,
            0,
            '{"status":"success","text":"hello","reliable":true}',
            "",
        )

    monkeypatch.setattr(ocr_assist.subprocess, "run", fake_run)

    config = ocr_assist.load_umiocr_config(executable)
    assert ocr_assist.run_umiocr_worker(image, config) == {
        "status": "success",
        "text": "hello",
        "reliable": True,
    }
    assert observed["arguments"][:2] == [str(sys.executable), str(ocr_assist.UMI_WORKER_PATH)]
    assert not observed["request"].exists()


def test_worker_json_error_is_preferred_over_stderr_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    image = tmp_path / "input.png"
    image.write_bytes(b"image")
    config = _ready_config(tmp_path)

    def fake_run(arguments: list[str], **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            arguments,
            2,
            '{"status":"processing-failed","message":"engine detail"}',
            "startup log",
        )

    monkeypatch.setattr(ocr_assist.subprocess, "run", fake_run)

    assert ocr_assist.run_paddle_worker(image, config, "ch") == {
        "status": "processing-failed",
        "message": "engine detail",
    }


def test_umiocr_is_the_default_engine_but_never_uses_an_implicit_fallback():
    arguments = ocr_assist._parser().parse_args(["doctor"])

    assert arguments.engine == "umiocr"


def test_paddle_run_preserves_order_and_partial_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    responses = iter(
        (
            {"status": "success", "text": "first text", "reliable": True},
            {"status": "processing-failed", "message": "cannot decode"},
        )
    )
    monkeypatch.setattr(ocr_assist, "run_paddle_worker", lambda *_args: next(responses))
    monkeypatch.setattr(
        ocr_assist,
        "run_tesseract",
        lambda *_args: pytest.fail("PaddleOCR must not silently select Tesseract"),
    )

    result = ocr_assist.run_paddle([first, second], _ready_config(tmp_path), "ch")

    assert result["status"] == "partial-success"
    assert [item["input"] for item in result["results"]] == [str(first), str(second)]
    assert result["results"][0]["text"] == "first text"
    assert result["results"][1]["status"] == "processing-failed"


def test_blank_or_low_confidence_text_is_reported_without_inference():
    assert ocr_assist.normalise_worker_result(
        {"status": "success", "text": "", "reliable": False}
    ) == {
        "status": "success",
        "text": "",
        "notice": "无法可靠识别",
    }


def test_partial_low_confidence_preserves_reliable_text_and_marks_the_gap():
    assert ocr_assist.normalise_worker_result(
        {
            "status": "success",
            "text": "reliable text",
            "reliable": True,
            "notice": "部分文字置信度不足",
        }
    ) == {
        "status": "success",
        "text": "reliable text",
        "notice": "部分文字置信度不足",
    }


def test_umiocr_filters_low_score_lines_without_discarding_reliable_text():
    result = umiocr_worker._result_from_response(
        {
            "code": 100,
            "data": [
                {"text": "reliable text", "score": 0.95},
                {"text": "uncertain text", "score": 0.30},
            ],
        }
    )

    assert result == {
        "status": "success",
        "text": "reliable text",
        "reliable": True,
        "notice": "部分文字置信度不足",
    }
