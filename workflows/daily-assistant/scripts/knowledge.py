from __future__ import annotations

from pathlib import Path


ALLOWED_KINDS = frozenset({"classification", "priority", "duration"})
FORBIDDEN_TERMS = frozenset(
    {"raw_input", "raw_text", "original_message", "source_message"}
)
MAX_ENTRIES = 30


class KnowledgeStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[tuple[str, str, str]]:
        if not self.path.exists():
            return []
        entries = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) != 3 or cells[0] in {"类型", "---"}:
                continue
            entries.append((cells[0], cells[1], cells[2]))
        return entries

    def upsert(self, *, kind: str, key: str, value: str) -> None:
        self._validate(kind=kind, key=key, value=value)
        existing = [
            entry
            for entry in self.entries()
            if (entry[0], entry[1]) != (kind, key)
        ]
        existing.append((kind, key, value))
        self._write(existing[-MAX_ENTRIES:])

    def _validate(self, *, kind: str, key: str, value: str) -> None:
        if kind not in ALLOWED_KINDS:
            raise ValueError("kind is invalid")
        for name, item in (("key", key), ("value", value)):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"{name} must be a non-empty string")
            if "|" in item or "\n" in item or "\r" in item:
                raise ValueError(f"{name} contains unsupported Markdown characters")
            if any(term in item.casefold() for term in FORBIDDEN_TERMS):
                raise ValueError(f"raw field is not allowed in {name}")

    def _write(self, entries: list[tuple[str, str, str]]) -> None:
        lines = [
            "# 日报助手经验",
            "",
            "| 类型 | 键 | 已确认经验 |",
            "| --- | --- | --- |",
        ]
        lines.extend(
            f"| {kind} | {key} | {value} |" for kind, key, value in entries
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(self.path)
