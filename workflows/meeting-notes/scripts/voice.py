from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class VoiceGateError(ValueError):
    """Raised when local TTS input or clone authorization is insufficient."""


@dataclass(frozen=True)
class SpeechInput:
    text: str
    source_kind: str
    source_path: str | None
    scope: str
    source_sha256: str


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
                raise VoiceGateError(f"unsafe reparse-point path: {current}")


def _safe_vault_note(vault: Path, relative: str) -> tuple[Path, Path]:
    root = vault.resolve(strict=True)
    if not root.is_dir():
        raise VoiceGateError("vault must be an existing directory")
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise VoiceGateError("note is outside vault")
    target = (root / requested).resolve(strict=True)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise VoiceGateError("note is outside vault") from exc
    if not target.is_file():
        raise VoiceGateError("selected Vault note must be a file")
    return root, target


def _section_text(markdown: str, section: str) -> str:
    lines = markdown.splitlines()
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and len(match.group(1)) == 2 and match.group(2).strip() == section:
            start = index + 1
            level = len(match.group(1))
            break
    if start is None:
        raise VoiceGateError(f"section not found: {section}")
    end = len(lines)
    for index in range(start, len(lines)):
        match = _HEADING.match(lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    text = "\n".join(lines[start:end]).strip()
    if not text:
        raise VoiceGateError(f"section is empty: {section}")
    return text


def select_speech_input(vault: Path, relative: str, *, section: str = "摘要") -> SpeechInput:
    """Select a user-approved Vault section without changing the note."""

    _, note = _safe_vault_note(vault, relative)
    text = _section_text(note.read_text(encoding="utf-8"), section)
    return SpeechInput(
        text=text,
        source_kind="vault-note",
        source_path=str(note),
        scope="summary" if section == "摘要" else section,
        source_sha256=sha256_file(note),
    )


def direct_speech_input(text: str) -> SpeechInput:
    if not isinstance(text, str) or not text.strip():
        raise VoiceGateError("speech text must not be empty")
    rendered = text.strip()
    return SpeechInput(
        text=rendered,
        source_kind="direct-text",
        source_path=None,
        scope="direct",
        source_sha256=_text_sha256(rendered),
    )


def _atomic_json(path: Path, value: dict[str, object]) -> None:
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


def create_voice_request(
    *,
    text: str,
    output_dir: Path,
    clone: bool,
    reference_audio: Path | None,
    clone_consent: str | None,
    retention: str = "",
    model_revision: str = "",
) -> Path:
    """Create immutable local-TTS provenance before a VoxCPM worker is invoked."""

    speech = direct_speech_input(text)
    reference: Path | None = None
    if clone:
        if reference_audio is None:
            raise VoiceGateError("reference audio is required for voice cloning")
        reference = reference_audio.resolve(strict=True)
        if not reference.is_file():
            raise VoiceGateError("reference audio must be a file")

    if not isinstance(model_revision, str) or not model_revision.strip():
        raise VoiceGateError("immutable model revision is required")
    revision = model_revision.strip()
    if revision.casefold() in {"main", "master", "latest", "head"}:
        raise VoiceGateError("model revision must not be a mutable alias")

    if not output_dir.is_absolute():
        raise VoiceGateError("voice output directory must be absolute")
    _assert_no_reparse_ancestors(output_dir.parent)
    if output_dir.exists():
        _assert_no_reparse_ancestors(output_dir)
        if not output_dir.is_dir() or (output_dir / "voice-request.json").exists() or (output_dir / "speech.wav").exists():
            raise FileExistsError("voice output directory already exists")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
        _assert_no_reparse_ancestors(output_dir)
    request_path = output_dir / "voice-request.json"
    output_path = output_dir / "speech.wav"
    request: dict[str, object] = {
        "provider": "VoxCPM",
        "text": speech.text,
        "text_sha256": speech.source_sha256,
        "clone": clone,
        "model_revision": revision,
        "output_dir": str(output_dir),
        "output_path": str(output_path),
    }
    if clone and reference is not None:
        request.update(
            {
                "reference_audio": str(reference),
                "reference_audio_sha256": sha256_file(reference),
            }
        )
    _atomic_json(request_path, request)
    return request_path
