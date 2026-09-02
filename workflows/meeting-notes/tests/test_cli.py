import json
import subprocess
import sys
import importlib.util
from types import SimpleNamespace
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "meeting_notes.py"
RUNTIME_SCRIPT = Path(__file__).parents[1] / "scripts" / "runtime.py"
RUNTIME_SPEC = importlib.util.spec_from_file_location("meeting_notes_runtime_for_cli", RUNTIME_SCRIPT)
assert RUNTIME_SPEC and RUNTIME_SPEC.loader
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
sys.modules[RUNTIME_SPEC.name] = RUNTIME
RUNTIME_SPEC.loader.exec_module(RUNTIME)
MEETING_SPEC = importlib.util.spec_from_file_location("meeting_notes_cli_module", SCRIPT)
assert MEETING_SPEC and MEETING_SPEC.loader
MEETING = importlib.util.module_from_spec(MEETING_SPEC)
sys.modules[MEETING_SPEC.name] = MEETING
MEETING_SPEC.loader.exec_module(MEETING)


def source_file(tmp_path: Path) -> Path:
    source = tmp_path / "source.wav"
    source.write_bytes(b"meeting-audio")
    return source


def make_voice_model(tmp_path: Path) -> Path:
    model = tmp_path / "model"
    model.mkdir(exist_ok=True)
    weight = model / "weights.bin"
    weight.write_bytes(b"weights")
    manifest = tmp_path / "model.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "revision": "0123456789abcdef",
                "files": [
                    {
                        "path": "weights.bin",
                        "size_bytes": weight.stat().st_size,
                        "sha256": MEETING.sha256_file(weight),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return model


def voice_args(
    tmp_path: Path,
    run_id: str,
    *,
    text: str = "测试",
    timeout_seconds: int | None = None,
) -> SimpleNamespace:
    make_voice_model(tmp_path)
    return SimpleNamespace(
        hub_root=str(tmp_path),
        run_id=run_id,
        text=text,
        vault=None,
        relative=None,
        section="摘要",
        clone=False,
        reference_audio=None,
        clone_consent=None,
        retention=None,
        voxcpm_python=sys.executable,
        model_path=str(tmp_path / "model"),
        model_revision="0123456789abcdef",
        model_manifest=str(tmp_path / "model.manifest.json"),
        timeout_seconds=timeout_seconds,
    )


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def error_category(result: subprocess.CompletedProcess[str]) -> str:
    return json.loads(result.stdout)["status"]


def test_transcribe_requires_local_funasr_and_leaves_run_at_review_gate(tmp_path: Path):
    result = invoke(
        "transcribe",
        "--hub-root",
        str(tmp_path),
        "--input",
        str(source_file(tmp_path)),
        "--run-id",
        "demo-1",
    )

    assert result.returncode != 0
    assert error_category(result) == "needs-dependency"


def test_transcribe_checks_funasr_import_before_creating_a_run(tmp_path: Path):
    model = tmp_path / "funasr-model"
    model.mkdir()
    manifest = tmp_path / "funasr.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    fake_ffmpeg = tmp_path / "ffmpeg.exe"
    fake_ffmpeg.write_text("placeholder", encoding="utf-8")

    result = invoke(
        "transcribe",
        "--hub-root",
        str(tmp_path),
        "--input",
        str(source_file(tmp_path)),
        "--run-id",
        "demo-2",
        "--ffmpeg",
        str(fake_ffmpeg),
        "--funasr-python",
        sys.executable,
        "--funasr-model",
        str(model),
        "--funasr-model-manifest",
        str(manifest),
    )

    assert result.returncode != 0
    assert error_category(result) == "needs-dependency"
    assert not (tmp_path / "workspace/workflows/meeting-notes/runs/demo-2").exists()


def test_transcribe_rejects_nonlocal_auxiliary_model_before_dependency_check(tmp_path: Path):
    result = invoke(
        "transcribe",
        "--hub-root",
        str(tmp_path),
        "--input",
        str(source_file(tmp_path)),
        "--run-id",
        "demo-3",
        "--vad-model",
        "remote-model-id",
    )

    assert result.returncode != 0
    assert error_category(result) == "invalid-input"
    assert not (tmp_path / "workspace/workflows/meeting-notes/runs/demo-3").exists()


def test_voice_doctor_does_not_require_core_transcription_dependencies(tmp_path: Path):
    result = invoke("doctor", "--hub-root", str(tmp_path), "--with-voice")
    payload = json.loads(result.stdout)

    assert result.returncode != 0
    assert payload["status"] == "needs-dependency"
    assert "ffmpeg" not in payload["missing"]
    assert "funasr-python" not in payload["missing"]
    assert "voxcpm-python" in payload["missing"]


def test_voice_doctor_ready_message_names_voice_dependencies(monkeypatch, capsys):
    monkeypatch.setattr(MEETING, "_voice_dependencies", lambda _args, verify_import: ({}, []))

    assert MEETING.command_doctor(SimpleNamespace(with_voice=True)) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert "VoxCPM" in payload["detail"]
    assert "CUDA" in payload["detail"]
    assert "FunASR" not in payload["detail"]


def test_voice_import_check_uses_distribution_metadata(monkeypatch):
    captured: dict[str, list[str]] = {}

    def fake_run(args, **_kwargs):
        captured["args"] = args
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(MEETING.subprocess, "run", fake_run)

    assert MEETING._voice_import_check(Path("C:/tools/voxcpm/python.exe"))
    command = captured["args"][2]
    assert "from importlib.metadata import version" in command
    assert "import torch" in command
    assert "torch.cuda.is_available()" in command
    assert "version('voxcpm')" in command
    assert "voxcpm.__version__" not in command


def test_voice_import_check_uses_fixed_short_timeout(monkeypatch):
    captured: dict[str, object] = {}

    def fake_run(args, **_kwargs):
        captured["args"] = args
        captured["kwargs"] = _kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(MEETING.subprocess, "run", fake_run)

    assert MEETING._voice_import_check(Path("C:/tools/voxcpm/python.exe"))
    assert captured["kwargs"]["timeout"] == 30


def test_speak_worker_timeout_reports_processing_failed_and_cleans_output(
    tmp_path: Path, monkeypatch, capsys
):
    args = voice_args(tmp_path, "voice-timeout", text="测试" * 100)
    monkeypatch.setattr(MEETING, "_voice_import_check", lambda _python: True)

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=_args[0], timeout=_kwargs["timeout"])

    monkeypatch.setattr(MEETING.subprocess, "run", fake_run)

    try:
        result = MEETING.command_speak(args)
    except subprocess.TimeoutExpired:
        pytest.fail("command_speak must convert a worker timeout into processing-failed")
    assert result == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "processing-failed"
    assert "timed out" in payload["detail"].casefold()
    assert not (tmp_path / "workflows/meeting-notes/outputs/voice-timeout").exists()


def test_speak_passes_timeout_seconds_to_worker(tmp_path: Path, monkeypatch, capsys):
    args = voice_args(tmp_path, "voice-timeout-flag", timeout_seconds=321)
    captured: dict[str, object] = {}
    monkeypatch.setattr(MEETING, "_voice_import_check", lambda _python: True)

    def fake_run(command, **_kwargs):
        captured["timeout"] = _kwargs["timeout"]
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        Path(request["output_path"]).write_bytes(b"fake-wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(MEETING.subprocess, "run", fake_run)

    assert MEETING.command_speak(args) == 0
    assert captured["timeout"] == 321


def test_speak_default_timeout_scales_with_text_length(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(MEETING, "_voice_import_check", lambda _python: True)
    captured: list[int] = []

    def fake_run(command, **_kwargs):
        captured.append(_kwargs["timeout"])
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        Path(request["output_path"]).write_bytes(b"fake-wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(MEETING.subprocess, "run", fake_run)

    assert MEETING.command_speak(voice_args(tmp_path, "voice-short")) == 0
    assert MEETING.command_speak(voice_args(tmp_path, "voice-long", text="测试" * 600)) == 0
    assert captured == [120, 360]


def test_speak_rejects_invalid_timeout_seconds(tmp_path: Path, monkeypatch, capsys):
    args = voice_args(tmp_path, "voice-invalid-timeout", timeout_seconds=0)
    monkeypatch.setattr(MEETING, "_voice_import_check", lambda _python: True)
    monkeypatch.setattr(
        MEETING.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=""),
    )

    assert MEETING.command_speak(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "invalid-input"
    assert not (tmp_path / "workflows/meeting-notes/outputs/voice-invalid-timeout").exists()


def test_clone_compatibility_options_are_accepted_but_not_serialized(
    tmp_path: Path, monkeypatch, capsys
):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    legacy_consent = tmp_path / "clone-consent.json"
    legacy_consent.write_text("{}", encoding="utf-8")
    model = tmp_path / "model"
    model.mkdir()
    manifest = tmp_path / "model.manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    args = MEETING.build_parser().parse_args(
        [
            "speak",
            "--hub-root",
            str(tmp_path),
            "--run-id",
            "voice-compat",
            "--text",
            "测试",
            "--clone",
            "--reference-audio",
            str(reference),
            "--clone-consent",
            "legacy-inline-value",
            "--clone-consent-file",
            str(legacy_consent),
            "--retention",
            "legacy-retention",
            "--voxcpm-python",
            sys.executable,
            "--model-path",
            str(model),
            "--model-revision",
            "0123456789abcdef",
            "--model-manifest",
            str(manifest),
        ]
    )
    dependencies = {
        "voxcpm_python": Path(sys.executable),
        "model_path": model,
        "model_manifest": manifest,
    }
    monkeypatch.setattr(MEETING, "_voice_dependencies", lambda *_args, **_kwargs: (dependencies, []))

    def fake_worker(command, **_kwargs):
        request = json.loads(Path(command[-1]).read_text(encoding="utf-8"))
        Path(request["output_path"]).write_bytes(b"fake-wav")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(MEETING.subprocess, "run", fake_worker)

    assert MEETING.command_speak(args) == 0
    request = json.loads(
        (tmp_path / "workflows/meeting-notes/outputs/voice-compat/voice-request.json").read_text(
            encoding="utf-8"
        )
    )

    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    assert request["reference_audio_sha256"] == MEETING.sha256_file(reference)
    assert "clone_consent" not in request
    assert "retention" not in request


def test_model_manifest_binds_existing_files_and_revision(tmp_path: Path):
    model = tmp_path / "model"
    model.mkdir()
    weight = model / "weights.bin"
    weight.write_bytes(b"weights")
    manifest = tmp_path / "model.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "revision": "0123456789abcdef",
                "files": [
                    {
                        "path": "weights.bin",
                        "size_bytes": weight.stat().st_size,
                        "sha256": "9a129038d9a00aed0cf6a7ea059ca50a813449061ab87848cf1a13eafdf33b2c",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manifest_hash = MEETING.validate_model_manifest(
        model, manifest, expected_revision="0123456789abcdef"
    )

    assert manifest_hash == MEETING.sha256_file(manifest)
    with pytest.raises(ValueError, match="revision"):
        MEETING.validate_model_manifest(model, manifest, expected_revision="different")
    weight.unlink()
    with pytest.raises(ValueError, match="missing"):
        MEETING.validate_model_manifest(model, manifest, expected_revision="0123456789abcdef")


def test_speak_worker_failure_cleans_the_new_output(tmp_path: Path, monkeypatch, capsys):
    model = tmp_path / "model"
    model.mkdir()
    weight = model / "weights.bin"
    weight.write_bytes(b"weights")
    manifest = tmp_path / "model.manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "revision": "0123456789abcdef",
                "files": [
                    {
                        "path": "weights.bin",
                        "size_bytes": weight.stat().st_size,
                        "sha256": MEETING.sha256_file(weight),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        hub_root=str(tmp_path),
        run_id="voice-failure",
        text="测试",
        vault=None,
        relative=None,
        section="摘要",
        clone=False,
        reference_audio=None,
        clone_consent=None,
        retention=None,
        voxcpm_python=sys.executable,
        model_path=str(model),
        model_revision="0123456789abcdef",
        model_manifest=str(manifest),
    )
    monkeypatch.setattr(MEETING, "_voice_import_check", lambda _python: True)
    monkeypatch.setattr(
        MEETING.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="synthetic failure"),
    )

    assert MEETING.command_speak(args) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "needs-dependency"
    assert not (tmp_path / "workflows/meeting-notes/outputs/voice-failure").exists()


def test_summary_commands_require_explicit_gates(tmp_path: Path):
    run = RUNTIME.create_run(tmp_path, "demo-1", source=source_file(tmp_path))
    RUNTIME.save_transcript(run, [{"start_ms": 0, "end_ms": 100, "text": "发布"}])
    reviewed = tmp_path / "reviewed.md"
    reviewed.write_text("发布", encoding="utf-8")
    summary = tmp_path / "summary.md"
    summary.write_text("# 摘要\n发布", encoding="utf-8")

    accepted = invoke(
        "accept-transcript", "--hub-root", str(tmp_path), "--run-id", "demo-1", "--reviewed-transcript", str(reviewed)
    )
    assert accepted.returncode == 0
    assert error_category(accepted) == "ready-for-summary"

    staged = invoke("summarize", "--hub-root", str(tmp_path), "--run-id", "demo-1", "--summary-file", str(summary))
    assert staged.returncode == 0
    assert error_category(staged) == "needs-user-confirmation"


def test_speak_voice_dependency_failure_does_not_create_output(tmp_path: Path):
    result = invoke("speak", "--hub-root", str(tmp_path), "--text", "你好", "--run-id", "voice-1")

    assert result.returncode != 0
    assert error_category(result) == "needs-dependency"
    assert not (tmp_path / "workflows/meeting-notes/outputs/voice-1").exists()
