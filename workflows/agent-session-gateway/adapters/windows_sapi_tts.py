"""Local Windows SAPI text-to-speech provider for Xiaozhi Server."""

from __future__ import annotations

import asyncio
import base64
import os
from pathlib import Path
import subprocess
import tempfile

from core.providers.tts.base import TTSProviderBase


_SAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $synth.SelectVoice($env:XIAOZHI_SAPI_VOICE)
    $synth.Rate = [int]$env:XIAOZHI_SAPI_RATE
    $synth.Volume = [int]$env:XIAOZHI_SAPI_VOLUME
    $text = [IO.File]::ReadAllText(
        $env:XIAOZHI_SAPI_INPUT,
        [Text.UTF8Encoding]::new($false)
    )
    $synth.SetOutputToWaveFile($env:XIAOZHI_SAPI_OUTPUT)
    $synth.Speak($text)
} finally {
    $synth.Dispose()
}
"""
_ENCODED_SAPI_SCRIPT = base64.b64encode(_SAPI_SCRIPT.encode("utf-16le")).decode(
    "ascii"
)


class TTSProvider(TTSProviderBase):
    """Generate real local WAV speech through the Windows SAPI runtime."""

    def __init__(self, config, delete_audio_file=True):
        super().__init__(config, delete_audio_file)
        if os.name != "nt":
            raise ValueError("windows_sapi is available only on Windows")
        self.powershell = Path(
            config.get(
                "powershell_executable",
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            )
        )
        if not self.powershell.is_absolute() or not self.powershell.is_file():
            raise ValueError("powershell_executable must be an existing absolute file")
        self.voice = str(config.get("voice", "Microsoft Huihui Desktop")).strip()
        if not self.voice or len(self.voice) > 128:
            raise ValueError("voice must be a non-empty string of at most 128 characters")
        self.rate = int(config.get("rate", 0))
        self.volume = int(config.get("volume", 100))
        if not -10 <= self.rate <= 10:
            raise ValueError("rate must be between -10 and 10")
        if not 0 <= self.volume <= 100:
            raise ValueError("volume must be between 0 and 100")
        self.audio_file_type = "wav"

    def _synthesize(self, text: str, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        text_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".txt",
                prefix="xiaozhi-sapi-",
                dir=target.parent,
                delete=False,
            ) as source:
                source.write(text)
                text_path = Path(source.name)
            environment = os.environ.copy()
            environment.update(
                {
                    "XIAOZHI_SAPI_INPUT": str(text_path),
                    "XIAOZHI_SAPI_OUTPUT": str(target),
                    "XIAOZHI_SAPI_VOICE": self.voice,
                    "XIAOZHI_SAPI_RATE": str(self.rate),
                    "XIAOZHI_SAPI_VOLUME": str(self.volume),
                }
            )
            completed = subprocess.run(
                [
                    str(self.powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    _ENCODED_SAPI_SCRIPT,
                ],
                check=False,
                capture_output=True,
                env=environment,
                timeout=self.tts_timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"Windows SAPI failed: {detail[:300]}")
            if not target.is_file() or target.stat().st_size <= 44:
                raise RuntimeError("Windows SAPI did not produce a valid WAV file")
        finally:
            if text_path is not None:
                text_path.unlink(missing_ok=True)

    async def text_to_speak(self, text, output_file):
        temporary_output = output_file is None
        if temporary_output:
            Path(self.output_file).mkdir(parents=True, exist_ok=True)
            descriptor, path = tempfile.mkstemp(
                suffix=".wav", prefix="xiaozhi-sapi-", dir=self.output_file
            )
            os.close(descriptor)
            target = Path(path).resolve()
            target.unlink(missing_ok=True)
        else:
            target = Path(output_file).resolve()
        try:
            await asyncio.to_thread(self._synthesize, str(text), target)
            if temporary_output:
                return target.read_bytes()
            return None
        finally:
            if temporary_output:
                target.unlink(missing_ok=True)
