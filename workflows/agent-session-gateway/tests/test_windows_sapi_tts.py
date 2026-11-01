from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


ADAPTER = (
    Path(__file__).resolve().parents[1] / "adapters" / "windows_sapi_tts.py"
)


def test_windows_sapi_uses_file_backed_text_and_fixed_encoded_command(
    tmp_path: Path, monkeypatch
) -> None:
    core = ModuleType("core")
    providers = ModuleType("core.providers")
    tts = ModuleType("core.providers.tts")
    base = ModuleType("core.providers.tts.base")

    class Base:
        def __init__(self, config, delete_audio_file):
            self.output_file = config["output_dir"]
            self.tts_timeout = float(config.get("tts_timeout", 15))

    base.TTSProviderBase = Base
    for name, module in (
        ("core", core),
        ("core.providers", providers),
        ("core.providers.tts", tts),
        ("core.providers.tts.base", base),
    ):
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location("test_windows_sapi", ADAPTER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    powershell = tmp_path / "powershell.exe"
    powershell.write_bytes(b"stub")
    calls: list[tuple[list[str], dict[str, str], str]] = []

    def fake_run(args, **kwargs):
        environment = kwargs["env"]
        source_text = Path(environment["XIAOZHI_SAPI_INPUT"]).read_text(
            encoding="utf-8"
        )
        Path(environment["XIAOZHI_SAPI_OUTPUT"]).write_bytes(b"R" * 64)
        calls.append((args, environment, source_text))
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    # Replacing the adapter's module reference avoids changing global
    # ``os.name`` and making pathlib construct WindowsPath objects on Linux.
    monkeypatch.setattr(
        module,
        "os",
        SimpleNamespace(name="nt", environ=os.environ),
    )
    provider = module.TTSProvider(
        {
            "output_dir": str(tmp_path),
            "powershell_executable": str(powershell.resolve()),
            "voice": "Microsoft Huihui Desktop",
            "rate": 1,
            "volume": 90,
        }
    )
    output = tmp_path / "speech.wav"

    assert asyncio.run(provider.text_to_speak('你好；$(危险输入)', output)) is None
    assert output.stat().st_size == 64
    args, environment, source_text = calls[0]
    assert "-EncodedCommand" in args
    assert all("危险输入" not in item for item in args)
    assert source_text == '你好；$(危险输入)'
    assert environment["XIAOZHI_SAPI_VOICE"] == "Microsoft Huihui Desktop"
    assert not Path(environment["XIAOZHI_SAPI_INPUT"]).exists()
