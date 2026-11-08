"""Isolated browser acceptance fixture; never connects to a real Agent host."""
import asyncio
import io
import math
import struct
import tempfile
import wave
from pathlib import Path

from aiohttp import web
from test_mobile_channel import Adapter
from agent_workflow_hub.xiaozhi_hermes_bridge.mobile_channel import MobileChannel


class DemoAdapter(Adapter):
    async def dispatch_bound(self, session, **kwargs):
        self.sent.append(kwargs)
        await kwargs["on_progress"]("软件验证：正在接收手机的问题……")
        await asyncio.sleep(0.3)
        return {"status": "completed", "output": "软件验证：手机可以独立显示回答和播放音频。\n本页面使用测试智能体，没有向 Hermes 或 DeepSeek Harness 发送消息。"}


async def transcribe(pcm, session):
    return "软件验证：已收到手机录音"


async def synthesize(text):
    data = io.BytesIO()
    with wave.open(data, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b"".join(struct.pack("<h", int(900*math.sin(2*math.pi*440*i/16000))) for i in range(3200)))
    return data.getvalue()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="lingxi-phone-browser-") as root:
        channel = MobileChannel(DemoAdapter(), transcribe=transcribe, synthesize=synthesize,
            attachment_root=Path(root), password="test-password", secure_cookie=False,
            origins=["http://127.0.0.1:18767"])
        web.run_app(channel.app, host="127.0.0.1", port=18767, access_log=None)
