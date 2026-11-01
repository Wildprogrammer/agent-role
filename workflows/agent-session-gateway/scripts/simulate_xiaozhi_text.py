"""Send one or more text turns through a local Xiaozhi WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import websockets


async def run(
    uri: str,
    texts: Sequence[str],
    timeout_seconds: float,
    device_id: str,
) -> dict[str, Any]:
    client_id = "xiaozhi-software-simulator"
    separator = "&" if "?" in uri else "?"
    target = (
        f"{uri}{separator}device-id={quote(device_id)}"
        f"&client-id={quote(client_id)}"
    )
    transcript: list[str] = []
    messages: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    async with websockets.connect(target, open_timeout=10, ping_interval=None) as socket:
        await socket.send(
            json.dumps(
                {
                    "type": "hello",
                    "device_id": device_id,
                    "device_name": "software-simulator",
                    "device_mac": device_id,
                    "features": {"mcp": False, "emoji": False},
                    "audio_params": {
                        "format": "opus",
                        "sample_rate": 24000,
                        "channels": 1,
                        "frame_duration": 60,
                    },
                }
            )
        )
        hello_raw = await asyncio.wait_for(socket.recv(), timeout=10)
        if not isinstance(hello_raw, str):
            raise RuntimeError("server hello was not JSON text")
        hello = json.loads(hello_raw)
        if hello.get("type") != "hello" or not hello.get("session_id"):
            raise RuntimeError(f"unexpected server hello: {hello!r}")

        loop = asyncio.get_running_loop()
        for text in texts:
            turn_transcript: list[str] = []
            await socket.send(
                json.dumps({"type": "listen", "state": "start", "mode": "auto"})
            )
            await socket.send(
                json.dumps({"type": "listen", "state": "detect", "text": text})
            )

            deadline = loop.time() + timeout_seconds
            while loop.time() < deadline:
                remaining = deadline - loop.time()
                try:
                    raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if not isinstance(raw, str):
                    continue
                message = json.loads(raw)
                messages.append(message)
                value = message.get("text")
                if isinstance(value, str) and value:
                    if value not in transcript:
                        transcript.append(value)
                    if value not in turn_transcript:
                        turn_transcript.append(value)
                if message.get("type") == "tts" and message.get("state") == "stop":
                    break
            turns.append({"input": text, "transcript": turn_transcript})

    return {
        "status": "ok",
        "session_id": hello["session_id"],
        "inputs": list(texts),
        "turns": turns,
        "transcript": transcript,
        "message_types": [message.get("type") for message in messages],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="+")
    parser.add_argument(
        "--uri", default="ws://127.0.0.1:8000/xiaozhi/v1/"
    )
    parser.add_argument("--device-id", default="02:00:00:00:00:01")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()
    result = asyncio.run(run(args.uri, args.text, args.timeout, args.device_id))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
