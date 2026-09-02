"""UTF-8 Markdown report hashing and atomic file writing."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .model import ReportingError


_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def report_sha256(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def write_test_report(
    output_root: Path,
    *,
    run_id: str,
    markdown: str,
) -> Path:
    """Write one UTF-8 report under a safe run-scoped output directory."""

    _validate_run_id(run_id)
    if not isinstance(output_root, Path) or not output_root.is_absolute():
        raise ReportingError("report output root must be an absolute path")
    if not isinstance(markdown, str) or not markdown:
        raise ReportingError("report Markdown is required")
    target_dir = output_root / run_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "test-report.md"
    temporary = target.with_suffix(".md.tmp")
    temporary.write_text(markdown, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def write_test_report_file(output_path: Path, *, markdown: str) -> Path:
    """Atomically replace one explicitly configured report file."""

    if not isinstance(output_path, Path) or not output_path.is_absolute():
        raise ReportingError("report output path must be an absolute path")
    if not isinstance(markdown, str) or not markdown:
        raise ReportingError("report Markdown is required")
    target = output_path.resolve()
    if target.exists() and target.is_dir():
        raise ReportingError("report output path must name a file")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp")
    temporary.write_text(markdown, encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target


def _validate_run_id(run_id: str) -> None:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ReportingError("run ID is invalid")
