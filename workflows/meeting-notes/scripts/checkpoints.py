from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    id: str
    start_ms: int
    end_ms: int


class CheckpointStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def initialize(self, source_sha256: str, segments: list[Segment]) -> None:
        existing = self._load()
        if existing and existing["source_sha256"] != source_sha256:
            raise ValueError("source fingerprint changed")
        if existing:
            return
        self._save(
            {
                "source_sha256": source_sha256,
                "segments": {
                    segment.id: {
                        **asdict(segment),
                        "status": "pending",
                        "text": None,
                        "output_sha256": None,
                        "error": None,
                    }
                    for segment in segments
                },
            }
        )

    def complete(
        self,
        segment_id: str,
        *,
        text: str,
        output_sha256: str,
    ) -> None:
        data = self._load()
        item = data["segments"][segment_id]
        item.update(
            status="done",
            text=text,
            output_sha256=output_sha256,
            error=None,
        )
        self._save(data)

    def fail(self, segment_id: str, *, error: str) -> None:
        data = self._load()
        item = data["segments"][segment_id]
        item.update(status="failed", error=error)
        self._save(data)

    def pending(self) -> list[Segment]:
        data = self._load()
        return [
            Segment(item["id"], item["start_ms"], item["end_ms"])
            for item in data["segments"].values()
            if item["status"] != "done"
        ]
