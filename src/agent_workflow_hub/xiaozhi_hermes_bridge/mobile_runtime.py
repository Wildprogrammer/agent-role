"""Opt-in phone listener hosted by the existing Xiaozhi process (Python 3.10)."""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import ssl
from pathlib import Path
from urllib.parse import urlsplit


def load_mobile_settings(path: Path) -> dict:
    if not path.is_absolute():
        raise ValueError("mobile config path must be absolute")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    keys = {"host", "port", "origins", "certificate", "private_key", "attachment_root"}
    if not isinstance(value, dict) or set(value) - keys or not {"host", "port", "origins", "attachment_root"} <= set(value):
        raise ValueError("invalid mobile config fields")
    address = ipaddress.ip_address(value["host"])
    if type(value["port"]) is not int or not 1024 <= value["port"] <= 65535:
        raise ValueError("invalid mobile port")
    origins = value["origins"]
    if not isinstance(origins, list) or not origins or len(origins) > 8:
        raise ValueError("mobile origins must be a nonempty list")
    for origin in origins:
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("mobile origins must be exact HTTPS origins")
    if bool(value.get("certificate")) != bool(value.get("private_key")):
        raise ValueError("certificate and private_key must be configured together")
    if not address.is_loopback and not value.get("certificate"):
        raise ValueError("LAN listeners require TLS; plaintext is only allowed behind a loopback HTTPS proxy")
    for name in ("certificate", "private_key", "attachment_root"):
        if value.get(name) and not Path(value[name]).is_absolute():
            raise ValueError(f"{name} must be absolute")
    return value


def install_mobile_start_hook():
    """Called before plugin import so the shared bridge captures the staging root."""
    path = os.environ.get("LINGXI_MOBILE_CONFIG", "")
    if not path:
        return
    settings = load_mobile_settings(Path(path))
    password = os.environ.get("LINGXI_MOBILE_PASSWORD", "admin")
    if any(not ipaddress_or_local(urlsplit(o).hostname) for o in settings["origins"]) and password == "admin":
        raise ValueError("公网手机入口请通过 LINGXI_MOBILE_PASSWORD 设置专用密码")
    os.environ["AGENT_GATEWAY_ATTACHMENT_ROOT"] = settings["attachment_root"]
    from core.websocket_server import WebSocketServer
    original = WebSocketServer.start

    async def start(server):
        from aiohttp import web
        from plugins_func.functions import hermes_bridge as plugin
        from .mobile_channel import MobileChannel
        asr = server._asr
        if asr is None or not hasattr(asr, "speech_to_text"):
            raise ValueError("手机端需要已配置的 ASR")
        # Capture the raw provider before physical connections wrap it. Both
        # paths use the same global conversion guard, including timed-out work.
        original_asr = asr.speech_to_text
        selected = server.config["selected_module"]["TTS"]
        tts_config = server.config["TTS"][selected]
        if tts_config.get("type", selected) != "edge":
            raise ValueError("手机首版音频需要现有 EdgeTTS 配置")
        from core.providers.tts.edge import TTSProvider
        tts = TTSProvider(tts_config, True)

        async def transcribe(pcm, session):
            from core.providers.asr.base import ASRProviderBase
            if asr.requires_file():
                raise ValueError("手机首版需要支持 PCM 内存输入的 ASR")
            artifacts = ASRProviderBase.AudioArtifacts([pcm], pcm, None, None)
            result, _ = await plugin._run_shared_asr_conversion(asr,
                lambda: original_asr([pcm], session, artifacts=artifacts),
                plugin._conversion_timeout_seconds("LINGXI_ASR_TIMEOUT_SECONDS", 10))
            if isinstance(result, dict):
                result = result.get("content", "")
            return result

        channel = MobileChannel(plugin._get_adapter(), transcribe=transcribe,
            synthesize=lambda text: tts.text_to_speak(text, None),
            attachment_root=Path(settings["attachment_root"]), password=password,
            origins=settings["origins"])
        runner = web.AppRunner(channel.app, access_log=None)
        context = None
        if settings.get("certificate"):
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(settings["certificate"], settings["private_key"])
        try:
            await runner.setup()
            await web.TCPSite(runner, settings["host"], settings["port"], ssl_context=context).start()
            # The companion/control coordinator must work without a physical device.
            control = plugin._get_control_runtime()
            if control:
                control.attach_running_loop(asyncio.get_running_loop())
            await original(server)
        finally:
            await runner.cleanup()

    WebSocketServer.start = start


def ipaddress_or_local(host):
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback
    except ValueError:
        return False
