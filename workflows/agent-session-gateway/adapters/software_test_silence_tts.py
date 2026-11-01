"""Silent local TTS used only for hardware-free Xiaozhi protocol validation."""

from __future__ import annotations

import io
import wave

from core.providers.tts.base import TTSProviderBase


class TTSProvider(TTSProviderBase):
    """Return a short valid WAV so text events can be tested without cloud TTS."""

    def __init__(self, config, delete_audio_file=True):
        super().__init__(config, delete_audio_file)
        self.sample_rate = int(config.get("sample_rate", 24000))
        self.duration_ms = int(config.get("duration_ms", 120))
        if not 8_000 <= self.sample_rate <= 48_000:
            raise ValueError("sample_rate must be between 8000 and 48000")
        if not 20 <= self.duration_ms <= 1_000:
            raise ValueError("duration_ms must be between 20 and 1000")
        self.audio_file_type = "wav"

    async def text_to_speak(self, text, output_file):
        frame_count = self.sample_rate * self.duration_ms // 1000
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self.sample_rate)
            wav_file.writeframes(b"\x00\x00" * frame_count)
        audio = buffer.getvalue()
        if output_file:
            with open(output_file, "wb") as destination:
                destination.write(audio)
            return None
        return audio
