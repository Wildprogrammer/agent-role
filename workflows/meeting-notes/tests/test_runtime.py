import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "runtime.py"
SPEC = importlib.util.spec_from_file_location("meeting_notes_runtime", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
GateError = MODULE.GateError
approve_summary = MODULE.approve_summary
accept_transcript = MODULE.accept_transcript
create_run = MODULE.create_run
load_run = MODULE.load_run
cleanup_voice_output = MODULE.cleanup_voice_output
prepare_voice_output = MODULE.prepare_voice_output
record_transcription_config = MODULE.record_transcription_config
save_transcript = MODULE.save_transcript
stage_summary = MODULE.stage_summary
stage_summary_file = MODULE.stage_summary_file
write_approved_summary = MODULE.write_approved_summary


def source_file(tmp_path: Path) -> Path:
    source = tmp_path / "source.wav"
    source.write_bytes(b"meeting-audio")
    return source


def manifest(run) -> dict:
    return json.loads(run.manifest_path.read_text(encoding="utf-8"))


def test_run_requires_transcript_review_before_summary(tmp_path: Path):
    source = source_file(tmp_path)
    run = create_run(tmp_path, "demo-1", source=source)

    assert manifest(run)["state"] == "created"
    assert manifest(run)["source"]["sha256"]
    assert manifest(run)["source"]["absolute_path"] == str(source.resolve())
    record_transcription_config(
        run,
        ffmpeg_path="C:/tools/ffmpeg.exe",
        funasr_python="C:/envs/funasr/python.exe",
        model_path="D:/models/funasr",
        model_manifest_path="D:/models/funasr.manifest.json",
        model_manifest_sha256="a" * 64,
        vad_model_path=None,
        punc_model_path=None,
        language="zh",
    )
    assert manifest(run)["transcription"]["model_manifest_sha256"] == "a" * 64

    save_transcript(run, [{"start_ms": 0, "end_ms": 900, "text": "结论是下周发布。"}])
    assert manifest(run)["state"] == "needs-transcript-review"

    with pytest.raises(GateError, match="transcript review"):
        stage_summary(run, "# 摘要\n下周发布。")

    reviewed = accept_transcript(run, reviewed_text="结论是下周发布。")
    assert reviewed.read_text(encoding="utf-8") == "结论是下周发布。\n"
    assert manifest(run)["state"] == "transcript-reviewed"

    draft = stage_summary(run, "# 摘要\n下周发布。")
    assert draft.name == "summary-draft.md"
    assert manifest(run)["state"] == "needs-summary-confirmation"

    approve_summary(run)
    assert manifest(run)["state"] == "summary-approved"
    assert (tmp_path / "workflows/meeting-notes/outputs/demo-1/meeting-notes.md").read_text(
        encoding="utf-8"
    ) == "# 摘要\n下周发布。\n"


def test_save_transcript_keeps_derived_timestamp_evidence(tmp_path: Path):
    run = create_run(tmp_path, "timestamp-evidence", source=source_file(tmp_path))

    transcript = save_transcript(
        run,
        [
            {
                "start_ms": 0,
                "end_ms": 15_000,
                "text": "本地转写",
                "timestamp_source": "derived-chunk",
            }
        ],
    )

    stored = json.loads(transcript.read_text(encoding="utf-8"))
    assert stored["segments"][0]["timestamp_source"] == "derived-chunk"


def test_run_refuses_reused_id_and_loads_existing_manifest(tmp_path: Path):
    source = source_file(tmp_path)
    run = create_run(tmp_path, "demo-1", source=source)

    with pytest.raises(FileExistsError):
        create_run(tmp_path, "demo-1", source=source)
    with pytest.raises(ValueError, match="invalid run id"):
        create_run(tmp_path, "../escape", source=source)

    loaded = load_run(tmp_path, "demo-1")
    assert loaded == run


def test_voice_output_uses_the_same_safe_hub_path_guards(tmp_path: Path):
    root, output_dir = prepare_voice_output(tmp_path, "voice-1")

    assert output_dir == tmp_path / "workflows/meeting-notes/outputs/voice-1"
    assert output_dir.is_dir()
    with pytest.raises(FileExistsError):
        prepare_voice_output(tmp_path, "voice-1")

    cleanup_voice_output(root, "voice-1")
    assert not output_dir.exists()


def test_summary_file_requires_reviewed_transcript_and_explicit_approval(tmp_path: Path):
    run = create_run(tmp_path, "demo-1", source=source_file(tmp_path))
    save_transcript(run, [{"start_ms": 0, "end_ms": 900, "text": "结论是下周发布。"}])
    accept_transcript(run, reviewed_text="结论是下周发布。")
    summary = tmp_path / "summary.md"
    summary.write_text("# 摘要\n下周发布。", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()

    stage_summary_file(run, summary)
    with pytest.raises(GateError, match="summary approval"):
        write_approved_summary(run, vault=vault, relative="Meetings/demo.md")

    approve_summary(run)
    target = write_approved_summary(run, vault=vault, relative="Meetings/demo.md")

    assert target.read_text(encoding="utf-8") == "# 摘要\n下周发布。\n"
    assert manifest(run)["state"] == "complete"
    assert manifest(run)["vault_delivery"]["sha256"]
