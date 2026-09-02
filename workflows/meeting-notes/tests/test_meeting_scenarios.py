import json
from pathlib import Path


def test_transcript_fixture_has_no_speaker_identity():
    data = json.loads(
        Path("workflows/meeting-notes/tests/fixtures/transcript-segments.json").read_text(
            encoding="utf-8"
        )
    )

    assert all("speaker" not in segment for segment in data["segments"])
    assert any("[听不清" in segment["text"] for segment in data["segments"])
