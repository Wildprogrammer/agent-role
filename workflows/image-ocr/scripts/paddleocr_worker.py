from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping


os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"

with contextlib.redirect_stdout(sys.stderr):
    from paddleocr import PaddleOCR


MINIMUM_SCORE = 0.70


def _read_request(path: Path) -> Mapping[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    required = ("image", "det_model", "rec_model", "language")
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
        raise ValueError("request is missing a required string field")
    if payload.get("orientation_model") is not None and not isinstance(payload["orientation_model"], str):
        raise ValueError("orientation_model must be a string or null")
    return payload


def _run(request: Mapping[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {
        "text_detection_model_dir": request["det_model"],
        "text_recognition_model_dir": request["rec_model"],
        "use_doc_orientation_classify": request.get("orientation_model") is not None,
        "use_doc_unwarping": False,
        "use_textline_orientation": False,
        "lang": request["language"],
    }
    if request.get("orientation_model") is not None:
        options["doc_orientation_classify_model_dir"] = request["orientation_model"]
    with contextlib.redirect_stdout(sys.stderr):
        engine = PaddleOCR(**options)
        pipeline_results = engine.predict(request["image"])
        texts: list[str] = []
        omitted_uncertain_text = False
        for result in pipeline_results:
            recognised = result.get("rec_texts", [])
            scores = result.get("rec_scores", [])
            if not isinstance(recognised, list) or not isinstance(scores, list):
                omitted_uncertain_text = True
                continue
            for text, score in zip(recognised, scores):
                if not isinstance(text, str) or not isinstance(score, (int, float)):
                    omitted_uncertain_text = True
                    continue
                if score < MINIMUM_SCORE:
                    omitted_uncertain_text = True
                    continue
                if text.strip():
                    texts.append(text.strip())
    rendered = "\n".join(texts)
    payload: dict[str, Any] = {
        "status": "success",
        "text": rendered,
        "reliable": bool(rendered),
    }
    if omitted_uncertain_text:
        payload["notice"] = "部分文字置信度不足"
    return payload


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print(json.dumps({"status": "processing-failed", "message": "expected one request path"}, ensure_ascii=False))
        return 2
    try:
        result = _run(_read_request(Path(arguments[0])))
    except Exception as error:
        result = {"status": "processing-failed", "message": str(error) or type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
