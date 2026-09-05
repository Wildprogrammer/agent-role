from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
import re


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from runtime import (
    GateError,
    accept_transcript,
    approve_summary,
    cleanup_voice_output,
    create_run,
    load_run,
    prepare_voice_output,
    record_transcription_config,
    save_transcript,
    stage_summary_file,
    write_approved_summary,
)
from transcription import normalize_media, transcribe_local
from voice import (
    VoiceGateError,
    create_voice_request,
    direct_speech_input,
    select_speech_input,
    sha256_file,
)
from voxcpm_worker import MAX_CHUNK_CHARS


_SHA256 = re.compile(r"[0-9a-f]{64}\Z", re.IGNORECASE)


def _emit(status: str, *, detail: str, **extra: object) -> None:
    print(json.dumps({"status": status, "detail": detail, **extra}, ensure_ascii=False))


def _command_path(value: str | None, label: str) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_file():
        return candidate.resolve(strict=True)
    resolved = shutil.which(value)
    if resolved:
        return Path(resolved).resolve(strict=True)
    return None


def _existing_path(value: str | None, label: str, *, directory: bool = False) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    if directory and path.is_dir():
        return path.resolve(strict=True)
    if not directory and path.is_file():
        return path.resolve(strict=True)
    return None


def validate_model_manifest(
    model_path: Path,
    manifest_path: Path,
    *,
    expected_revision: str | None = None,
) -> str:
    """Verify a user-managed model cache manifest without downloading anything."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("model manifest must be readable JSON") from exc
    if not isinstance(manifest, dict):
        raise ValueError("model manifest must be a JSON object")
    revision = manifest.get("revision")
    if not isinstance(revision, str) or not revision.strip():
        raise ValueError("model manifest requires a nonempty revision")
    if expected_revision is not None and revision != expected_revision:
        raise ValueError("model manifest revision does not match requested revision")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("model manifest requires a nonempty files list")
    root = model_path.resolve(strict=True)
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("model manifest files entries must be objects")
        relative = entry.get("path")
        size_bytes = entry.get("size_bytes")
        digest = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(size_bytes, int)
            or size_bytes < 0
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise ValueError("model manifest contains an invalid file entry")
        try:
            candidate = (root / relative).resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError("model manifest file is missing from local cache") from exc
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError("model manifest file is outside the model directory") from exc
        if not candidate.is_file() or candidate.stat().st_size != size_bytes:
            raise ValueError("model manifest file size does not match local cache")
        if sha256_file(candidate).casefold() != digest.casefold():
            raise ValueError("model manifest file hash does not match local cache")
    return sha256_file(manifest_path)


def _import_check(python: Path, package: str) -> bool:
    try:
        completed = subprocess.run(
            [str(python), "-c", f"import {package}; print({package}.__version__)"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return completed.returncode == 0


def _voice_import_check(python: Path) -> bool:
    try:
        completed = subprocess.run(
            [
                str(python),
                "-c",
                "import sys; from importlib.metadata import version; import torch; import voxcpm; assert (3, 10) <= sys.version_info[:2] < (3, 13); assert torch.cuda.is_available(); print(version('voxcpm'), torch.__version__)",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _voice_timeout_seconds(value: object | None, text_length: int) -> int:
    if value is None:
        num_chunks = max(1, math.ceil(text_length / MAX_CHUNK_CHARS))
        return max(900, 60 + 30 * num_chunks)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("--timeout-seconds must be a positive integer")
    try:
        timeout = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("--timeout-seconds must be a positive integer") from exc
    if timeout <= 0:
        raise ValueError("--timeout-seconds must be a positive integer")
    return timeout


def _core_dependencies(args: argparse.Namespace, *, verify_import: bool) -> tuple[dict[str, Path], list[str]]:
    values: dict[str, Path] = {}
    errors: list[str] = []
    ffmpeg = _command_path(getattr(args, "ffmpeg", None), "ffmpeg")
    if ffmpeg is None:
        errors.append("ffmpeg")
    else:
        values["ffmpeg"] = ffmpeg
    funasr_python = _existing_path(getattr(args, "funasr_python", None), "funasr_python")
    if funasr_python is None:
        errors.append("funasr-python")
    else:
        values["funasr_python"] = funasr_python
        if verify_import and not _import_check(funasr_python, "funasr"):
            errors.append("funasr-package")
    model = _existing_path(getattr(args, "funasr_model", None), "funasr_model", directory=True)
    if model is None:
        errors.append("funasr-model")
    else:
        values["funasr_model"] = model
    manifest = _existing_path(getattr(args, "funasr_model_manifest", None), "funasr_model_manifest")
    if manifest is None:
        errors.append("funasr-model-manifest")
    else:
        values["funasr_model_manifest"] = manifest
    if model is not None and manifest is not None:
        try:
            validate_model_manifest(model, manifest)
        except ValueError:
            errors.append("funasr-model-manifest-invalid")
    return values, errors


def _voice_dependencies(args: argparse.Namespace, *, verify_import: bool) -> tuple[dict[str, Path], list[str]]:
    values: dict[str, Path] = {}
    errors: list[str] = []
    python = _existing_path(getattr(args, "voxcpm_python", None), "voxcpm_python")
    if python is None:
        errors.append("voxcpm-python")
    else:
        values["voxcpm_python"] = python
        if verify_import and not _voice_import_check(python):
            errors.append("voxcpm-package-python-or-cuda")
    model = _existing_path(getattr(args, "model_path", None), "model_path", directory=True)
    if model is None:
        errors.append("voxcpm-model")
    else:
        values["model_path"] = model
    manifest = _existing_path(getattr(args, "model_manifest", None), "model_manifest")
    if manifest is None:
        errors.append("voxcpm-model-manifest")
    else:
        values["model_manifest"] = manifest
    revision = getattr(args, "model_revision", None)
    if not isinstance(revision, str) or not revision.strip() or revision.casefold() in {"main", "master", "latest", "head"}:
        errors.append("immutable-model-revision")
    elif model is not None and manifest is not None:
        try:
            validate_model_manifest(model, manifest, expected_revision=revision)
        except ValueError:
            errors.append("voxcpm-model-manifest-invalid")
    return values, errors


def _local_auxiliary_models(args: argparse.Namespace) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for option, request_field in (("vad_model", "vad_model"), ("punc_model", "punc_model")):
        value = getattr(args, option, "")
        if not value:
            resolved[request_field] = ""
            continue
        path = _existing_path(value, option, directory=True)
        if path is None:
            raise ValueError(f"--{option.replace('_', '-')} must be an existing absolute local directory")
        resolved[request_field] = str(path)
    return resolved


def _atomic_json(path: Path, value: Any) -> None:
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


def command_doctor(args: argparse.Namespace) -> int:
    if args.with_voice:
        _, errors = _voice_dependencies(args, verify_import=True)
        ready_detail = "local VoxCPM Python, CUDA, model path, and manifest are available"
    else:
        _, errors = _core_dependencies(args, verify_import=True)
        ready_detail = "local FFmpeg, FunASR Python, model path, and manifest are available"
    if errors:
        _emit("needs-dependency", detail="missing or unverified local dependency", missing=errors)
        return 2
    _emit("ready", detail=ready_detail)
    return 0


def command_transcribe(args: argparse.Namespace) -> int:
    try:
        auxiliary_models = _local_auxiliary_models(args)
    except ValueError as exc:
        _emit("invalid-input", detail=str(exc))
        return 2
    dependencies, errors = _core_dependencies(args, verify_import=True)
    if errors:
        _emit("needs-dependency", detail="transcription requires local dependencies", missing=errors)
        return 2
    source = _existing_path(args.input, "input")
    if source is None:
        _emit("invalid-input", detail="--input must be an existing absolute file")
        return 2
    try:
        run = create_run(Path(args.hub_root), args.run_id, source=source)
        record_transcription_config(
            run,
            ffmpeg_path=str(dependencies["ffmpeg"]),
            funasr_python=str(dependencies["funasr_python"]),
            model_path=str(dependencies["funasr_model"]),
            model_manifest_path=str(dependencies["funasr_model_manifest"]),
            model_manifest_sha256=sha256_file(dependencies["funasr_model_manifest"]),
            vad_model_path=auxiliary_models["vad_model"] or None,
            punc_model_path=auxiliary_models["punc_model"] or None,
            language=args.language,
        )
        normalized = run.private_dir / "normalized.wav"
        normalize_media(dependencies["ffmpeg"], source, normalized)
        request_path = run.private_dir / "transcription-request.json"
        result_path = run.private_dir / "funasr-result.json"
        _atomic_json(
            request_path,
            {
                "audio_path": str(normalized.resolve(strict=True)),
                "model": str(dependencies["funasr_model"]),
                "model_manifest_path": str(dependencies["funasr_model_manifest"]),
                "model_manifest_sha256": sha256_file(dependencies["funasr_model_manifest"]),
                "vad_model": auxiliary_models["vad_model"],
                "punc_model": auxiliary_models["punc_model"],
                "language": args.language,
                "result_path": str(result_path.resolve(strict=False)),
                "output_dir": str(run.private_dir),
            },
        )
        segments = transcribe_local(
            dependencies["funasr_python"],
            SCRIPT_DIR / "funasr_worker.py",
            request_path.resolve(strict=True),
        )
        save_transcript(run, segments)
    except (FileExistsError, OSError, RuntimeError, ValueError) as exc:
        _emit("processing-failed", detail=str(exc))
        return 2
    _emit(
        "needs-user-confirmation",
        detail="review and redact the transcript before staging an Agent summary",
        run_id=run.run_id,
        next_action="accept-transcript",
    )
    return 0


def _input_file(value: str, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"{label} must be an existing absolute file")
    return path.resolve(strict=True)


def command_accept_transcript(args: argparse.Namespace) -> int:
    try:
        run = load_run(Path(args.hub_root), args.run_id)
        reviewed = _input_file(args.reviewed_transcript, "reviewed transcript")
        accept_transcript(run, reviewed_text=reviewed.read_text(encoding="utf-8"))
    except (GateError, OSError, ValueError) as exc:
        _emit("invalid-input", detail=str(exc))
        return 2
    _emit("ready-for-summary", detail="the current Agent may now create a summary file from the reviewed transcript")
    return 0


def command_summarize(args: argparse.Namespace) -> int:
    try:
        run = load_run(Path(args.hub_root), args.run_id)
        summary = _input_file(args.summary_file, "summary file")
        stage_summary_file(run, summary)
    except (GateError, OSError, ValueError) as exc:
        _emit("invalid-input", detail=str(exc))
        return 2
    _emit("needs-user-confirmation", detail="approve the staged summary before writing Obsidian", next_action="approve-summary")
    return 0


def command_approve_summary(args: argparse.Namespace) -> int:
    try:
        run = load_run(Path(args.hub_root), args.run_id)
        approve_summary(run)
    except (GateError, OSError, ValueError) as exc:
        _emit("invalid-input", detail=str(exc))
        return 2
    _emit("ready-for-obsidian", detail="summary approved; choose an explicit Vault target", next_action="write-obsidian")
    return 0


def command_write_obsidian(args: argparse.Namespace) -> int:
    try:
        run = load_run(Path(args.hub_root), args.run_id)
        vault = Path(args.vault)
        if not vault.is_absolute() or not vault.is_dir():
            raise ValueError("vault must be an existing absolute directory")
        target = write_approved_summary(
            run,
            vault=vault,
            relative=args.relative,
            mode=args.mode,
            overwrite_approved=args.overwrite_approved,
        )
    except (GateError, OSError, ValueError) as exc:
        _emit("invalid-input", detail=str(exc))
        return 2
    _emit("complete", detail="approved summary written to Obsidian", target=str(target))
    return 0


def command_speak(args: argparse.Namespace) -> int:
    dependencies, errors = _voice_dependencies(args, verify_import=True)
    if errors:
        _emit("needs-dependency", detail="local VoxCPM dependencies are unavailable", missing=errors)
        return 2

    created_output = False
    output_dir: Path | None = None
    root: Path | None = None
    voice_timeout = 0
    try:
        root, output_dir = prepare_voice_output(Path(args.hub_root), args.run_id)
        created_output = True
        if args.text is not None:
            speech = direct_speech_input(args.text)
        else:
            if not args.relative:
                raise VoiceGateError("--relative is required with --vault")
            vault = Path(args.vault)
            if not vault.is_absolute():
                raise VoiceGateError("vault must be an absolute path")
            speech = select_speech_input(vault, args.relative, section=args.section)
        voice_timeout = _voice_timeout_seconds(
            getattr(args, "timeout_seconds", None), len(speech.text)
        )
        reference = Path(args.reference_audio) if args.reference_audio else None
        request_path = create_voice_request(
            text=speech.text,
            output_dir=output_dir,
            clone=args.clone,
            reference_audio=reference,
            clone_consent=None,
            retention="",
            model_revision=args.model_revision,
        )
        request = json.loads(request_path.read_text(encoding="utf-8"))
        request["model_path"] = str(dependencies["model_path"])
        request["model_manifest_path"] = str(dependencies["model_manifest"])
        request["model_manifest_sha256"] = sha256_file(dependencies["model_manifest"])
        request["source"] = {
            "kind": speech.source_kind,
            "path": speech.source_path,
            "scope": speech.scope,
            "sha256": speech.source_sha256,
        }
        _atomic_json(request_path, request)
        completed = subprocess.run(
            [str(dependencies["voxcpm_python"]), str(SCRIPT_DIR / "voxcpm_worker.py"), "--request", str(request_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=voice_timeout,
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or str(completed.returncode)
            raise RuntimeError(f"VoxCPM worker failed: {detail}")
        output_path = output_dir / "speech.wav"
        if not output_path.is_file():
            raise RuntimeError("VoxCPM worker did not create speech.wav")
        request["output_sha256"] = sha256_file(output_path)
        _atomic_json(request_path, request)
    except subprocess.TimeoutExpired:
        if created_output and root is not None and output_dir is not None:
            cleanup_voice_output(root, args.run_id)
        _emit(
            "processing-failed",
            detail=f"VoxCPM worker timed out after {voice_timeout} seconds",
        )
        return 2
    except (FileExistsError, OSError, RuntimeError, ValueError, VoiceGateError) as exc:
        if created_output and root is not None and output_dir is not None:
            cleanup_voice_output(root, args.run_id)
        category = "needs-dependency" if isinstance(exc, RuntimeError) else "invalid-input"
        _emit(category, detail=str(exc))
        return 2
    _emit("complete", detail="local audio generated under workflow outputs", output=str(output_path))
    return 0


def _add_core_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ffmpeg")
    parser.add_argument("--funasr-python")
    parser.add_argument("--funasr-model")
    parser.add_argument("--funasr-model-manifest")


def _add_voice_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--voxcpm-python")
    parser.add_argument("--model-path")
    parser.add_argument("--model-revision")
    parser.add_argument("--model-manifest")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meeting-notes")
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor")
    doctor.add_argument("--hub-root", required=True)
    doctor.add_argument("--with-voice", action="store_true")
    _add_core_options(doctor)
    _add_voice_options(doctor)
    doctor.set_defaults(handler=command_doctor)

    transcribe = commands.add_parser("transcribe")
    transcribe.add_argument("--hub-root", required=True)
    transcribe.add_argument("--input", required=True)
    transcribe.add_argument("--run-id", required=True)
    transcribe.add_argument("--vad-model", default="")
    transcribe.add_argument("--punc-model", default="")
    transcribe.add_argument("--language", default="zh")
    _add_core_options(transcribe)
    transcribe.set_defaults(handler=command_transcribe)

    accept = commands.add_parser("accept-transcript")
    accept.add_argument("--hub-root", required=True)
    accept.add_argument("--run-id", required=True)
    accept.add_argument("--reviewed-transcript", required=True)
    accept.set_defaults(handler=command_accept_transcript)

    summarize = commands.add_parser("summarize")
    summarize.add_argument("--hub-root", required=True)
    summarize.add_argument("--run-id", required=True)
    summarize.add_argument("--summary-file", required=True)
    summarize.set_defaults(handler=command_summarize)

    approve = commands.add_parser("approve-summary")
    approve.add_argument("--hub-root", required=True)
    approve.add_argument("--run-id", required=True)
    approve.set_defaults(handler=command_approve_summary)

    write = commands.add_parser("write-obsidian")
    write.add_argument("--hub-root", required=True)
    write.add_argument("--run-id", required=True)
    write.add_argument("--vault", required=True)
    write.add_argument("--relative", required=True)
    write.add_argument("--mode", choices=("new", "append", "overwrite"), default="new")
    write.add_argument("--overwrite-approved", action="store_true")
    write.set_defaults(handler=command_write_obsidian)

    speak = commands.add_parser("speak")
    speak.add_argument("--hub-root", required=True)
    speak.add_argument("--run-id", required=True)
    source = speak.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--vault")
    speak.add_argument("--relative")
    speak.add_argument("--section", default="摘要")
    speak.add_argument("--clone", action="store_true")
    speak.add_argument("--reference-audio")
    speak.add_argument("--timeout-seconds", type=int)
    speak.add_argument("--clone-consent")
    speak.add_argument("--clone-consent-file")
    speak.add_argument("--retention")
    _add_voice_options(speak)
    speak.set_defaults(handler=command_speak)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
