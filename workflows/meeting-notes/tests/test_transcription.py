import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "transcription.py"
SPEC = importlib.util.spec_from_file_location("meeting_notes_transcription", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
normalize_command = MODULE.normalize_command
normalize_funasr_result = MODULE.normalize_funasr_result
normalize_media = MODULE.normalize_media
transcribe_local = MODULE.transcribe_local

WORKER_SCRIPT = Path(__file__).parents[1] / "scripts" / "funasr_worker.py"
WORKER_SPEC = importlib.util.spec_from_file_location("meeting_notes_funasr_worker", WORKER_SCRIPT)
assert WORKER_SPEC and WORKER_SPEC.loader
WORKER = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules[WORKER_SPEC.name] = WORKER
WORKER_SPEC.loader.exec_module(WORKER)


def test_normalize_never_overwrites_source_and_uses_16khz_mono_wav(tmp_path: Path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "audio.wav"

    command = normalize_command(Path("ffmpeg.exe"), source, destination)

    assert command == [
        "ffmpeg.exe",
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


def test_normalize_media_rejects_source_overwrite_and_nonzero_result(tmp_path: Path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")

    with pytest.raises(ValueError, match="overwrite source"):
        normalize_media(Path("ffmpeg.exe"), source, source)

    with pytest.raises(RuntimeError, match="FFmpeg failed"):
        normalize_media(
            Path("ffmpeg.exe"),
            source,
            tmp_path / "audio.wav",
            run=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="bad"),
        )


def test_normalize_funasr_result_drops_speaker_identity():
    segments = normalize_funasr_result(
        [{"text": "开始会议", "timestamp": [[0, 500]], "speaker": "ignored"}]
    )

    assert segments == [{"start_ms": 0, "end_ms": 500, "text": "开始会议"}]


def test_normalize_funasr_result_preserves_worker_ranges_and_timestamp_source():
    segments = normalize_funasr_result(
        [
            {
                "start_ms": 0,
                "end_ms": 15_000,
                "text": "本地转写",
                "timestamp_source": "derived-chunk",
            }
        ]
    )

    assert segments == [
        {
            "start_ms": 0,
            "end_ms": 15_000,
            "text": "本地转写",
            "timestamp_source": "derived-chunk",
        }
    ]


def test_funasr_worker_derives_chunk_ranges_when_provider_has_no_timestamp():
    segments = WORKER._normalize(
        [{"text": "first"}, {"text": "second"}], fallback_duration_ms=15_000
    )

    assert segments == [
        {
            "start_ms": 0,
            "end_ms": 7_500,
            "text": "first",
            "timestamp_source": "derived-chunk",
        },
        {
            "start_ms": 7_500,
            "end_ms": 15_000,
            "text": "second",
            "timestamp_source": "derived-chunk",
        },
    ]


def test_transcribe_local_uses_only_explicit_python_and_worker(tmp_path: Path):
    python = (tmp_path / "python.exe").resolve()
    python.write_text("placeholder", encoding="utf-8")
    worker = (tmp_path / "funasr_worker.py").resolve()
    worker.write_text("placeholder", encoding="utf-8")
    request = (tmp_path / "request.json").resolve()
    result = (tmp_path / "result.json").resolve()
    request.write_text(json.dumps({"result_path": str(result)}), encoding="utf-8")

    seen: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs):
        seen.append(command)
        result.write_text(
            json.dumps([{"text": "本地转写", "timestamp": [[0, 300]]}]),
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0, stderr="")

    actual = transcribe_local(python, worker, request, run=fake_run)

    assert seen == [[str(python), str(worker), "--request", str(request)]]
    assert actual == [{"start_ms": 0, "end_ms": 300, "text": "本地转写"}]
