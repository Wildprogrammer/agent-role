from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")

_STATES = (
    "created",
    "needs-transcript-review",
    "transcript-reviewed",
    "needs-summary-confirmation",
    "summary-approved",
    "complete",
)


class GateError(ValueError):
    """Raised when a user-confirmation gate has not been passed."""


@dataclass(frozen=True)
class MeetingRun:
    root: Path
    run_id: str
    manifest_path: Path

    @property
    def private_dir(self) -> Path:
        return self.manifest_path.parent

    @property
    def output_dir(self) -> Path:
        return self.root / "workflows" / "meeting-notes" / "outputs" / self.run_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except FileNotFoundError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    is_junction = getattr(path, "is_junction", lambda: False)
    return path.is_symlink() or bool(attributes & reparse) or is_junction()


def _assert_no_reparse_ancestors(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            if _is_reparse_point(current):
                raise ValueError(f"unsafe reparse-point path: {current}")


def _hub_root(hub_root: Path) -> Path:
    root = hub_root.absolute()
    if not root.is_dir():
        raise ValueError(f"hub root must be an existing directory: {root}")
    _assert_no_reparse_ancestors(root)
    return root.resolve(strict=True)


def _path_in_root(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe path outside hub root: {candidate}") from exc
    _assert_no_reparse_ancestors(candidate.parent)
    return candidate


def _mkdir_safely(root: Path, path: Path) -> None:
    _path_in_root(root, *path.relative_to(root).parts)
    _assert_no_reparse_ancestors(path.parent)
    path.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_ancestors(path)


def _atomic_text(root: Path, path: Path, text: str) -> None:
    _mkdir_safely(root, path.parent)
    _path_in_root(root, *path.relative_to(root).parts)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _atomic_json(root: Path, path: Path, value: Any) -> None:
    _atomic_text(root, path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_run_id(run_id: str) -> None:
    if not RUN_ID.fullmatch(run_id):
        raise ValueError(f"invalid run id: {run_id!r}")


def _private_dir(root: Path, run_id: str) -> Path:
    return _path_in_root(root, "workspace", "workflows", "meeting-notes", "runs", run_id)


def _voice_output_dir(root: Path, run_id: str) -> Path:
    return _path_in_root(root, "workflows", "meeting-notes", "outputs", run_id)


def prepare_voice_output(hub_root: Path, run_id: str) -> tuple[Path, Path]:
    """Create a new voice-output directory using the run-path safety checks."""

    _validate_run_id(run_id)
    root = _hub_root(hub_root)
    output_dir = _voice_output_dir(root, run_id)
    if output_dir.exists() or output_dir.is_symlink():
        raise FileExistsError(f"voice output run already exists: {run_id}")
    _mkdir_safely(root, output_dir)
    return root, output_dir


def cleanup_voice_output(hub_root: Path, run_id: str) -> None:
    """Remove only a newly-created, safe voice-output directory."""

    _validate_run_id(run_id)
    root = _hub_root(hub_root)
    output_dir = _voice_output_dir(root, run_id)
    if not output_dir.exists():
        return
    _assert_no_reparse_ancestors(output_dir)
    if not output_dir.is_dir():
        raise ValueError(f"voice output is not a directory: {output_dir}")
    shutil.rmtree(output_dir)


def _read_manifest(run: MeetingRun) -> dict[str, Any]:
    _assert_no_reparse_ancestors(run.manifest_path)
    try:
        data = json.loads(run.manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"meeting run not found: {run.run_id}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid meeting run manifest: {run.manifest_path}") from exc
    if not isinstance(data, dict) or data.get("run_id") != run.run_id:
        raise ValueError(f"invalid meeting run manifest: {run.manifest_path}")
    if data.get("state") not in _STATES:
        raise ValueError(f"invalid meeting run state: {data.get('state')!r}")
    if not isinstance(data.get("source"), dict):
        raise ValueError("meeting run manifest is missing source evidence")
    return data


def _write_manifest(run: MeetingRun, manifest: dict[str, Any]) -> None:
    _atomic_json(run.root, run.manifest_path, manifest)


def _require_state(run: MeetingRun, expected: str) -> dict[str, Any]:
    manifest = _read_manifest(run)
    if manifest["state"] != expected:
        raise GateError(f"requires state {expected}, found {manifest['state']}")
    return manifest


def _transition(run: MeetingRun, manifest: dict[str, Any], target: str) -> None:
    current_index = _STATES.index(manifest["state"])
    target_index = _STATES.index(target)
    if target_index != current_index + 1:
        raise GateError(f"invalid state transition: {manifest['state']} -> {target}")
    manifest["state"] = target
    manifest["updated_at"] = _now()
    _write_manifest(run, manifest)


def create_run(hub_root: Path, run_id: str, *, source: Path) -> MeetingRun:
    """Create a run without copying or altering the source media."""

    _validate_run_id(run_id)
    root = _hub_root(hub_root)
    resolved_source = source.resolve(strict=True)
    if not resolved_source.is_file():
        raise ValueError(f"source must be a file: {source}")
    private_dir = _private_dir(root, run_id)
    if private_dir.exists() or private_dir.is_symlink():
        raise FileExistsError(f"meeting run already exists: {run_id}")
    _mkdir_safely(root, private_dir)
    manifest_path = private_dir / "manifest.json"
    run = MeetingRun(root=root, run_id=run_id, manifest_path=manifest_path)
    stat_result = resolved_source.stat()
    _write_manifest(
        run,
        {
            "schema_version": 1,
            "run_id": run_id,
            "created_at": _now(),
            "updated_at": _now(),
            "state": "created",
            "source": {
                "absolute_path": str(resolved_source),
                "sha256": _sha256(resolved_source),
                "size_bytes": stat_result.st_size,
            },
        },
    )
    return run


def load_run(hub_root: Path, run_id: str) -> MeetingRun:
    _validate_run_id(run_id)
    root = _hub_root(hub_root)
    manifest_path = _private_dir(root, run_id) / "manifest.json"
    run = MeetingRun(root=root, run_id=run_id, manifest_path=manifest_path)
    _read_manifest(run)
    return run


def record_transcription_config(
    run: MeetingRun,
    *,
    ffmpeg_path: str,
    funasr_python: str,
    model_path: str,
    model_manifest_path: str,
    model_manifest_sha256: str,
    vad_model_path: str | None,
    punc_model_path: str | None,
    language: str,
) -> None:
    """Bind locally verified ASR dependencies before source processing begins."""

    manifest = _require_state(run, "created")
    required = {
        "ffmpeg_path": ffmpeg_path,
        "funasr_python": funasr_python,
        "model_path": model_path,
        "model_manifest_path": model_manifest_path,
        "language": language,
    }
    if not all(isinstance(value, str) and value.strip() for value in required.values()):
        raise ValueError("transcription configuration fields must be nonempty strings")
    if not re.fullmatch(r"[0-9a-f]{64}", model_manifest_sha256, re.IGNORECASE):
        raise ValueError("model manifest SHA-256 must be a hexadecimal digest")
    manifest["transcription"] = {
        **required,
        "model_manifest_sha256": model_manifest_sha256.casefold(),
        "vad_model_path": vad_model_path,
        "punc_model_path": punc_model_path,
    }
    manifest["updated_at"] = _now()
    _write_manifest(run, manifest)


def _validated_segments(segments: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for index, segment in enumerate(segments):
        start = segment.get("start_ms")
        end = segment.get("end_ms")
        text = segment.get("text")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or not isinstance(text, str)
        ):
            raise ValueError(f"invalid transcript segment at index {index}")
        evidence = segment.get("timestamp_source")
        if evidence is not None and evidence not in {"provider", "derived-chunk"}:
            raise ValueError(f"invalid timestamp source at index {index}")
        result: dict[str, object] = {"start_ms": start, "end_ms": end, "text": text}
        if evidence is not None:
            result["timestamp_source"] = evidence
        normalized.append(result)
    return normalized


def save_transcript(run: MeetingRun, segments: list[dict[str, object]]) -> Path:
    """Save raw local ASR output and open the transcript-review gate."""

    manifest = _require_state(run, "created")
    transcript = _validated_segments(segments)
    target = run.private_dir / "transcript.json"
    _atomic_json(run.root, target, {"segments": transcript})
    _transition(run, manifest, "needs-transcript-review")
    return target


def accept_transcript(run: MeetingRun, *, reviewed_text: str) -> Path:
    """Persist human-reviewed transcript text and permit Agent summarization."""

    manifest = _require_state(run, "needs-transcript-review")
    if not reviewed_text.strip():
        raise ValueError("reviewed transcript must not be empty")
    content = reviewed_text.rstrip() + "\n"
    _atomic_text(run.root, run.private_dir / "reviewed-transcript.md", content)
    public_copy = run.output_dir / "transcript.md"
    _atomic_text(run.root, public_copy, content)
    _transition(run, manifest, "transcript-reviewed")
    return public_copy


def stage_summary(run: MeetingRun, summary: str) -> Path:
    """Stage an Agent-generated summary after transcript review."""

    manifest = _read_manifest(run)
    if manifest["state"] != "transcript-reviewed":
        raise GateError("transcript review must be accepted before summary")
    if not summary.strip():
        raise ValueError("summary must not be empty")
    target = run.private_dir / "summary-draft.md"
    _atomic_text(run.root, target, summary.rstrip() + "\n")
    _transition(run, manifest, "needs-summary-confirmation")
    return target


def stage_summary_file(run: MeetingRun, summary_file: Path) -> Path:
    """Bind an Agent-written Markdown file to a reviewed meeting run."""

    manifest = _read_manifest(run)
    if manifest["state"] != "transcript-reviewed":
        raise GateError("transcript review must be accepted before summary")
    source = summary_file.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"summary file must be a file: {summary_file}")
    content = source.read_text(encoding="utf-8")
    if not content.strip():
        raise ValueError("summary file must not be empty")
    target = run.private_dir / "summary-draft.md"
    _atomic_text(run.root, target, content.rstrip() + "\n")
    manifest["summary"] = {
        "source_path": str(source),
        "source_sha256": _sha256(source),
        "draft_sha256": _sha256(target),
    }
    _transition(run, manifest, "needs-summary-confirmation")
    return target


def approve_summary(run: MeetingRun) -> Path:
    """Publish the approved summary to workflow outputs, not the Vault."""

    manifest = _require_state(run, "needs-summary-confirmation")
    draft = run.private_dir / "summary-draft.md"
    try:
        content = draft.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GateError("summary draft is missing") from exc
    target = run.output_dir / "meeting-notes.md"
    _atomic_text(run.root, target, content)
    _transition(run, manifest, "summary-approved")
    return target


def write_approved_summary(
    run: MeetingRun,
    *,
    vault: Path,
    relative: str,
    mode: str = "new",
    overwrite_approved: bool = False,
) -> Path:
    """Write an approved summary to an explicit Vault target and close the run."""

    manifest = _read_manifest(run)
    if manifest["state"] != "summary-approved":
        raise GateError("summary approval is required before writing Obsidian")
    summary_path = run.output_dir / "meeting-notes.md"
    try:
        content = summary_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GateError("approved summary is missing") from exc

    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from obsidian_writer import write_note

    target = write_note(
        vault,
        relative,
        content,
        mode=mode,
        overwrite_approved=overwrite_approved,
    )
    resolved_target = target.resolve(strict=True)
    manifest["vault_delivery"] = {
        "absolute_path": str(resolved_target),
        "sha256": _sha256(resolved_target),
        "mode": mode,
    }
    _transition(run, manifest, "complete")
    return resolved_target
