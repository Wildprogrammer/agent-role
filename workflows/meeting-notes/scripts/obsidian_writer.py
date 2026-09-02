from __future__ import annotations

from pathlib import Path


class VaultError(ValueError):
    pass


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def write_note(
    vault: Path,
    relative: str,
    content: str,
    *,
    mode: str,
    overwrite_approved: bool = False,
) -> Path:
    root = vault.resolve(strict=True)
    requested = Path(relative)
    if requested.is_absolute() or ".." in requested.parts:
        raise VaultError("target is outside vault")

    target = (root / requested).resolve(strict=False)
    if not _within(target, root):
        raise VaultError("target is outside vault")
    target.parent.mkdir(parents=True, exist_ok=True)

    if mode == "new":
        candidate = target
        index = 2
        while candidate.exists():
            candidate = target.with_name(f"{target.stem}-{index}{target.suffix}")
            index += 1
        target = candidate
    elif mode == "append":
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        content = existing + ("\n" if existing else "") + content
    elif mode == "overwrite":
        if target.exists() and not overwrite_approved:
            raise VaultError("overwrite approval required")
    else:
        raise VaultError("mode must be new, append, or overwrite")

    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)
    return target
