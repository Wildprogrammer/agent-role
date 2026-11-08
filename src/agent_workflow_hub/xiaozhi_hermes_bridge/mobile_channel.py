"""Standalone phone channel. No ESP32 connection or device-control dependency.

Runs on the Xiaozhi event loop with its existing adapter and media providers.
Only terminal identity persists in the browser; media and captions are ephemeral.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from aiohttp import WSMsgType, web

from .attachments import AttachmentBinding, AttachmentError, AttachmentStore

ID = re.compile(r"^[a-f0-9]{32}$")
MAX_PCM = 16000 * 2 * 60
STATIC = Path(__file__).with_name("static")


def binding_snapshot(device: str, value: dict) -> AttachmentBinding:
    if value.get("status") != "ok" or value.get("bound") is not True:
        raise ValueError("请先选择智能体和会话。")
    return AttachmentBinding(device, value["host"], value["profile"],
                             value["stored_session_id"], value["binding_revision"])


def public_binding(value: dict) -> dict:
    return {key: value[key] for key in (
        "status", "bound", "host", "profile", "stored_session_id", "title", "binding_revision"
    ) if key in value}


@dataclass
class Phone:
    device: str
    socket: Any = None
    task: asyncio.Task | None = None
    dispatch_task: asyncio.Task | None = None
    stop_task: asyncio.Task | None = None
    turn: str = ""
    capture: bytearray | None = None
    snapshot: AttachmentBinding | None = None
    attachments: tuple[str, ...] = ()
    seen: OrderedDict = field(default_factory=OrderedDict)
    ack: asyncio.Event = field(default_factory=asyncio.Event)
    sequence: int = -1
    cancelling: bool = False
    capture_timer: asyncio.TimerHandle | None = None
    listed: set = field(default_factory=set)
    audio_enabled: bool = True
    muted_turn: str = ""
    tts_task: asyncio.Task | None = None

    @property
    def busy(self):
        return self.cancelling or self.capture is not None or (self.task is not None and not self.task.done())


class MobileChannel:
    def __init__(self, adapter, *, transcribe, synthesize, attachment_root: Path,
                 password: str, origins: list[str], secure_cookie: bool = True):
        if not password or not password.strip() or len(password) > 256:
            raise ValueError("手机密码不能为空或超过256字符")
        if not origins or any(urlsplit(o).scheme not in {"https", "http"} or
                              urlsplit(o).path or urlsplit(o).query or urlsplit(o).fragment
                              for o in origins):
            raise ValueError("必须配置精确的手机入口 origin")
        self.adapter, self.transcribe, self.synthesize = adapter, transcribe, synthesize
        self.store = AttachmentStore(attachment_root)
        self.password, self.origins, self.secure_cookie = password, set(origins), secure_cookie
        self.phones: dict[str, Phone] = {}
        self.logins: dict[str, tuple[str, str, float]] = {}
        self.attempts: list[float] = []
        self.app = web.Application(client_max_size=65536, middlewares=[self.guard])
        self.app.add_routes([
            web.get("/", self.index), web.get("/static/{name}", self.static),
            web.get("/favicon.ico", self.favicon),
            web.post("/api/login", self.login), web.get("/api/session", self.session),
            web.post("/api/logout", self.logout), web.post("/api/photo", self.photo),
            web.get("/api/socket", self.websocket),
        ])
        self.app.on_cleanup.append(self.cleanup)
        self.app.on_startup.append(self.startup)

    @web.middleware
    async def guard(self, request, handler):
        if request.host not in {urlsplit(o).netloc for o in self.origins}:
            raise web.HTTPForbidden(text="入口地址不匹配")
        if request.method == "POST" or request.path == "/api/socket":
            if request.headers.get("Origin") not in self.origins:
                raise web.HTTPForbidden(text="来源不匹配")
        if request.path.startswith("/api/") and request.path != "/api/login":
            token = request.cookies.get("lingxi_mobile", "")
            login = self.logins.get(token)
            if not login or login[2] <= time.monotonic():
                raise web.HTTPUnauthorized(text="请登录手机端")
            request["login"] = login
            if request.method == "POST" and not hmac.compare_digest(request.headers.get("X-CSRF", ""), login[1]):
                raise web.HTTPForbidden(text="登录已过期")
        try:
            response = await handler(request)
        except (ValueError, AttachmentError) as exc:
            response = web.json_response({"error": str(exc)}, status=getattr(exc, "status_code", 400))
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Permissions-Policy": "camera=(self), microphone=(self)",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'"})
        return response

    async def index(self, request):
        return web.FileResponse(STATIC / "index-mobile.html")

    async def favicon(self, request):
        return web.Response(status=204)

    async def static(self, request):
        name = request.match_info["name"]
        if name not in {"mobile.js", "mobile.css", "mobile-recorder.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(STATIC / name)

    async def login(self, request):
        now = time.monotonic()
        self.attempts = [t for t in self.attempts if now - t < 60]
        if len(self.attempts) >= 20:
            raise web.HTTPTooManyRequests()
        self.attempts.append(now)
        data = await request.json()
        if not isinstance(data, dict) or not isinstance(data.get("password"), str) or not hmac.compare_digest(data["password"].encode(), self.password.encode()):
            raise web.HTTPUnauthorized()
        terminal = data.get("terminal", "")
        if not isinstance(terminal, str) or not ID.fullmatch(terminal):
            raise ValueError("终端标识无效")
        device = "mobile-" + terminal
        if device not in self.phones and len(self.phones) >= 16:
            raise web.HTTPServiceUnavailable(text="本次运行的手机数量已达上限")
        self.phones.setdefault(device, Phone(device))
        self.logins = {k: v for k, v in self.logins.items() if v[2] > now and v[0] != device}
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.logins[token] = (device, csrf, now + 8 * 3600)
        response = web.json_response({"csrf": csrf, "device": device})
        response.set_cookie("lingxi_mobile", token, httponly=True, secure=self.secure_cookie, samesite="Strict", max_age=28800)
        return response

    async def session(self, request):
        device, csrf, _ = request["login"]
        return web.json_response({"device": device, "csrf": csrf})

    async def logout(self, request):
        phone = self.phones[request["login"][0]]
        self.logins.pop(request.cookies.get("lingxi_mobile"), None)
        await self.stop(phone)
        if phone.socket:
            await phone.socket.close()
        response = web.json_response({"status": "ok"})
        response.del_cookie("lingxi_mobile")
        return response

    async def photo(self, request):
        phone = self.phones[request["login"][0]]
        if phone.busy:
            raise ValueError("请结束当前回合后再添加照片")
        mime = request.headers.get("X-File-Type", "")
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("只支持 JPEG、PNG、WebP 照片")
        snapshot = binding_snapshot(phone.device, await self.adapter.show_binding(phone.device, device_id=phone.device))
        grant = await self.store.prepare(request_id=secrets.token_hex(16), binding=snapshot)

        async def receive():
            try:
                chunk = await request.content.read(65536)
            except ConnectionResetError:
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}

        record = await self.store.upload(token=grant.token, name=unquote(request.headers.get("X-File-Name", "")),
            declared_media_type=mime, receive=receive, content_length=request.content_length,
            timeout_seconds=600, idle_timeout_seconds=60)
        return web.json_response({"attachment_id": record.attachment_id})

    async def emit(self, phone, kind, *, turn=None, **data):
        if turn is not None and turn != phone.turn:
            return
        if phone.socket is not None and not phone.socket.closed:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await phone.socket.send_json({"type": kind, "turn": turn or "", **data})

    async def websocket(self, request):
        phone = self.phones[request["login"][0]]
        if phone.socket is not None and not phone.socket.closed:
            raise web.HTTPConflict(text="此手机已有页面在线，请先关闭旧页面")
        socket = web.WebSocketResponse(heartbeat=20, max_msg_size=65536)
        await socket.prepare(request)
        phone.socket = socket
        try:
            await self.emit(phone, "ready", device=phone.device, busy=phone.busy,
                            audio_preference_supported=True, audio_enabled=phone.audio_enabled)
            async for msg in socket:
                if request["login"][2] <= time.monotonic() or self.logins.get(request.cookies.get("lingxi_mobile")) != request["login"]:
                    break
                try:
                    if msg.type == WSMsgType.BINARY:
                        if phone.capture is None:
                            raise ValueError("未开始收音")
                        if len(msg.data) % 2 or len(phone.capture) + len(msg.data) > MAX_PCM:
                            await self.stop(phone)
                            raise ValueError("单次录音最长60秒")
                        phone.capture.extend(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        data = json.loads(msg.data)
                        if not isinstance(data, dict):
                            raise ValueError("请求格式无效")
                        await self.command(phone, data)
                    elif msg.type in {WSMsgType.ERROR, WSMsgType.CLOSE}:
                        break
                except (ValueError, AttachmentError) as exc:
                    await self.emit(phone, "error", message=str(exc))
                except Exception:
                    await self.emit(phone, "error", message="电脑端通道操作失败，请检查服务状态。")
        finally:
            await self.stop(phone)
            if phone.socket is socket:
                phone.socket = None
            await socket.close()
        return socket

    async def command(self, phone, data):
        kind = data.get("type")
        if kind == "audio_preference":
            enabled = data.get("enabled")
            if type(enabled) is not bool:
                raise ValueError("语音开关必须为布尔值")
            phone.audio_enabled = enabled
            if not enabled:
                phone.muted_turn = phone.turn
                phone.ack.set()
                if phone.tts_task and not phone.tts_task.done():
                    phone.tts_task.cancel()
            await self.emit(phone, "audio_preference", enabled=enabled)
            return
        if kind == "stop":
            await self.stop(phone)
            return
        if kind == "played":
            if data.get("turn") == phone.turn and data.get("sequence") == phone.sequence:
                phone.ack.set()
            return
        if kind == "binding":
            value = await self.adapter.show_binding(phone.device, device_id=phone.device)
            await self.emit(phone, "binding", binding=public_binding(value))
            return
        if kind == "agents":
            value = await self.adapter.list_agents(phone.device)
            await self.emit(phone, "agents", agents=[{k: item[k] for k in ("host", "id", "name", "available") if k in item}
                for item in value.get("agents", [])])
            return
        if kind == "sessions":
            host, agent = data.get("host"), data.get("agent")
            value = await self.adapter.list_sessions(phone.device, host=host, profile=agent, limit=100)
            rows = [{k: item[k] for k in ("id", "title") if k in item} for item in value.get("sessions", [])]
            phone.listed = {(host, agent, item.get("id")) for item in rows if isinstance(item.get("id"), str)}
            await self.emit(phone, "sessions", host=host, agent=agent, sessions=rows)
            return
        if kind == "bind":
            if phone.busy:
                raise ValueError("请先停止当前回合再切换会话")
            target = (data.get("host"), data.get("agent"), data.get("session_id"))
            if target not in phone.listed:
                raise ValueError("请重新列出并选择已有会话")
            current = await self.adapter.show_binding(phone.device, device_id=phone.device)
            value = await self.adapter.bind_session(phone.device, device_id=phone.device,
                host=target[0], profile=target[1], stored_session_id=target[2],
                expected_binding_revision=current.get("binding_revision", 0))
            if value.get("status") != "ok":
                raise ValueError("绑定失败，请刷新会话列表")
            await self.command(phone, {"type": "binding"})
            return
        if kind == "capture_end":
            if phone.capture is None or data.get("turn") != phone.turn:
                raise ValueError("收音回合已失效")
            pcm, phone.capture = bytes(phone.capture), None
            if phone.capture_timer:
                phone.capture_timer.cancel()
            phone.task = asyncio.create_task(self.run_turn(phone, phone.turn, "", pcm))
            return
        if kind not in {"capture", "submit"}:
            raise ValueError("不支持的手机操作")
        if phone.busy:
            raise ValueError("当前回合尚未结束，请先停止")
        turn = data.get("turn", "")
        if not isinstance(turn, str) or not ID.fullmatch(turn):
            raise ValueError("请求编号无效")
        if turn in phone.seen:
            raise ValueError("该请求已处理或状态未知，请在智能体端核对，不能重复发送")
        text = data.get("text", "")
        if not isinstance(text, str) or len(text) > 8000 or (kind == "submit" and not text.strip()):
            raise ValueError("请输入1至8000字符的问题")
        ids = data.get("attachments", [])
        if not isinstance(ids, list) or len(ids) > 4 or any(not isinstance(i, str) or not re.fullmatch(r"att_[a-f0-9]{32}", i) for i in ids):
            raise ValueError("照片编号无效")
        snapshot = binding_snapshot(phone.device, await self.adapter.show_binding(phone.device, device_id=phone.device))
        if ids:
            await self.store.resolve(ids, binding=snapshot, request_id=turn)
        phone.turn, phone.snapshot, phone.attachments = turn, snapshot, tuple(ids)
        phone.seen[turn] = True
        while len(phone.seen) > 256:
            phone.seen.popitem(last=False)
        if kind == "capture":
            phone.capture = bytearray()
            async def expire_capture():
                if phone.turn == turn and phone.capture is not None:
                    await self.stop(phone)
            phone.capture_timer = asyncio.get_running_loop().call_later(65, lambda: asyncio.create_task(expire_capture()))
            await self.emit(phone, "capture_ready", turn=turn)
        else:
            phone.task = asyncio.create_task(self.run_turn(phone, turn, text.strip(), None))

    async def run_turn(self, phone, turn, text, pcm):
        snapshot, ids = phone.snapshot, phone.attachments
        try:
            if pcm is not None:
                await self.emit(phone, "state", turn=turn, state="transcribing")
                if len(pcm) < 3200:
                    raise ValueError("录音太短，请再说一次")
                text = await self.transcribe(pcm, phone.device)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("没有识别到语音，请重试")
            if phone.turn != turn:
                return
            await self.emit(phone, "question", turn=turn, text=text)
            command = text.strip().rstrip("。！？.!?")
            if command in {"/new", "/compress", "开启新会话", "新建会话", "压缩上下文", "压缩当前会话", "清除上下文", "清空上下文", "重置上下文"}:
                raise ValueError("手机首版请在电脑端执行新建或压缩，再在手机选择会话；本次未作为普通问题发送。")
            await self.emit(phone, "state", turn=turn, state="thinking")
            records = await self.store.resolve(ids, binding=snapshot, request_id=turn) if ids else ()
            if phone.turn != turn:
                return

            async def progress(value):
                await self.emit(phone, "answer", turn=turn, text=value, final=False)

            async def dispatch():
                if phone.turn != turn:
                    return {"status": "cancelled"}
                return await asyncio.wait_for(self.adapter.dispatch_bound(phone.device, device_id=phone.device, prompt=text,
                    attachments=tuple(r.to_bridge_mapping() for r in records), expected_binding=snapshot, on_progress=progress), timeout=660)
            phone.dispatch_task = asyncio.create_task(dispatch())
            result = await asyncio.shield(phone.dispatch_task)
            if phone.turn != turn:
                return
            if result.get("status") != "completed":
                await self.emit(phone, "error", turn=turn, message="智能体返回：" + str(result.get("status", "failed")) + "。请在电脑端核对，勿自动重发。")
                return
            answer = result.get("output", "")
            if not isinstance(answer, str):
                raise ValueError("智能体未返回文字")
            await self.emit(phone, "answer", turn=turn, text=answer, final=True)
            if ids:
                await self.store.remove(ids)
            # Keep full captions. Bound speech separately, without hiding omissions.
            spoken = answer[:4000]
            if phone.audio_enabled and phone.muted_turn != turn and len(answer) > len(spoken):
                await self.emit(phone, "notice", turn=turn, message="长回答仅朗读前4000字，完整文字已显示。")
            for index, part in enumerate(speech_parts(spoken)):
                if not phone.audio_enabled or phone.muted_turn == turn:
                    break
                await self.emit(phone, "state", turn=turn, state="synthesizing")
                if not phone.audio_enabled or phone.muted_turn == turn:
                    break
                try:
                    phone.tts_task = asyncio.create_task(self.synthesize(part))
                    audio = await asyncio.wait_for(phone.tts_task, timeout=20)
                    if not audio:
                        raise ValueError("empty TTS")
                except asyncio.CancelledError:
                    if phone.turn == turn and phone.muted_turn == turn:
                        break
                    raise
                except Exception:
                    await self.emit(phone, "notice", turn=turn, message="语音合成暂不可用，文字回答已保留。")
                    break
                finally:
                    phone.tts_task = None
                if phone.turn != turn:
                    return
                if not phone.audio_enabled or phone.muted_turn == turn:
                    break
                phone.sequence, phone.ack = index, asyncio.Event()
                await self.emit(phone, "audio", turn=turn, sequence=index)
                if not phone.audio_enabled or phone.muted_turn == turn:
                    break
                # Frame includes immutable request id: old audio can never play in a new turn.
                await phone.socket.send_bytes(turn.encode("ascii") + audio)
                await asyncio.wait_for(phone.ack.wait(), timeout=120)
            await self.emit(phone, "done", turn=turn)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await self.emit(phone, "error", turn=turn, message="语音处理或播放超时，已退出；请查看文字结果。")
        except (ValueError, AttachmentError) as exc:
            await self.emit(phone, "error", turn=turn, message=str(exc))
        except Exception:
            await self.emit(phone, "error", turn=turn, message="处理未完成；如已提交，请到电脑端核对，勿重复发送。")
        finally:
            await self.emit(phone, "idle", turn=turn)

    async def stop(self, phone):
        if phone.cancelling:
            return
        phone.cancelling = True
        phone.turn = ""
        phone.capture = None
        if phone.capture_timer:
            phone.capture_timer.cancel()
            phone.capture_timer = None
        await self.emit(phone, "stopping")
        task, dispatch = phone.task, phone.dispatch_task
        try:
            # Cancel the host BEFORE cancelling the data task. Cancelling the
            # adapter first would change its state to outcome_unknown and make
            # the exact in-flight cancellation impossible.
            if dispatch is not None and not dispatch.done():
                result = await asyncio.wait_for(self.adapter.cancel_active_for_device(phone.device, device_id=phone.device), 12)
                await self.emit(phone, "notice", message="播放已停止；智能体取消状态：" + str(result.get("status", "unknown")))
        except Exception:
            await self.emit(phone, "notice", message="播放已停止；智能体取消结果未知，请在电脑端核对。")

        async def finish_stop():
            # Keep this phone busy until its original submission settles. No
            # previous cancel is allowed to race a new submission on this phone.
            if dispatch is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.shield(dispatch)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            phone.task = None
            phone.dispatch_task = None
            phone.cancelling = False
            await self.emit(phone, "stopped")
        if dispatch is not None and not dispatch.done():
            await self.emit(phone, "notice", message="播放已停止，仍在等待原回合结束；暂不接受新的发送。")
            phone.stop_task = asyncio.create_task(finish_stop())
        else:
            await finish_stop()

    async def startup(self, app):
        async def prune():
            while True:
                await asyncio.sleep(60)
                await self.store.cleanup_expired()
        self.prune_task = asyncio.create_task(prune())

    async def cleanup(self, app):
        self.prune_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.prune_task
        for phone in self.phones.values():
            await self.stop(phone)
            if phone.socket:
                await phone.socket.close()
            for task in (phone.stop_task, phone.task, phone.dispatch_task):
                if task and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task


def speech_parts(text: str):
    """Small independent MP3 segments, allowing bounded synthesis and playback."""
    while text:
        part = text[:180]
        end = max(part.rfind(p) for p in "。！？\n.!?") + 1
        if end < 20:
            end = len(part)
        yield text[:end]
        text = text[end:]
