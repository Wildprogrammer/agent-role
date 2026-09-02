from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable


def normalize_command(ffmpeg: Path, source: Path, destination: Path) -> list[str]:
    """Build a non-interactive command that never replaces the source file."""

    return [
        str(ffmpeg),
        "-nostdin",
        "-n",
        "-i",
        str(source),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]


def normalize_media(
    ffmpeg: Path,
    source: Path,
    destination: Path,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> None:
    """Normalize one approved source into a new 16 kHz mono PCM WAV file."""

    if source.absolute() == destination.absolute():
        raise ValueError("refusing to overwrite source media")
    if not source.is_file():
        raise ValueError(f"source media does not exist: {source}")
    if destination.exists():
        raise FileExistsError(f"normalized destination already exists: {destination}")

    completed = run(
        normalize_command(ffmpeg, source, destination),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = getattr(completed, "stderr", "").strip()
        raise RuntimeError(f"FFmpeg failed: {detail or completed.returncode}")
    if not destination.is_file():
        raise RuntimeError("FFmpeg reported success but did not create normalized audio")


def _timestamp_pair(value: object) -> tuple[int, int]:
    if not isinstance(value, list) or not value:
        return (0, 0)
    pair = value[0] if isinstance(value[0], list) else value
    if (
        not isinstance(pair, list)
        or len(pair) < 2
        or not isinstance(pair[0], (int, float))
        or not isinstance(pair[1], (int, float))
    ):
        return (0, 0)
    start, end = int(pair[0]), int(pair[1])
    return (max(0, start), max(0, end))


def _segment_range(item: dict[str, object]) -> tuple[int, int]:
    start = item.get("start_ms")
    end = item.get("end_ms")
    if isinstance(start, int) and isinstance(end, int) and start >= 0 and end > start:
        return start, end
    return _timestamp_pair(item.get("timestamp"))


def _unclear_marker(start_ms: int) -> str:
    seconds = start_ms // 1000
    return f"[听不清 {seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}]"


def normalize_funasr_result(raw: list[dict[str, object]]) -> list[dict[str, object]]:
    """Map provider output to the no-speaker-identity transcript contract."""

    normalized: list[dict[str, object]] = []
    for item in raw:
        start, end = _segment_range(item)
        text = item.get("text")
        rendered = text.strip() if isinstance(text, str) else ""
        if not rendered:
            rendered = _unclear_marker(start)
        if end <= start:
            end = start + 1
        segment: dict[str, object] = {"start_ms": start, "end_ms": end, "text": rendered}
        source = item.get("timestamp_source")
        if source in {"provider", "derived-chunk"}:
            segment["timestamp_source"] = source
        normalized.append(segment)
    return normalized


def transcribe_local(
    funasr_python: Path,
    worker: Path,
    request_path: Path,
    *,
    run: Callable[..., Any] = subprocess.run,
) -> list[dict[str, object]]:
    """Invoke only the explicit user-managed FunASR environment and worker."""

    for label, path in (
        ("FunASR Python", funasr_python),
        ("FunASR worker", worker),
        ("transcription request", request_path),
    ):
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f"{label} must be an existing absolute file: {path}")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict) or not isinstance(request.get("result_path"), str):
        raise ValueError("transcription request is missing result_path")
    result_path = Path(request["result_path"])
    if not result_path.is_absolute():
        raise ValueError("transcription result_path must be absolute")

    completed = run(
        [str(funasr_python), str(worker), "--request", str(request_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = getattr(completed, "stderr", "").strip()
        raise RuntimeError(f"FunASR worker failed: {detail or completed.returncode}")
    try:
        raw = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError("FunASR worker did not create a result") from exc
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise RuntimeError("FunASR worker result must be a list of objects")
    return normalize_funasr_result(raw)
