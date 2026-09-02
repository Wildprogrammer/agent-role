import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "voice.py"
SPEC = importlib.util.spec_from_file_location("meeting_notes_voice", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
VoiceGateError = MODULE.VoiceGateError
create_voice_request = MODULE.create_voice_request
direct_speech_input = MODULE.direct_speech_input
select_speech_input = MODULE.select_speech_input
sha256_file = MODULE.sha256_file
WORKER_SCRIPT = Path(__file__).parents[1] / "scripts" / "voxcpm_worker.py"
WORKER_SPEC = importlib.util.spec_from_file_location("meeting_notes_voxcpm_worker", WORKER_SCRIPT)
assert WORKER_SPEC and WORKER_SPEC.loader
WORKER = importlib.util.module_from_spec(WORKER_SPEC)
sys.modules[WORKER_SPEC.name] = WORKER
WORKER_SPEC.loader.exec_module(WORKER)


class _FakeArray:
    """Lightweight 1D array stand-in so Hub contract tests need no numpy."""

    def __init__(self, values):
        self._values = list(values)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


class _FakeNumpy:
    """Minimal numpy stand-in: concatenate 1D chunk results in order."""

    def concatenate(self, arrays):
        if not arrays:
            raise ValueError("need at least one array to concatenate")
        merged = []
        for array in arrays:
            merged.extend(array)
        return merged


LONG_SENTENCE = "这是用于验证长文本切块的测试句子内容。"


def make_vault(tmp_path: Path, content: str) -> Path:
    vault = tmp_path / "vault"
    note = vault / "Meetings" / "demo.md"
    note.parent.mkdir(parents=True)
    note.write_text(content, encoding="utf-8")
    return vault


def test_vault_input_defaults_to_summary_and_binds_file_hash(tmp_path: Path):
    vault = make_vault(
        tmp_path,
        "# 会议\n\n## 摘要\n发布在周一。\n\n## 决策\n使用蓝色。\n",
    )

    request = select_speech_input(vault, "Meetings/demo.md")

    assert request.text == "发布在周一。"
    assert request.scope == "summary"
    assert request.source_sha256 == sha256_file(vault / "Meetings/demo.md")


def test_clone_request_records_reference_hash_without_consent_or_retention(tmp_path: Path):
    with pytest.raises(VoiceGateError, match="reference audio is required"):
        create_voice_request(
            text="你好",
            output_dir=(tmp_path / "output").resolve(),
            clone=True,
            reference_audio=None,
            clone_consent=None,
        )

    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"authorized voice")
    request_path = create_voice_request(
        text="你好",
        output_dir=(tmp_path / "output").resolve(),
        clone=True,
        reference_audio=reference.resolve(),
        clone_consent=None,
        retention="",
        model_revision="0123456789abcdef",
    )
    request = json.loads(request_path.read_text(encoding="utf-8"))

    assert request["provider"] == "VoxCPM"
    assert request["reference_audio_sha256"] == sha256_file(reference)
    assert "clone_consent" not in request
    assert "retention" not in request
    assert "speaker" not in request


def test_direct_text_rejects_blank_input():
    with pytest.raises(VoiceGateError, match="text"):
        direct_speech_input("   ")


def test_voxcpm_worker_writes_only_the_requested_output_without_a_service(tmp_path: Path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    model = tmp_path / "model"
    model.mkdir()
    output = output_dir / "speech.wav"
    request = output_dir / "voice-request.json"
    request.write_text(
        json.dumps(
            {
                "output_dir": str(output_dir.resolve()),
                "output_path": str(output.resolve()),
                "model_path": str(model.resolve()),
                "text": "测试",
                "clone": False,
            }
        ),
        encoding="utf-8",
    )

    class FakeVoxCPM:
        @classmethod
        def from_pretrained(cls, _path, *, load_denoiser):
            assert load_denoiser is False
            return SimpleNamespace(
                generate=lambda **kwargs: [kwargs["text"]],
                tts_model=SimpleNamespace(sample_rate=48_000),
            )

    monkeypatch.setitem(sys.modules, "voxcpm", SimpleNamespace(VoxCPM=FakeVoxCPM))
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(write=lambda path, _wav, _rate: Path(path).write_bytes(b"fake-wav")),
    )

    WORKER.execute(request.resolve())

    assert output.read_bytes() == b"fake-wav"
    worker_source = WORKER_SCRIPT.read_text(encoding="utf-8")
    assert "app.py" not in worker_source
    assert "FastAPI" not in worker_source


def test_split_text_is_lossless_bounded_and_prefers_punctuation():
    text = LONG_SENTENCE * 20

    chunks = WORKER.split_text(text)

    assert "".join(chunks) == text
    assert len(chunks) > 1
    assert all(len(chunk) <= WORKER.MAX_CHUNK_CHARS for chunk in chunks)
    assert all(chunk.endswith(("。", "！", "？", "；", "，")) for chunk in chunks[:-1])


@pytest.mark.parametrize("chunk_factory", [list, _FakeArray])
def test_worker_merges_chunk_results_in_order_and_writes_once(
    tmp_path: Path, monkeypatch, chunk_factory
):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    model = tmp_path / "model"
    model.mkdir()
    output = output_dir / "speech.wav"
    text = LONG_SENTENCE * 30
    request = output_dir / "voice-request.json"
    request.write_text(
        json.dumps(
            {
                "output_dir": str(output_dir.resolve()),
                "output_path": str(output.resolve()),
                "model_path": str(model.resolve()),
                "text": text,
                "clone": False,
            }
        ),
        encoding="utf-8",
    )

    generated: list[str] = []
    loaded: list[str] = []
    chunk_samples = [0.5, 1.5]

    class FakeVoxCPM:
        @classmethod
        def from_pretrained(cls, path, *, load_denoiser):
            assert load_denoiser is False
            loaded.append(str(path))
            return SimpleNamespace(
                generate=lambda **kwargs: generated.append(kwargs["text"])
                or chunk_factory(chunk_samples),
                tts_model=SimpleNamespace(sample_rate=48_000),
            )

    writes: list[tuple[Path, object, int]] = []

    def fake_write(path, wav, rate):
        writes.append((path, wav, rate))
        Path(path).write_bytes(b"fake-wav")

    monkeypatch.setitem(sys.modules, "voxcpm", SimpleNamespace(VoxCPM=FakeVoxCPM))
    monkeypatch.setitem(sys.modules, "numpy", _FakeNumpy())
    monkeypatch.setitem(
        sys.modules,
        "soundfile",
        SimpleNamespace(write=fake_write),
    )

    WORKER.execute(request.resolve())

    chunks = WORKER.split_text(text)
    assert len(chunks) > 1
    assert generated == chunks
    assert loaded == [str(model.resolve())]
    assert len(writes) == 1
    path, wav, rate = writes[0]
    assert Path(path) == output
    assert rate == 48_000
    assert list(wav) == chunk_samples * len(chunks)
    assert len(wav) == len(chunk_samples) * len(chunks)
    assert output.read_bytes() == b"fake-wav"
