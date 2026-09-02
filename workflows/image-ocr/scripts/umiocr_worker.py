from __future__ import annotations

import contextlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType
from typing import Any, Mapping


MINIMUM_SCORE = 0.70


def _fail(message: str) -> dict[str, Any]:
    return {"status": "processing-failed", "message": message}


def _result_from_response(response: Mapping[str, Any]) -> dict[str, Any]:
    if response.get("code") != 100:
        detail = response.get("data")
        return _fail(detail if isinstance(detail, str) and detail else "Umi-OCR returned an error")

    records = response.get("data")
    if not isinstance(records, list):
        return _fail("Umi-OCR returned invalid text records")

    text_lines: list[str] = []
    omitted_uncertain_text = False
    for record in records:
        if not isinstance(record, Mapping):
            omitted_uncertain_text = True
            continue
        text = record.get("text")
        score = record.get("score")
        if not isinstance(text, str) or not isinstance(score, (int, float)):
            omitted_uncertain_text = True
            continue
        if score < MINIMUM_SCORE:
            omitted_uncertain_text = True
            continue
        if text.strip():
            text_lines.append(text.strip())

    rendered = "\n".join(text_lines)
    payload: dict[str, Any] = {
        "status": "success",
        "text": rendered,
        "reliable": bool(rendered),
    }
    if omitted_uncertain_text:
        payload["notice"] = "部分文字置信度不足"
    return payload


def _read_request(request_path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("worker request must be UTF-8 JSON") from error
    if not isinstance(payload, Mapping):
        raise ValueError("worker request must be a JSON object")
    return payload


def _load_api_module(executable: Path) -> ModuleType:
    api_path = executable.parent / "PPOCR_api.py"
    if not api_path.is_file():
        raise ValueError("Umi-OCR PaddleOCR-json package is missing PPOCR_api.py")
    spec = importlib.util.spec_from_file_location("image_ocr_umi_api", api_path)
    if spec is None or spec.loader is None:
        raise ValueError("Umi-OCR PaddleOCR-json API could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _open_engine(executable: Path) -> Any:
    api = _load_api_module(executable)
    with contextlib.redirect_stdout(sys.stderr):
        return api.PPOCR_pipe(
            str(executable),
            argument={
                "enable_mkldnn": False,
                "limit_side_len": 960,
                "cpu_threads": 8,
                "cls": False,
            },
        )


def _run_request(engine: Any, image: Path) -> dict[str, Any]:
    response = engine.run(str(image))
    if not isinstance(response, Mapping):
        return _fail("Umi-OCR engine returned an invalid response")
    return _result_from_response(response)


def _run(request: Mapping[str, Any]) -> dict[str, Any]:
    executable_value = request.get("executable")
    executable = Path(executable_value) if isinstance(executable_value, str) else Path()
    if not executable.is_absolute() or not executable.is_file():
        return _fail("Umi-OCR PaddleOCR-json executable must be an existing absolute file")

    engine = _open_engine(executable)
    try:
        if request.get("mode") == "doctor":
            return {
                "status": "ready",
                "engine": "umiocr",
                "local_transport": "stdin-stdout-json",
                "mkldnn": False,
            }
        image_value = request.get("image")
        image = Path(image_value) if isinstance(image_value, str) else Path()
        if not image.is_absolute() or not image.is_file():
            return _fail("Umi-OCR input must be an existing absolute image file")
        return _run_request(engine, image)
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            engine.exit()


def main(argv: list[str] | None = None) -> int:
    arguments = argv if argv is not None else sys.argv[1:]
    try:
        if len(arguments) != 1:
            raise ValueError("exactly one request path is required")
        result = _run(_read_request(Path(arguments[0])))
    except (OSError, RuntimeError, ValueError) as error:
        result = _fail(str(error))
    sys.stdout.buffer.write((json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0 if result["status"] in {"ready", "success"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
