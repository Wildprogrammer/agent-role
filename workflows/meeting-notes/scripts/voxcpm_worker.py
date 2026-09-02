from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path


MAX_CHUNK_CHARS = 120

_SENTENCE_ENDS = "。！？!?；;…"
_SOFT_BREAKS = "，,、.：: \t\n"


def _last_break(text: str, start: int, end: int) -> int | None:
    window = text[start:end]
    for breaks in (_SENTENCE_ENDS, _SOFT_BREAKS):
        for index in range(len(window) - 1, 0, -1):
            if window[index] in breaks:
                return start + index + 1
    return None


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split text into bounded, lossless chunks at natural punctuation."""

    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end == len(text):
            chunks.append(text[start:])
            break
        cut = _last_break(text, start, end)
        if cut is None or cut <= start:
            cut = end
        chunks.append(text[start:cut])
        start = cut
    return chunks


def _absolute_directory(value: object, label: str) -> Path:
    if not isinstance(value, str):
        raise ValueError(f"{label} is required")
    path = Path(value)
    if not path.is_absolute() or not path.is_dir():
        raise ValueError(f"{label} must be an existing absolute directory")
    return path.resolve(strict=True)


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


def _inside(root: Path, candidate: Path) -> Path:
    if not candidate.is_absolute():
        raise ValueError("output path must be absolute")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("output path is outside the approved run directory") from exc
    return candidate


def execute(request_path: Path) -> None:
    if not request_path.is_absolute() or not request_path.is_file():
        raise ValueError("request must be an existing absolute JSON file")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    output_dir = _absolute_directory(request.get("output_dir"), "output_dir")
    _assert_no_reparse_ancestors(Path(str(request["output_dir"])))
    if request_path.parent.resolve(strict=True) != output_dir:
        raise ValueError("request must live in the approved run directory")
    output_path = _inside(output_dir, Path(str(request.get("output_path", ""))))
    if output_path.exists():
        raise FileExistsError("voice output already exists")
    model_path = _absolute_directory(request.get("model_path"), "model_path")
    text = request.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required")
    clone = request.get("clone") is True
    reference: Path | None = None
    if clone:
        value = request.get("reference_audio")
        if not isinstance(value, str):
            raise ValueError("clone reference audio is required")
        reference = Path(value)
        if not reference.is_absolute() or not reference.is_file():
            raise ValueError("clone reference audio must be an existing absolute file")

    from voxcpm import VoxCPM
    import soundfile as sf

    model = VoxCPM.from_pretrained(str(model_path), load_denoiser=False)
    chunks = split_text(text)
    arguments: dict[str, object] = {"cfg_value": 2.0, "inference_timesteps": 10}
    if clone and reference is not None:
        arguments["reference_wav_path"] = str(reference)
    wavs = []
    for chunk in chunks:
        arguments["text"] = chunk
        wavs.append(model.generate(**arguments))
    if len(wavs) == 1:
        combined = wavs[0]
    else:
        import numpy as np

        combined = np.concatenate(wavs)
    sf.write(output_path, combined, model.tts_model.sample_rate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="voxcpm-worker")
    parser.add_argument("--request", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        execute(args.request)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"voxcpm-worker: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
