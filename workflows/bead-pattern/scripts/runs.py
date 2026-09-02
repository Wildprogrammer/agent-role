from __future__ import annotations

import json
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pipeline import Candidate


RUN_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
REPARSE_POINT = 0x400


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError("run-id must match [a-z0-9][a-z0-9-]{0,63}")


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    attributes = getattr(path.stat(), "st_file_attributes", 0)
    return bool(attributes & REPARSE_POINT)


def _checked_hub_root(hub_root: Path) -> Path:
    root = Path(hub_root).resolve(strict=True)
    if not root.is_dir() or _is_reparse(root):
        raise ValueError("hub root must be a normal existing directory")
    return root


def _run_directory(hub_root: Path, run_id: str) -> Path:
    _validate_run_id(run_id)
    root = _checked_hub_root(hub_root)
    run_directory = root / "workspace" / "workflows" / "bead-pattern" / "runs" / run_id
    if not run_directory.is_relative_to(root):
        raise ValueError("run directory escapes hub root")
    return run_directory


def _ensure_normal_ancestors(root: Path, path: Path) -> None:
    current = root
    for part in path.relative_to(root).parts:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise ValueError("run directory cannot use a symlink or reparse point")


def _write_json_once(path: Path, payload: dict[str, object]) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path.name}")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(encoded)
        temporary_path = Path(temporary.name)
    try:
        if path.exists():
            raise FileExistsError(f"refusing to overwrite {path.name}")
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def create_candidate(hub_root: Path, run_id: str, candidate: "Candidate") -> Path:
    directory = _run_directory(hub_root, run_id)
    root = _checked_hub_root(hub_root)
    _ensure_normal_ancestors(root, directory.parent)
    directory.mkdir(parents=True, exist_ok=False)
    try:
        candidate_path = directory / "candidate.json"
        _write_json_once(candidate_path, candidate.to_dict())
        return candidate_path
    except BaseException:
        directory.rmdir()
        raise


def _read_candidate(path: Path) -> "Candidate":
    from pipeline import Candidate

    if not path.is_file():
        raise FileNotFoundError("candidate does not exist")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("candidate JSON must contain an object")
    return Candidate.from_dict(raw)


def accept_candidate(hub_root: Path, run_id: str) -> "Candidate":
    directory = _run_directory(hub_root, run_id)
    candidate = _read_candidate(directory / "candidate.json")
    accepted_path = directory / "pattern.json"
    _write_json_once(accepted_path, candidate.to_dict())
    return candidate


def load_accepted(hub_root: Path, run_id: str) -> "Candidate":
    return _read_candidate(_run_directory(hub_root, run_id) / "pattern.json")
