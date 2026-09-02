from __future__ import annotations

import argparse
import json
import os
import tempfile
import wave
from pathlib import Path
from typing import Any


def _absolute_existing_file(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute file")
    return path.resolve(strict=True)


def _absolute_existing_directory(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"{label} must be an existing absolute directory")
    return path.resolve(strict=True)


def _result_path(request: dict[str, Any], request_path: Path) -> Path:
    value = request.get("result_path")
    output_dir = request.get("output_dir")
    if not isinstance(value, str) or not isinstance(output_dir, str):
        raise ValueError("result_path and output_dir are required")
    result = Path(value)
    allowed_root = Path(output_dir)
    if not result.is_absolute() or not allowed_root.is_absolute():
        raise ValueError("result paths must be absolute")
    resolved_root = allowed_root.resolve(strict=True)
    resolved_result = result.resolve(strict=False)
    try:
        resolved_result.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("result_path is outside the approved run directory") from exc
    if result.exists():
        raise FileExistsError("FunASR result already exists")
    if request_path.parent.resolve(strict=True) != resolved_root:
        raise ValueError("request must live in the approved run directory")
    return result


def _timestamp_pair(value: object) -> tuple[int, int] | None:
    if not isinstance(value, list) or not value:
        return None
    pair = value[0] if isinstance(value[0], list) else value
    if (
        not isinstance(pair, list)
        or len(pair) < 2
        or not isinstance(pair[0], (int, float))
        or not isinstance(pair[1], (int, float))
    ):
        return None
    start, end = max(0, int(pair[0])), max(0, int(pair[1]))
    return (start, end) if end > start else None


def _normalize(raw: object, *, fallback_duration_ms: int) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise ValueError("FunASR returned an invalid result")
    if fallback_duration_ms < 1:
        raise ValueError("normalized audio duration must be positive")
    items = [item for item in raw if isinstance(item, dict)]
    timestamp_pairs = [_timestamp_pair(item.get("timestamp")) for item in items]
    derived_count = sum(pair is None for pair in timestamp_pairs)
    derived_index = 0
    segments: list[dict[str, object]] = []
    for item, pair in zip(items, timestamp_pairs):
        if pair is None:
            start = fallback_duration_ms * derived_index // derived_count
            derived_index += 1
            end = fallback_duration_ms * derived_index // derived_count
            timestamp_source = "derived-chunk"
        else:
            start, end = pair
            timestamp_source = "provider"
        text = item.get("text")
        rendered = text.strip() if isinstance(text, str) else ""
        if not rendered:
            seconds = start // 1000
            rendered = f"[听不清 {seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}]"
        segments.append(
            {
                "start_ms": start,
                "end_ms": max(end, start + 1),
                "text": rendered,
                "timestamp_source": timestamp_source,
            }
        )
    return segments


def _normalized_wav_duration_ms(audio: Path) -> int:
    with wave.open(str(audio), "rb") as reader:
        rate = reader.getframerate()
        frames = reader.getnframes()
    if rate <= 0:
        raise ValueError("normalized audio has an invalid sample rate")
    return max(1, round(frames * 1000 / rate))


def _atomic_json(path: Path, value: object) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def execute(request_path: Path) -> None:
    if not request_path.is_absolute() or not request_path.is_file():
        raise ValueError("request must be an existing absolute JSON file")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    audio = _absolute_existing_file(request.get("audio_path"), "audio_path")
    model = _absolute_existing_directory(request.get("model"), "model")
    result = _result_path(request, request_path)
    language = request.get("language")
    if not isinstance(language, str) or not language.strip():
        raise ValueError("language is required")

    from funasr import AutoModel

    kwargs: dict[str, object] = {"model": str(model)}
    for source, destination in (("vad_model", "vad_model"), ("punc_model", "punc_model")):
        value = request.get(source)
        if value not in (None, ""):
            kwargs[destination] = str(_absolute_existing_directory(value, source))
    engine = AutoModel(**kwargs)
    raw = engine.generate(input=str(audio), language=language)
    _atomic_json(result, _normalize(raw, fallback_duration_ms=_normalized_wav_duration_ms(audio)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="funasr-worker")
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        execute(args.request)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"funasr-worker: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
