"""Thin xiaozhi-esp32-server plugin for the Agent Workflow Hub bridge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
import weakref
from pathlib import Path
from threading import Lock
from time import monotonic

from agent_workflow_hub.xiaozhi_hermes_bridge.control_runtime import (
    control_runtime_from_environment,
)
from agent_workflow_hub.xiaozhi_hermes_bridge.xiaozhi_server_adapter import (
    XiaozhiAdapterError,
    adapter_from_environment,
)
from plugins_func.register import Action, ActionResponse, ToolType, register_function


_DEFAULT_PROFILES = [
    "default",
    "specialist",
    "research-agent",
]
_DEFAULT_DSH_PRESETS = [
    "standard",
    "assistant-agent",
    "conversation-agent",
]
_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _configured_agent_ids():
    """Load public tool enums from the reviewed bridge configuration.

    Unit tests and standalone schema inspection use small synthetic defaults. A
    managed server always provides ``XIAOZHI_HERMES_BRIDGE_CONFIG`` and its
    reviewed SHA-256, so production enums follow configuration rather than
    source-code allowlists.
    """

    config_path = os.environ.get("XIAOZHI_HERMES_BRIDGE_CONFIG", "").strip()
    if not config_path:
        return list(_DEFAULT_PROFILES), list(_DEFAULT_DSH_PRESETS)

    raw = Path(config_path).read_bytes()
    expected_digest = os.environ.get(
        "XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256", ""
    ).strip().lower()
    actual_digest = hashlib.sha256(raw).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise RuntimeError("reviewed bridge configuration SHA-256 is missing")
    if actual_digest != expected_digest:
        raise RuntimeError("bridge configuration changed after review")

    value = json.loads(raw.decode("utf-8"))
    profile_rows = value.get("profiles", [])
    if not isinstance(profile_rows, list) or any(
        not isinstance(row, dict) for row in profile_rows
    ):
        raise RuntimeError("bridge configuration contains an invalid profile")
    profiles = [row.get("name") for row in profile_rows]
    deepseek = value.get("deepseek_harness") or {}
    if not isinstance(deepseek, dict):
        raise RuntimeError("bridge configuration contains invalid DeepSeek settings")
    presets = deepseek.get("allowed_presets", [])
    for label, rows in (("profile", profiles), ("preset", presets)):
        if not isinstance(rows, list) or any(
            not isinstance(row, str) or not _AGENT_ID_RE.fullmatch(row)
            for row in rows
        ):
            raise RuntimeError(f"bridge configuration contains an invalid {label}")
    if not profiles:
        raise RuntimeError("bridge configuration exposes no Hermes profiles")
    return list(dict.fromkeys(profiles)), list(dict.fromkeys(presets))


_PROFILES, _DSH_PRESETS = _configured_agent_ids()
_AGENT_IDS = sorted(set(_PROFILES + _DSH_PRESETS))
_HOSTS = ["hermes", "deepseek-harness"]
_DEVICE_RESPONSE_LIMIT = 200
_DEVICE_DETAIL_NOTICE = "完整内容已保留在电脑端当前会话。"
_MARKDOWN_LINK_RE = re.compile(r"!?\[([^\]]*)\]\((?:<[^>]*>|[^)]*)\)")
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\s，。；;、<>|]+"
)
_URL_RE = re.compile(r"https?://[^\s，。；;、<>]+", re.IGNORECASE)
_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_PRIORITY_PARAGRAPH_RE = re.compile(r"^(?:结论|结果|建议|下一步|摘要)\s*[：:]?")
_adapter = None
_control_runtime = None
_control_runtime_initialized = False
_control_runtime_lock = Lock()
_asr_conversion_lock = Lock()
_asr_conversion_task = None
_PENDING_SWITCH_TTL_SECONDS = 180
_MAX_PENDING_SWITCHES = 32
_switch_states = {}
_switch_generations = {}
_switch_states_lock = Lock()


class _StreamingVoiceProjector:
    """Turn cumulative host text into non-repeating, bounded speech chunks."""

    def __init__(self):
        self.spoken = ""
        self.queued = False
        self.saw_progress = False

    def feed(self, text, *, final=False):
        self.saw_progress = True
        target = _device_response_projection(text)
        if not final:
            target = target.removesuffix(_DEVICE_DETAIL_NOTICE).rstrip()
        if self.spoken and not target.startswith(self.spoken):
            if final and _DEVICE_DETAIL_NOTICE not in self.spoken:
                return [self._record(_DEVICE_DETAIL_NOTICE)]
            return []
        remainder = target[len(self.spoken) :]
        if not remainder:
            return []
        if final:
            return [self._record(remainder)]
        boundary = max(remainder.rfind(mark) for mark in "。！？!?；;")
        if boundary < 0:
            comma = max(remainder.rfind(mark) for mark in "，,")
            if comma >= 11:
                boundary = comma
        if boundary < 0:
            return []
        return [self._record(remainder[: boundary + 1])]

    def _record(self, chunk):
        clean = chunk.strip()
        if clean:
            self.spoken += chunk
            self.queued = True
        return clean


def _queue_streaming_speech(conn, text, *, sentence_id):
    if not text:
        return
    from core.providers.tts.dto.dto import ContentType

    conn.tts.tts_one_sentence(
        conn,
        ContentType.TEXT,
        content_detail=text,
        sentence_id=sentence_id,
    )


def _begin_streaming_voice_turn(conn):
    generation = getattr(conn, "_lingxi_streaming_tts_generation", 0)
    generation = generation + 1 if isinstance(generation, int) else 1
    cancel_event = asyncio.Event()
    sentence_id = getattr(conn, "sentence_id", None)
    if not isinstance(sentence_id, str) or not sentence_id:
        sentence_id = uuid.uuid4().hex
        conn.sentence_id = sentence_id
    conn._lingxi_streaming_tts_generation = generation
    conn._lingxi_streaming_tts_cancel_event = cancel_event
    return generation, cancel_event, sentence_id


def _streaming_voice_turn_active(conn, generation, cancel_event, sentence_id):
    return (
        not cancel_event.is_set()
        and getattr(conn, "_lingxi_streaming_tts_generation", None) == generation
        and getattr(conn, "sentence_id", None) == sentence_id
    )


def _invalidate_streaming_voice_turn(conn):
    cancel_event = getattr(conn, "_lingxi_streaming_tts_cancel_event", None)
    if isinstance(cancel_event, asyncio.Event):
        cancel_event.set()
    generation = getattr(conn, "_lingxi_streaming_tts_generation", 0)
    conn._lingxi_streaming_tts_generation = (
        generation + 1 if isinstance(generation, int) else 1
    )


def _conversion_timeout_seconds(name, default):
    raw = os.environ.get(name, str(default)).strip()
    try:
        seconds = int(raw)
    except ValueError:
        return default
    return seconds if 3 <= seconds <= 60 else default


def _invalidate_live_detection(conn):
    conn._lingxi_wake_capture_turn = ""
    conn._lingxi_live_detect_pending = False
    conn._lingxi_live_detect_turn_id = ""


async def _expire_voice_pipeline(conn, *, operation):
    """Fail closed when ASR/TTS conversion cannot finish in bounded time."""

    if getattr(conn, "_lingxi_voice_pipeline_expiring", False):
        return
    conn._lingxi_voice_pipeline_expiring = True
    try:
        try:
            await _stop_current_speech(conn)
        except Exception:
            # State projection and connection shutdown must not depend on the
            # device accepting the best-effort TTS stop frame.
            _invalidate_streaming_voice_turn(conn)
            try:
                conn.clear_queues()
            except Exception:
                pass
            try:
                conn.clearSpeakStatus()
            except Exception:
                pass
        _set_live_capture(conn, False)
        _set_live_speech(conn, False)
        _publish_control(conn, "idle", operation=operation)
        close = getattr(conn, "close", None)
        if close is not None:
            await close()
    finally:
        conn._lingxi_voice_pipeline_expiring = False


def _schedule_voice_pipeline_expiry(conn, *, operation):
    loop = getattr(conn, "loop", None)
    if loop is None or loop.is_closed():
        return None
    existing = getattr(conn, "_lingxi_voice_pipeline_expiry_future", None)
    if existing is not None and not existing.done():
        return existing
    coroutine = _expire_voice_pipeline(conn, operation=operation)
    try:
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    except Exception:
        coroutine.close()
        return None
    conn._lingxi_voice_pipeline_expiry_future = future

    def consume_expiry(completed):
        try:
            completed.exception()
        except Exception:
            pass
        if getattr(conn, "_lingxi_voice_pipeline_expiry_future", None) is completed:
            conn._lingxi_voice_pipeline_expiry_future = None

    future.add_done_callback(consume_expiry)
    return future


def _edge_tts_provider(tts):
    provider_module = type(tts).__module__.lower()
    return provider_module == "core.providers.tts.edge" or provider_module.endswith(
        ".tts.edge"
    )


async def _run_shared_asr_conversion(asr, convert, timeout):
    """Share the existing non-cancellable ASR worker guard across all channels."""
    global _asr_conversion_task
    with _asr_conversion_lock:
        active = _asr_conversion_task
        if active is not None and not active.done():
            raise RuntimeError("asr_busy")
        task = asyncio.create_task(convert())
        _asr_conversion_task = task
        asr._lingxi_asr_conversion_task = task

    def release_conversion(completed):
        global _asr_conversion_task
        try:
            completed.exception()
        except (asyncio.CancelledError, Exception):
            pass
        with _asr_conversion_lock:
            if _asr_conversion_task is completed:
                _asr_conversion_task = None
        if getattr(asr, "_lingxi_asr_conversion_task", None) is completed:
            asr._lingxi_asr_conversion_task = None

    task.add_done_callback(release_conversion)
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()
    raise asyncio.TimeoutError("asr_timeout")


def _ensure_bounded_voice_converters(conn):
    """Bound local ASR/TTS conversion without modifying pinned upstream files."""

    asr = getattr(conn, "asr", None)
    original_speech_to_text = getattr(asr, "speech_to_text", None)
    if (
        callable(original_speech_to_text)
        and not getattr(asr, "_lingxi_bounded_conversion", False)
    ):
        timeout = _conversion_timeout_seconds("LINGXI_ASR_TIMEOUT_SECONDS", 10)

        async def bounded_speech_to_text(*args, **kwargs):
            try:
                return await _run_shared_asr_conversion(asr,
                    lambda: original_speech_to_text(*args, **kwargs), timeout)
            except asyncio.TimeoutError:
                await _expire_voice_pipeline(conn, operation="asr_timeout")
                return "", None
            except RuntimeError as exc:
                if str(exc) != "asr_busy":
                    raise
                await _expire_voice_pipeline(conn, operation="asr_busy")
                return "", None

        asr.speech_to_text = bounded_speech_to_text
        asr._lingxi_bounded_conversion = True

    tts = getattr(conn, "tts", None)
    original_text_to_speak = getattr(tts, "text_to_speak", None)
    if (
        callable(original_text_to_speak)
        and _edge_tts_provider(tts)
        and not getattr(tts, "_lingxi_bounded_conversion", False)
    ):
        timeout = _conversion_timeout_seconds("LINGXI_TTS_TIMEOUT_SECONDS", 15)

        async def bounded_text_to_speak(text, output_file):
            if getattr(conn, "_lingxi_tts_conversion_expired", False):
                raise RuntimeError("Lingxi TTS conversion already expired")
            try:
                return await asyncio.wait_for(
                    original_text_to_speak(text, output_file), timeout=timeout
                )
            except asyncio.TimeoutError:
                conn._lingxi_tts_conversion_expired = True
                if output_file:
                    try:
                        os.remove(output_file)
                    except OSError:
                        pass
                expiry = _schedule_voice_pipeline_expiry(
                    conn, operation="tts_timeout"
                )
                if expiry is None:
                    # If the owner loop is already unavailable, still fail
                    # closed locally so stale UI/control state is not retained.
                    _invalidate_streaming_voice_turn(conn)
                    _invalidate_live_detection(conn)
                    try:
                        conn.clear_queues()
                    except Exception:
                        pass
                    try:
                        conn.clearSpeakStatus()
                    except Exception:
                        pass
                    _set_live_capture(conn, False)
                    _set_live_speech(conn, False)
                    _publish_control(conn, "idle", operation="tts_timeout")
                raise RuntimeError("Lingxi TTS conversion timed out") from None

        tts.text_to_speak = bounded_text_to_speak
        tts._lingxi_bounded_conversion = True


def _apply_voice_session_policy(conn):
    """Keep a short post-answer follow-up window and never synthesize a farewell."""

    raw = os.environ.get("LINGXI_FOLLOWUP_WINDOW_SECONDS", "12").strip()
    try:
        seconds = int(raw)
    except ValueError:
        seconds = 12
    if not 5 <= seconds <= 120:
        seconds = 12
    config = getattr(conn, "config", None)
    if isinstance(config, dict):
        config["close_connection_no_voice_time"] = seconds
        config["end_prompt"] = {"enable": False}
    _ensure_bounded_voice_converters(conn)


def _control_error_label(exc):
    label = type(exc).__name__
    code = getattr(exc, "code", "")
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        return f"{label}:{code}"
    return label


def _get_adapter():
    global _adapter
    if _adapter is None:
        _adapter = adapter_from_environment()
    return _adapter


def _get_control_runtime():
    global _control_runtime, _control_runtime_initialized
    if _control_runtime_initialized:
        return _control_runtime
    with _control_runtime_lock:
        if not _control_runtime_initialized:
            _control_runtime = control_runtime_from_environment(
                _get_adapter(),
                switch_in_progress=lambda device_id: _active_switch(device_id)
                is not None,
            )
            _control_runtime_initialized = True
    return _control_runtime


def _json_result(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _is_markdown_table_line(line):
    stripped = line.strip()
    return bool(
        _TABLE_SEPARATOR_RE.fullmatch(stripped)
        or (stripped.count("|") >= 2 and not stripped.startswith("http"))
    )


def _clean_device_line(line):
    cleaned = _MARKDOWN_LINK_RE.sub(lambda match: match.group(1), line)
    cleaned = _WINDOWS_PATH_RE.sub("本机路径", cleaned)
    cleaned = _URL_RE.sub("链接", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"^\s*(?:#{1,6}|>|[-*+]\s+|\d+[.)]\s+)", "", cleaned)
    cleaned = re.sub(r"[*_~]", "", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _complete_prefix(text, limit):
    clean = text.strip()
    if not clean or limit <= 0:
        return ""
    if len(clean) <= limit:
        return clean if clean[-1] in "。！？!?" else clean + "。"
    window = clean[:limit]
    boundary = max(window.rfind(mark) for mark in "。！？!?")
    return window[: boundary + 1].strip() if boundary >= 0 else ""


def _join_device_paragraphs(parts):
    joined = ""
    for part in parts:
        if joined and joined[-1] not in "。！？!?":
            joined += "。"
        joined += part
    return joined.strip()


def _device_response_projection(output):
    """Build a bounded device-only view while preserving the bridge result verbatim."""

    original = output.strip() if isinstance(output, str) else ""
    if not original:
        return "Agent 任务已完成。"

    had_table = False
    had_code = False
    had_path = bool(_WINDOWS_PATH_RE.search(original))
    evidence_count = 0
    evidence_chars = 0
    in_code = False
    paragraphs = []
    current = []

    def flush_current():
        if current:
            paragraphs.append(" ".join(current))
            current.clear()

    for raw_line in original.splitlines():
        stripped = raw_line.strip()
        if "```" in stripped:
            had_code = True
            flush_current()
            if stripped.count("```") < 2:
                in_code = not in_code
            continue
        if in_code:
            continue
        if _is_markdown_table_line(stripped):
            had_table = True
            flush_current()
            continue
        if not stripped:
            flush_current()
            continue
        if re.match(r"^(?:>|[-*+]\s+|\d+[.)]\s+)", stripped):
            evidence_count += 1
            evidence_chars += len(stripped)
        cleaned = _clean_device_line(stripped)
        if cleaned:
            current.append(cleaned)
    flush_current()

    clean_text = _join_device_paragraphs(part for part in paragraphs if part)
    sentence_count = len(re.findall(r"[。！？!?]", clean_text))
    long_evidence = evidence_count > 3 or evidence_chars > 120
    needs_projection = (
        had_table
        or had_code
        or had_path
        or long_evidence
        or len(clean_text) > _DEVICE_RESPONSE_LIMIT
        or sentence_count > 5
    )
    if clean_text and not needs_projection:
        return clean_text

    priority = next(
        (part for part in paragraphs if _PRIORITY_PARAGRAPH_RE.match(part)), None
    )
    if priority is not None:
        summary_source = priority
    else:
        summary_source = _join_device_paragraphs(paragraphs[:2])

    suffix_parts = []
    if had_table:
        suffix_parts.append("答案包含表格。")
    if had_path:
        suffix_parts.append("涉及的本机路径请在电脑端查看。")
    suffix_parts.append(_DEVICE_DETAIL_NOTICE)
    suffix = "".join(suffix_parts)
    summary_limit = _DEVICE_RESPONSE_LIMIT - len(suffix)
    summary = _complete_prefix(summary_source, summary_limit)
    if not summary:
        summary = "Agent 任务已完成。"
    projected = summary + suffix
    if len(projected) > _DEVICE_RESPONSE_LIMIT:
        projected = "Agent 任务已完成。" + suffix
    return projected


def _failure(exc):
    return ActionResponse(
        action=Action.ERROR,
        response=f"Agent 桥接拒绝了请求：{exc}",
    )


def _stable_device_id(conn):
    value = getattr(conn, "device_id", None)
    if not isinstance(value, str) or not value.strip():
        raise XiaozhiAdapterError("Xiaozhi connection has no stable device_id")
    device_id = value.strip()
    _touch_control_connection(conn, device_id)
    return device_id


def _connection_alive(conn_ref):
    conn = conn_ref()
    if conn is None:
        return False
    websocket = getattr(conn, "websocket", None)
    if websocket is None:
        return False
    if getattr(websocket, "closed", False):
        return False
    state = getattr(websocket, "state", None)
    return getattr(state, "name", "") != "CLOSED"


def _device_tool_names(conn):
    handler = getattr(conn, "func_handler", None)
    manager = getattr(handler, "tool_manager", None)
    if manager is None:
        return ()
    return tuple(sorted(set(manager.get_supported_tool_names())))


def _mapping_result(result):
    if getattr(result, "action", None) in {Action.ERROR, Action.NOTFOUND}:
        raise XiaozhiAdapterError("Lingxi device rejected the diagnostic request")
    raw = getattr(result, "result", None)
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise XiaozhiAdapterError("Lingxi device returned an invalid diagnostic response")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise XiaozhiAdapterError("Lingxi device returned invalid diagnostic JSON") from None
    if not isinstance(value, dict):
        raise XiaozhiAdapterError("Lingxi device diagnostic response must be an object")
    return value


async def _inspect_device(conn_ref, operation):
    conn = conn_ref()
    if conn is None:
        raise XiaozhiAdapterError("Lingxi device disconnected before diagnostics")
    handler = getattr(conn, "func_handler", None)
    manager = getattr(handler, "tool_manager", None)
    if manager is None:
        raise XiaozhiAdapterError("Lingxi device tools are not ready")
    tools = {
        "device_status": "self_get_device_status",
        "audio_diagnostics": "self_audio_get_diagnostics",
    }
    tool_name = tools.get(operation)
    if tool_name is None or tool_name not in set(manager.get_supported_tool_names()):
        raise XiaozhiAdapterError("Lingxi diagnostic capability is unavailable")
    return _mapping_result(await manager.execute_tool(tool_name, {}))


async def _set_device_volume(conn_ref, volume):
    conn = conn_ref()
    if conn is None:
        raise XiaozhiAdapterError("Lingxi device disconnected before volume control")
    handler = getattr(conn, "func_handler", None)
    manager = getattr(handler, "tool_manager", None)
    if manager is None:
        raise XiaozhiAdapterError("Lingxi device tools are not ready")
    candidates = (
        "self_audio_speaker_set_volume_verified",
        "self_audio_speaker_set_volume",
    )
    available = set(manager.get_supported_tool_names())
    tool_name = next((name for name in candidates if name in available), None)
    if tool_name is None:
        raise XiaozhiAdapterError("Lingxi device does not expose absolute volume control")
    result = await manager.execute_tool(tool_name, {"volume": volume})
    if getattr(result, "action", None) in {Action.ERROR, Action.NOTFOUND}:
        raise XiaozhiAdapterError("Lingxi device rejected volume control")
    return {"status": "ok", "volume": volume}


def _volume_available(conn):
    handler = getattr(conn, "func_handler", None)
    manager = getattr(handler, "tool_manager", None)
    if manager is None:
        return False
    available = set(manager.get_supported_tool_names())
    return bool(
        {
            "self_audio_speaker_set_volume_verified",
            "self_audio_speaker_set_volume",
        }
        & available
    )


async def _register_control_connection(conn, device_id):
    _apply_voice_session_policy(conn)
    runtime = _get_control_runtime()
    if runtime is None:
        return
    loop = asyncio.get_running_loop()
    runtime.attach_running_loop(loop)
    generation = getattr(conn, "_lingxi_connection_generation", None)
    if not isinstance(generation, str) or not generation:
        generation = uuid.uuid4().hex
        conn._lingxi_connection_generation = generation
    conn_ref = weakref.ref(conn)

    async def stop():
        live = conn_ref()
        if live is None:
            raise XiaozhiAdapterError("Lingxi device disconnected before stop")
        await _stop_current_speech(live)
        # Xiaozhi v0.9.7 has no device-side stop ACK.  Do not claim silence.
        return {"status": "ok", "interrupt_state": "requested"}

    async def set_volume(volume):
        return await _set_device_volume(conn_ref, volume)

    async def inspect_device(operation):
        return await _inspect_device(conn_ref, operation)

    tool_names = _device_tool_names(conn)
    await runtime.register_connection(
        device_id=device_id,
        session_id=conn.session_id,
        connection_generation=generation,
        stop=stop,
        set_volume=set_volume if _volume_available(conn) else None,
        inspect=inspect_device,
        tool_names=tool_names,
        alive=lambda: _connection_alive(conn_ref),
    )
    conn._lingxi_control_registered = True


async def _unregister_control_connection(conn):
    runtime = _get_control_runtime()
    device_id = getattr(conn, "device_id", "")
    generation = getattr(conn, "_lingxi_connection_generation", "")
    session_id = getattr(conn, "session_id", "")
    if (
        runtime is None
        or not isinstance(device_id, str)
        or not isinstance(generation, str)
        or not isinstance(session_id, str)
        or not device_id
        or not generation
        or not session_id
    ):
        return
    await runtime.unregister_connection(
        device_id=device_id,
        session_id=session_id,
        connection_generation=generation,
    )


async def _close_with_control_cleanup(conn, original_close, ws=None):
    _invalidate_live_detection(conn)
    unregister_cancelled = None
    try:
        await _unregister_control_connection(conn)
    except asyncio.CancelledError as exc:
        unregister_cancelled = exc
    except Exception as exc:
        logger = getattr(conn, "logger", None)
        if logger is not None:
            logger.bind(tag=__name__).warning(
                "Lingxi control unregister failed: "
                f"{_control_error_label(exc)}"
            )
    result = await original_close(conn, ws)
    if unregister_cancelled is not None:
        raise unregister_cancelled
    return result


def _touch_control_connection(conn, device_id):
    try:
        runtime = _get_control_runtime()
        if runtime is None:
            return
        loop = asyncio.get_running_loop()
        runtime.attach_running_loop(loop)
        loop.create_task(_register_control_connection(conn, device_id))
    except Exception as exc:
        logger = getattr(conn, "logger", None)
        if logger is not None:
            logger.bind(tag=__name__).error(
                f"Lingxi control surface unavailable: {_control_error_label(exc)}"
            )


def _live_identity(conn):
    runtime = _get_control_runtime()
    device_id = getattr(conn, "device_id", "")
    generation = getattr(conn, "_lingxi_connection_generation", "")
    if (
        runtime is None
        or not isinstance(device_id, str)
        or not device_id.strip()
        or not isinstance(generation, str)
        or not generation
    ):
        return None
    return runtime, device_id.strip(), generation


def _ensure_live_identity(conn):
    runtime = _get_control_runtime()
    device_id = getattr(conn, "device_id", "")
    if runtime is None or not isinstance(device_id, str) or not device_id.strip():
        return None
    generation = getattr(conn, "_lingxi_connection_generation", "")
    if not isinstance(generation, str) or not generation:
        generation = uuid.uuid4().hex
        conn._lingxi_connection_generation = generation
    device_id = device_id.strip()
    runtime.register_live_projection_connection(
        device_id=device_id,
        connection_generation=generation,
    )
    return runtime, device_id, generation


def _start_live_turn(conn, *, capture_active):
    _invalidate_live_detection(conn)
    try:
        identity = _ensure_live_identity(conn)
        if identity is None:
            return ""
        runtime, device_id, generation = identity
        turn_id = runtime.start_live_turn(
            device_id=device_id,
            connection_generation=generation,
            capture_active=bool(capture_active),
        )
        if isinstance(turn_id, str) and turn_id:
            conn._lingxi_live_turn_id = turn_id
            return turn_id
    except Exception:
        pass
    return ""


def _current_live_turn(conn, *, create=False):
    turn_id = getattr(conn, "_lingxi_live_turn_id", "")
    if isinstance(turn_id, str) and turn_id:
        return turn_id
    return _start_live_turn(conn, capture_active=False) if create else ""


def _set_live_capture(conn, active):
    try:
        identity = _live_identity(conn)
        if identity is None:
            return
        runtime, device_id, generation = identity
        runtime.set_live_capture(
            device_id=device_id,
            connection_generation=generation,
            active=bool(active),
        )
    except Exception:
        return


def _begin_live_detection(conn, text):
    """Publish a wake detection immediately and reuse it for the following start."""

    turn_id = _start_live_turn(conn, capture_active=False)
    if not turn_id:
        return ""
    conn._lingxi_live_detect_pending = True
    conn._lingxi_live_detect_turn_id = turn_id
    _publish_live_question(conn, text, replace=True)
    return turn_id


def _begin_live_capture(conn):
    pending_turn = getattr(conn, "_lingxi_live_detect_turn_id", "")
    pending = getattr(conn, "_lingxi_live_detect_pending", False)
    current_turn = _current_live_turn(conn, create=False)
    if current_turn and current_turn == getattr(conn, "_lingxi_wake_capture_turn", ""):
        return current_turn
    _invalidate_live_detection(conn)
    if pending and pending_turn and pending_turn == current_turn:
        conn._lingxi_wake_capture_turn = current_turn
        _set_live_capture(conn, True)
        return current_turn
    return _start_live_turn(conn, capture_active=True)


def _end_live_capture(conn):
    _invalidate_live_detection(conn)
    _set_live_capture(conn, False)


def _caption_question_text(text):
    if not isinstance(text, str):
        return ""
    value = text.strip()
    if value.startswith("{") and value.endswith("}"):
        try:
            payload = json.loads(value)
            content = payload.get("content") if isinstance(payload, dict) else None
            if isinstance(content, str):
                value = content.strip()
        except (TypeError, ValueError):
            pass
    return value


def _caption_answer_text(text):
    if not isinstance(text, str):
        return ""
    return text.strip()


def _publish_live_question(conn, text, *, replace=True):
    try:
        turn_id = _current_live_turn(conn, create=True)
        identity = _live_identity(conn)
        value = _caption_question_text(text)
        if identity is None or not turn_id or not value:
            return
        conn._lingxi_wake_capture_turn = ""
        runtime, device_id, generation = identity
        runtime.publish_live_question(
            device_id=device_id,
            connection_generation=generation,
            turn_id=turn_id,
            text=value,
            replace=bool(replace),
        )
    except Exception:
        return


def _publish_live_answer(conn, text, *, source="agent", turn_id=""):
    try:
        selected_turn = turn_id if isinstance(turn_id, str) and turn_id else _current_live_turn(
            conn, create=True
        )
        identity = _live_identity(conn)
        value = _caption_answer_text(text)
        if identity is None or not selected_turn or not value:
            return
        _invalidate_live_detection(conn)
        runtime, device_id, generation = identity
        runtime.publish_live_answer(
            device_id=device_id,
            connection_generation=generation,
            turn_id=selected_turn,
            text=value,
            source=source,
        )
    except Exception:
        return


def _append_live_spoken_answer(conn, text, *, turn_id=""):
    try:
        selected_turn = turn_id if isinstance(turn_id, str) and turn_id else _current_live_turn(
            conn, create=True
        )
        identity = _live_identity(conn)
        value = _caption_answer_text(text)
        if identity is None or not selected_turn or not value:
            return
        _invalidate_live_detection(conn)
        runtime, device_id, generation = identity
        runtime.append_live_spoken_answer(
            device_id=device_id,
            connection_generation=generation,
            turn_id=selected_turn,
            text=value,
        )
    except Exception:
        return


def _set_live_speech(conn, active):
    if active:
        _invalidate_live_detection(conn)
    try:
        identity = _live_identity(conn)
        if identity is None:
            return
        runtime, device_id, generation = identity
        runtime.set_live_speech(
            device_id=device_id,
            connection_generation=generation,
            active=bool(active),
        )
    except Exception:
        return


_SENSITIVE_LOG_MARKERS = (
    "收到listen消息",
    "收到未知类型消息",
    "解析到错误的消息",
    "识别文本",
    "识别到文本",
    "大模型收到用户消息",
    "发送第一段语音",
    "发送音频消息",
    "收到设备呼叫指令",
    "播放唤醒词回复",
    "识别到明确的退出命令",
    "加入TTS上报队列失败",
    "加入ASR上报队列失败",
    "User prompt",
    "使用快速提示词",
    "已注入动态上下文数据",
    "TTS请求成功",
    "句子语音生成开始",
    "句子语音生成成功",
    "语音生成成功",
)


def _redact_log_message(message):
    if not isinstance(message, str):
        return message
    if not any(marker in message for marker in _SENSITIVE_LOG_MARKERS):
        return message
    label = message.split(":", 1)[0].split("：", 1)[0].strip()
    return f"{label}: [content redacted; chars={len(message)}]"


class _PrivacyLogger:
    def __init__(self, wrapped):
        self._wrapped = wrapped

    def bind(self, *args, **kwargs):
        return _PrivacyLogger(self._wrapped.bind(*args, **kwargs))

    def opt(self, *args, **kwargs):
        return _PrivacyLogger(self._wrapped.opt(*args, **kwargs))

    def patch(self, *args, **kwargs):
        return _PrivacyLogger(self._wrapped.patch(*args, **kwargs))

    def _emit(self, level, message, *args, **kwargs):
        return getattr(self._wrapped, level)(
            _redact_log_message(message), *args, **kwargs
        )

    def trace(self, message, *args, **kwargs):
        return self._emit("trace", message, *args, **kwargs)

    def debug(self, message, *args, **kwargs):
        return self._emit("debug", message, *args, **kwargs)

    def info(self, message, *args, **kwargs):
        return self._emit("info", message, *args, **kwargs)

    def success(self, message, *args, **kwargs):
        return self._emit("success", message, *args, **kwargs)

    def warning(self, message, *args, **kwargs):
        return self._emit("warning", message, *args, **kwargs)

    def error(self, message, *args, **kwargs):
        return self._emit("error", message, *args, **kwargs)

    def critical(self, message, *args, **kwargs):
        return self._emit("critical", message, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


def _privacy_logger(value):
    return value if isinstance(value, _PrivacyLogger) else _PrivacyLogger(value)


def _privacy_logger_factory(factory):
    def create(*args, **kwargs):
        return _privacy_logger(factory(*args, **kwargs))

    return create


def _refresh_privacy_loggers(conn=None):
    if conn is not None:
        logger = getattr(conn, "logger", None)
        if logger is not None:
            conn.logger = _privacy_logger(logger)
    for name, module in tuple(sys.modules.items()):
        if not name.startswith(
            ("core.providers.asr", "core.providers.tts", "core.providers.intent")
        ):
            continue
        logger = getattr(module, "logger", None)
        if logger is not None:
            module.logger = _privacy_logger(logger)


def _install_privacy_logging_hooks(connection_handler):
    try:
        import config.logger as logger_config

        if not getattr(logger_config, "_lingxi_privacy_hook", False):
            original_setup = logger_config.setup_logging

            def setup_logging(*args, **kwargs):
                return _privacy_logger(original_setup(*args, **kwargs))

            logger_config.setup_logging = setup_logging
            logger_config._lingxi_privacy_hook = True

        connection_module = sys.modules.get(connection_handler.__module__)
        if connection_module is not None and not getattr(
            connection_module, "_lingxi_privacy_factory_hook", False
        ):
            for factory_name in ("setup_logging", "create_connection_logger"):
                factory = getattr(connection_module, factory_name, None)
                if callable(factory):
                    setattr(
                        connection_module,
                        factory_name,
                        _privacy_logger_factory(factory),
                    )
            connection_module._lingxi_privacy_factory_hook = True

        _refresh_privacy_loggers()

        if not getattr(connection_handler, "_lingxi_privacy_setattr_hook", False):
            original_setattr = connection_handler.__setattr__

            def set_attribute(self, name, value):
                if name == "logger" and value is not None:
                    value = _privacy_logger(value)
                return original_setattr(self, name, value)

            connection_handler.__setattr__ = set_attribute
            connection_handler._lingxi_privacy_setattr_hook = True

        if not getattr(connection_handler, "_lingxi_privacy_logger_hook", False):
            original_init = connection_handler.__init__

            def init(self, *args, **kwargs):
                original_init(self, *args, **kwargs)
                logger = getattr(self, "logger", None)
                if logger is not None:
                    self.logger = _privacy_logger(logger)

            connection_handler.__init__ = init
            connection_handler._lingxi_privacy_logger_hook = True
    except Exception:
        return


def _install_control_lifecycle_hooks():
    """Attach to MCP-ready and disconnect without changing pinned server files."""

    try:
        from core.connection import ConnectionHandler
        from core.handle import helloHandle as hello_module
        from core.handle import intentHandler as intent_module
        from core.handle import receiveAudioHandle as receive_audio_module
        from core.handle import sendAudioHandle as send_audio_module
        from core.handle.textHandler import listenMessageHandler as listen_module
        from core.handle.textHandler.mcpMessageHandler import McpTextMessageHandler
        from core.providers.tools.device_mcp.mcp_handler import handle_mcp_message
    except ImportError:
        return

    _install_privacy_logging_hooks(ConnectionHandler)

    connection_module = sys.modules.get(ConnectionHandler.__module__)
    if connection_module is not None and not getattr(
        connection_module, "_lingxi_text_entry_hook", False
    ):
        original_handle_text = connection_module.handleTextMessage

        async def handle_text_message(conn, message):
            # ConnectionHandler imports handleTextMessage by value before function
            # plugins load.  This is the earliest stable per-connection entrypoint:
            # redact before TextMessageProcessor logs the raw JSON and establish the
            # transient turn before its listen handler starts downstream work.
            _refresh_privacy_loggers(conn)
            _apply_voice_session_policy(conn)
            payload = None
            if isinstance(message, str):
                try:
                    payload = json.loads(message)
                except (TypeError, ValueError):
                    payload = None
            if isinstance(payload, dict) and payload.get("type") == "listen":
                state = payload.get("state")
                if state == "start":
                    _begin_live_capture(conn)
                elif state == "detect":
                    _begin_live_detection(conn, payload.get("text"))
                elif state == "stop":
                    _end_live_capture(conn)
            return await original_handle_text(conn, message)

        connection_module.handleTextMessage = handle_text_message
        connection_module._lingxi_text_entry_hook = True

    if not getattr(send_audio_module, "_lingxi_live_hook", False):
        original_send_stt = send_audio_module.send_stt_message
        original_send_tts = send_audio_module.send_tts_message

        async def send_stt_message(conn, text):
            _publish_live_question(conn, text, replace=True)
            return await original_send_stt(conn, text)

        async def send_tts_message(conn, state, text=None):
            sentence_id = getattr(conn, "sentence_id", None)
            result = await original_send_tts(conn, state, text)
            pinned_turn = getattr(conn, "_lingxi_live_tts_turn_id", "")
            current_turn = _current_live_turn(conn, create=False)
            projection_allowed = not pinned_turn or pinned_turn == current_turn
            if state == "sentence_start":
                if projection_allowed:
                    _append_live_spoken_answer(conn, text, turn_id=pinned_turn)
                    _set_live_speech(conn, True)
            elif state == "stop" and getattr(conn, "sentence_id", None) == sentence_id:
                if projection_allowed:
                    _set_live_speech(conn, False)
                conn._lingxi_live_tts_turn_id = ""
            return result

        send_audio_module.send_stt_message = send_stt_message
        send_audio_module.send_tts_message = send_tts_message
        for module in (receive_audio_module, intent_module, listen_module):
            if getattr(module, "send_stt_message", None) is original_send_stt:
                module.send_stt_message = send_stt_message
        for module in (hello_module, listen_module):
            if getattr(module, "send_tts_message", None) is original_send_tts:
                module.send_tts_message = send_tts_message
        send_audio_module._lingxi_live_hook = True

    if not getattr(McpTextMessageHandler, "_lingxi_lifecycle_hook", False):

        async def handle(self, conn, msg_json):
            payload = msg_json.get("payload")
            if payload is None:
                return

            async def process():
                await handle_mcp_message(conn, conn.mcp_client, payload)
                if not isinstance(payload, dict) or int(payload.get("id", 0)) != 2:
                    return
                result = payload.get("result")
                if not isinstance(result, dict) or result.get("nextCursor"):
                    return
                if not await conn.mcp_client.is_ready():
                    return
                device_id = getattr(conn, "device_id", "")
                if isinstance(device_id, str) and device_id.strip():
                    await _register_control_connection(conn, device_id.strip())

            asyncio.create_task(process())

        McpTextMessageHandler.handle = handle
        McpTextMessageHandler._lingxi_lifecycle_hook = True

    if not getattr(ConnectionHandler, "_lingxi_lifecycle_hook", False):
        original_close = ConnectionHandler.close
        original_clear_speak = ConnectionHandler.clearSpeakStatus

        def clear_speak_status(self):
            try:
                return original_clear_speak(self)
            finally:
                _set_live_speech(self, False)

        async def close(self, ws=None):
            return await _close_with_control_cleanup(self, original_close, ws)

        ConnectionHandler.close = close
        ConnectionHandler.clearSpeakStatus = clear_speak_status
        ConnectionHandler._lingxi_lifecycle_hook = True


def _publish_control(conn, state, **fields):
    try:
        runtime = _get_control_runtime()
        device_id = getattr(conn, "device_id", "")
        if runtime is not None and isinstance(device_id, str) and device_id.strip():
            runtime.publish(state=state, device_id=device_id.strip(), **fields)
    except Exception:
        return


_LIST_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_list_profiles",
        "description": "列出当前小智会话允许使用的 Hermes Profile 及活动 Profile。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_list_profiles", _LIST_DESC, ToolType.SYSTEM_CTL)
async def hermes_list_profiles(conn):
    try:
        result = await _get_adapter().list_profiles(conn.session_id)
        profiles = result.get("profiles", [])
        active = result.get("active_profile", "未知")
        profile_text = "、".join(profiles) if isinstance(profiles, list) else "未知"
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=f"可用 Hermes Profile：{profile_text}。当前 Profile：{active}。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_SWITCH_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_switch_profile",
        "description": "将当前小智会话切换到指定 Hermes Profile。切换不会执行任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "enum": _PROFILES,
                    "description": "要切换到的 Hermes Profile。",
                }
            },
            "required": ["profile"],
        },
    },
}


@register_function("hermes_switch_profile", _SWITCH_DESC, ToolType.SYSTEM_CTL)
async def hermes_switch_profile(conn, profile):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().switch_profile(conn.session_id, profile)
        active = result.get("active_profile", profile)
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=f"已切换到 {active} Profile。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_LIST_SESSIONS_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_list_sessions",
        "description": "列出指定 Hermes Profile 中可绑定的最近已有对话，不创建新对话。",
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "enum": _PROFILES},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["profile"],
        },
    },
}


@register_function("hermes_list_sessions", _LIST_SESSIONS_DESC, ToolType.SYSTEM_CTL)
async def hermes_list_sessions(conn, profile, limit=5):
    try:
        result = await _get_adapter().list_sessions(
            conn.session_id, profile=profile, limit=limit
        )
        sessions = result.get("sessions", [])
        if not isinstance(sessions, list) or not sessions:
            summary = f"{profile} 没有可绑定的已有对话。"
        else:
            lines = []
            for index, item in enumerate(sessions, 1):
                if isinstance(item, dict):
                    lines.append(
                        f"{index}. {item.get('title') or '未命名对话'}，ID {item.get('id')}"
                    )
            summary = f"{profile} 最近对话：" + "；".join(lines)
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=summary,
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_BIND_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_bind_session",
        "description": "把当前稳定设备显式绑定到一条已存在的 Hermes 对话。",
        "parameters": {
            "type": "object",
            "properties": {
                "profile": {"type": "string", "enum": _PROFILES},
                "stored_session_id": {
                    "type": "string",
                    "description": "由 hermes_list_sessions 返回的精确对话 ID。",
                },
            },
            "required": ["profile", "stored_session_id"],
        },
    },
}


@register_function("hermes_bind_session", _BIND_DESC, ToolType.SYSTEM_CTL)
async def hermes_bind_session(conn, profile, stored_session_id):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().bind_session(
            conn.session_id,
            device_id=_stable_device_id(conn),
            profile=profile,
            stored_session_id=stored_session_id,
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=f"已绑定到 {profile} 的对话“{result.get('title') or stored_session_id}”。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_SHOW_BINDING_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_show_binding",
        "description": "查看当前稳定设备绑定的 Hermes 对话。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_show_binding", _SHOW_BINDING_DESC, ToolType.SYSTEM_CTL)
async def hermes_show_binding(conn):
    try:
        result = await _get_adapter().show_binding(
            conn.session_id, device_id=_stable_device_id(conn)
        )
        response = (
            f"当前绑定：{result.get('profile')}，{result.get('title') or result.get('stored_session_id')}。"
            if result.get("bound") is True
            else "当前设备还没有绑定 Hermes 对话。"
        )
        return ActionResponse(
            action=Action.RESPONSE, result=_json_result(result), response=response
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_UNBIND_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_unbind_session",
        "description": "解除当前稳定设备的 Hermes 对话绑定，不删除 Hermes 聊天记录。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_unbind_session", _UNBIND_DESC, ToolType.SYSTEM_CTL)
async def hermes_unbind_session(conn):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().unbind_session(
            conn.session_id, device_id=_stable_device_id(conn)
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response="已解除 Hermes 对话绑定。" if result.get("removed") else "当前设备没有 Hermes 对话绑定。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_PREPARE_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_prepare_dispatch",
        "description": (
            "准备把用户命令提交给当前 Hermes Profile，但暂不执行。"
            "必须先调用本工具并要求用户明确确认。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "准备交给当前 Hermes Profile 的完整命令。",
                }
            },
            "required": ["prompt"],
        },
    },
}


@register_function("hermes_prepare_dispatch", _PREPARE_DESC, ToolType.SYSTEM_CTL)
async def hermes_prepare_dispatch(conn, prompt):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().prepare_shared_dispatch(
            conn.session_id, device_id=_stable_device_id(conn), prompt=prompt
        )
        _publish_control(
            conn,
            "waiting_confirmation",
            operation="dispatch_shared",
            host=result.get("host", ""),
            agent=result.get("active_profile", ""),
            stored_session_id=result.get("stored_session_id", ""),
            binding_revision=result.get("binding_revision"),
            action_id=result.get("operation_id", ""),
        )
        profile = result.get("active_profile", "未知")
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=f"准备提交到 {profile}。请明确说“确认”后才会执行，或说“取消”。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_PREPARE_NEW_SESSION_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_prepare_new_session",
        "description": "准备在当前绑定的 Hermes Profile 中开启空白新会话，但暂不创建。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_prepare_new_session", _PREPARE_NEW_SESSION_DESC, ToolType.SYSTEM_CTL)
async def hermes_prepare_new_session(conn):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().prepare_new_session(
            conn.session_id, device_id=_stable_device_id(conn)
        )
        _publish_control(
            conn,
            "waiting_confirmation",
            operation="create_session",
            host=result.get("host", ""),
            agent=result.get("active_profile", ""),
            stored_session_id=result.get("stored_session_id", ""),
            binding_revision=result.get("binding_revision"),
            action_id=result.get("operation_id", ""),
        )
        profile = result.get("active_profile", "未知")
        title = result.get("stored_session_id", "当前会话")
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=(
                f"准备在 {profile} 开启空白新会话，并把当前设备从 {title} 切换过去；"
                "旧会话和历史会保留。请明确说“确认”后才会执行，或说“取消”。"
            ),
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_PREPARE_COMPRESS_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_prepare_compress_context",
        "description": "准备压缩当前设备绑定的 Hermes 会话上下文，但暂不执行。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_prepare_compress_context", _PREPARE_COMPRESS_DESC, ToolType.SYSTEM_CTL)
async def hermes_prepare_compress_context(conn):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().prepare_compress_context(
            conn.session_id, device_id=_stable_device_id(conn)
        )
        _publish_control(
            conn,
            "waiting_confirmation",
            operation="compress_session",
            host=result.get("host", ""),
            agent=result.get("active_profile", ""),
            stored_session_id=result.get("stored_session_id", ""),
            binding_revision=result.get("binding_revision"),
            action_id=result.get("operation_id", ""),
        )
        profile = result.get("active_profile", "未知")
        title = result.get("stored_session_id", "当前会话")
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=(
                f"准备压缩 {profile} 的当前会话 {title}；不会删除整段历史或新建会话。"
                "请明确说“确认”后才会执行，或说“取消”。"
            ),
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_CLARIFY_CLEAR_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_clarify_clear_context",
        "description": "澄清“清除上下文”的歧义，不执行任何 Hermes 会话写操作。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_clarify_clear_context", _CLARIFY_CLEAR_DESC, ToolType.SYSTEM_CTL)
async def hermes_clarify_clear_context(conn):
    try:
        await _get_adapter().cancel_dispatch(conn.session_id)
        result = {
            "schema_version": "1.0",
            "status": "clarification_required",
            "operation": "clarify_clear_context",
            "session_id": conn.session_id,
        }
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response="你是要压缩当前会话，还是开启一个空白新会话？",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_CONFIRM_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_confirm_dispatch",
        "description": (
            "仅当用户在上一轮准备 Hermes 操作后明确说确认时调用，执行该会话唯一待确认操作。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_confirm_dispatch", _CONFIRM_DESC, ToolType.SYSTEM_CTL)
async def hermes_confirm_dispatch(conn):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        _publish_control(conn, "thinking", operation="confirm_action")
        result = await _get_adapter().confirm_shared_dispatch(
            conn.session_id, device_id=_stable_device_id(conn)
        )
        _publish_control(
            conn,
            "speaking" if result.get("status") == "completed" else "success",
            operation=str(result.get("operation", "")),
        )
        return _agent_result_response(result)
    except XiaozhiAdapterError as exc:
        _publish_control(conn, "error", operation="confirm_action")
        return _failure(exc)


def _agent_response_text(result):
    status = result.get("status")
    operation = result.get("operation")
    if status == "outcome_unknown":
        response = "Agent 提交结果不确定，请在电脑端检查；不要重复提交。"
    elif status == "pending":
        response = "Agent 已开始处理，仍在后台运行；请不要重复提交。"
    elif status in {"conflict", "partial_failure", "reconciliation_required"}:
        response = (
            "Agent 操作可能已经完成，但设备绑定需要在电脑端核对；"
            "请不要重复执行。"
        )
    elif status == "waiting_for_approval":
        response = "Agent 已收到任务，但需要在电脑端审批工具操作。"
    elif status == "completed":
        if operation == "create_session":
            response = "已开启并绑定到新的 Agent 会话；旧会话和历史仍然保留。"
        elif operation == "compress_session":
            compression_status = result.get("compression_status")
            if compression_status == "compressed":
                response = "当前 Agent 会话上下文已压缩，可以继续对话。"
            else:
                response = "Agent 未压缩当前上下文；会话绑定没有改变。"
        elif operation in {"dispatch", "dispatch_shared", "dispatch_bound_voice"}:
            response = _device_response_projection(result.get("output"))
        else:
            response = "Agent 任务已完成。"
    elif status == "rejected":
        response = "Agent 拒绝了本次任务。"
    elif status == "failed":
        error = result.get("error")
        host = result.get("host")
        host_name = "DeepSeek Harness" if host == "deepseek-harness" else "Hermes"
        response = (
            f"{host_name} 任务失败：{error}"
            if isinstance(error, str) and error.strip()
            else f"{host_name} 任务失败，但没有返回具体原因。"
        )
    else:
        response = f"Agent 返回状态：{status or '未知'}。"
    return response


def _agent_result_response(result):
    return ActionResponse(
        action=Action.RESPONSE,
        result=_json_result(result),
        response=_agent_response_text(result),
    )


_CANCEL_DESC = {
    "type": "function",
    "function": {
        "name": "hermes_cancel_dispatch",
        "description": "取消当前小智会话中尚未执行的 Hermes 操作。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("hermes_cancel_dispatch", _CANCEL_DESC, ToolType.SYSTEM_CTL)
async def hermes_cancel_dispatch(conn):
    try:
        result = await _get_adapter().cancel_dispatch(conn.session_id)
        _publish_control(conn, "idle", operation="cancel_action")
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response="已取消待确认的 Hermes 命令。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


async def _stop_current_speech(conn):
    """Stop device playback without mutating its durable Agent binding."""

    _invalidate_streaming_voice_turn(conn)
    _invalidate_live_detection(conn)
    try:
        try:
            conn.close_after_chat = False
            conn.client_abort = True
        except Exception:
            pass
        try:
            conn.clear_queues()
        except Exception:
            pass
        try:
            websocket = getattr(conn, "websocket", None)
            if websocket is not None:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "tts",
                            "state": "stop",
                            "session_id": conn.session_id,
                        },
                        ensure_ascii=False,
                    )
                )
        except Exception:
            pass
    finally:
        try:
            conn.clearSpeakStatus()
        except Exception:
            pass
        _set_live_capture(conn, False)
        _set_live_speech(conn, False)
        _publish_control(conn, "idle", operation="stop_speaking")


async def _cancel_active_agent_turn(conn):
    try:
        cancel = getattr(_get_adapter(), "cancel_active_for_device", None)
        if cancel is None:
            return {"status": "ok", "cancelled": False}
        return await cancel(
            conn.session_id, device_id=_stable_device_id(conn)
        )
    except XiaozhiAdapterError as exc:
        return {
            "status": "requested",
            "cancelled": False,
            "error": _control_error_label(exc),
        }


_STOP_SPEAKING_DESC = {
    "type": "function",
    "function": {
        "name": "lingxi_stop_speaking",
        "description": "立即停止灵溪当前语音播报，但保留设备会话、绑定和待确认操作。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("lingxi_stop_speaking", _STOP_SPEAKING_DESC, ToolType.SYSTEM_CTL)
async def lingxi_stop_speaking(conn):
    try:
        await _stop_current_speech(conn)
        agent_cancel = await _cancel_active_agent_turn(conn)
        return ActionResponse(
            action=Action.NONE,
            result=_json_result(
                {
                    "status": "requested",
                    "playback_stop_requested": True,
                    "agent_cancel": agent_cancel,
                }
            ),
            response="",
        )
    except Exception as exc:
        return _failure(XiaozhiAdapterError(f"stop speaking failed: {exc}"))


_EXIT_CONVERSATION_DESC = {
    "type": "function",
    "function": {
        "name": "lingxi_exit_conversation",
        "description": (
            "立即停止播报并退出当前灵溪在线会话；保留持久 Agent 绑定和电脑端历史。"
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("lingxi_exit_conversation", _EXIT_CONVERSATION_DESC, ToolType.SYSTEM_CTL)
async def lingxi_exit_conversation(conn):
    device_id = _stable_device_id(conn)
    switch_cancelled, binding_done = _cancel_switch_for_exit(device_id)
    prepared_cancelled = 0
    cleanup_error = None
    operation_error = None
    cancelled_error = None
    try:
        if binding_done is not None:
            binding_outcome = await asyncio.shield(binding_done)
            if binding_outcome.get("status") != "cancelled_and_restored":
                cleanup_error = XiaozhiAdapterError(
                    "exit could not verify cancellation of the in-flight Agent switch"
                )
        if cleanup_error is None:
            try:
                cancelled = await _get_adapter().cancel_prepared_for_device(
                    device_id=device_id
                )
                prepared_cancelled = cancelled.get("cancelled_count", 0)
            except XiaozhiAdapterError as exc:
                cleanup_error = exc
            except Exception as exc:
                cleanup_error = XiaozhiAdapterError(
                    f"exit prepared cleanup failed: {_control_error_label(exc)}"
                )
        await _stop_current_speech(conn)
        await _cancel_active_agent_turn(conn)
    except asyncio.CancelledError as exc:
        cancelled_error = exc
    except Exception as exc:
        operation_error = XiaozhiAdapterError(
            f"exit conversation cleanup failed: {_control_error_label(exc)}"
        )
    finally:
        try:
            await conn.close()
        except Exception as exc:
            if operation_error is None:
                operation_error = XiaozhiAdapterError(
                    f"exit conversation close failed: {_control_error_label(exc)}"
                )
    if cancelled_error is not None:
        raise cancelled_error
    if operation_error is not None:
        return _failure(operation_error)
    if cleanup_error is not None:
        return _failure(cleanup_error)
    return ActionResponse(
        action=Action.NONE,
        result=_json_result(
            {
                "status": "exited",
                "switch_cancelled": switch_cancelled,
                "prepared_cancelled": prepared_cancelled,
            }
        ),
        response="",
    )


_REJECT_VOLUME_DESC = {
    "type": "function",
    "function": {
        "name": "lingxi_reject_volume",
        "description": "拒绝超出零到一百范围的灵溪音量设置。",
        "parameters": {
            "type": "object",
            "properties": {"volume": {"type": "integer"}},
            "required": ["volume"],
        },
    },
}


@register_function("lingxi_reject_volume", _REJECT_VOLUME_DESC, ToolType.SYSTEM_CTL)
async def lingxi_reject_volume(conn, volume):
    return ActionResponse(
        action=Action.RESPONSE,
        result=_json_result({"status": "invalid", "volume": volume}),
        response="音量只能设置为零到一百。",
    )


_PENDING_CONFIRMATION_DESC = {
    "type": "function",
    "function": {
        "name": "agent_continue_pending_confirmation",
        "description": (
            "在设备存在待确认的新会话或压缩操作时保持确认门，"
            "不执行音量、目录、切换或普通正文命令。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"choice": {"type": "string"}},
            "required": ["choice"],
        },
    },
}


@register_function(
    "agent_continue_pending_confirmation",
    _PENDING_CONFIRMATION_DESC,
    ToolType.SYSTEM_CTL,
)
async def agent_continue_pending_confirmation(conn, choice):
    try:
        state = await _get_adapter().pending_for_device(
            device_id=_stable_device_id(conn)
        )
        if state.get("pending") is not True:
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result({"status": "not_found"}),
                response="当前没有待确认的 Agent 操作，请重新说出你的请求。",
            )
        pending_state = state.get("pending_state")
        if pending_state != "prepared":
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result(
                    {"status": "operation_in_progress", "state": pending_state}
                ),
                response="Agent 操作正在处理中，请稍候，不要重复执行。",
            )
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(
                {
                    "status": "confirmation_required",
                    "operation": state.get("pending_operation"),
                    "ignored_choice": str(choice),
                }
            ),
            response=(
                "待确认操作仍未执行。请明确说“确认”后才会执行，"
                "或说“取消”；其他命令请在处理后再说。"
            ),
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_LIST_DESC = {
    "type": "function",
    "function": {
        "name": "agent_list_agents",
        "description": "按宿主列出小智设备可绑定的全部 Agent，包括 Hermes Profile 和 DeepSeek Harness Preset。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_list_agents", _AGENT_LIST_DESC, ToolType.SYSTEM_CTL)
async def agent_list_agents(conn):
    try:
        adapter = _get_adapter()
        result = await adapter.list_agents(conn.session_id)
        try:
            binding = await adapter.show_binding(
                conn.session_id, device_id=_stable_device_id(conn)
            )
        except XiaozhiAdapterError:
            binding = {"bound": None, "status": "unavailable"}
        result = dict(result)
        result["binding"] = binding
        current = None
        if binding.get("bound") is True:
            current = (binding.get("host"), binding.get("profile"))
        agents = result.get("agents", [])
        groups = {}
        unavailable = []
        if isinstance(agents, list):
            for item in agents:
                if isinstance(item, dict):
                    host = item.get("host")
                    agent_id = item.get("id")
                    if isinstance(host, str) and isinstance(agent_id, str):
                        labels = []
                        if item.get("available") is False:
                            labels.append("不可用")
                            unavailable.append(f"{host}/{agent_id}")
                        if current == (host, agent_id):
                            labels.append("当前绑定")
                        label = agent_id
                        if labels:
                            label += f"（{'、'.join(labels)}）"
                        groups.setdefault(host, []).append(label)
        summary = "；".join(
            f"{host}：{'、'.join(ids)}" for host, ids in groups.items()
        ) or "没有可用 Agent"
        if unavailable:
            summary += "；当前不可用：" + "、".join(unavailable)
        if current is not None:
            summary += f"；当前绑定：{current[0]}/{current[1]}"
        elif binding.get("status") == "unavailable":
            summary += "；当前绑定状态暂时不可读取"
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=f"可用 Agent：{summary}。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_SWITCH_DESC = {
    "type": "function",
    "function": {
        "name": "agent_switch_agent",
        "description": (
            "让当前设备先选择指定 Agent 的宿主，再显式选择一条已有对话。"
            "不自动选择最近会话，不创建新对话、不发送聊天正文。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "enum": _AGENT_IDS},
                "host": {"type": "string", "enum": _HOSTS},
            },
            "required": ["agent_id"],
        },
    },
}


def _hosts_for_agent(agent_id):
    hosts = []
    if agent_id in _PROFILES:
        hosts.append("hermes")
    if agent_id in _DSH_PRESETS:
        hosts.append("deepseek-harness")
    return hosts


def _copy_switch_state(value):
    copied = dict(value)
    if isinstance(copied.get("sessions"), list):
        copied["sessions"] = [dict(item) for item in copied["sessions"]]
    return copied


def _prune_expired_switches_locked(now):
    for device_id, state in list(_switch_states.items()):
        if state["stage"] != "binding" and now > state["deadline"]:
            _switch_states.pop(device_id, None)
            _switch_generations[device_id] = max(
                _switch_generations.get(device_id, 0), state["generation"] + 1
            )


def _active_switch(device_id):
    with _switch_states_lock:
        _prune_expired_switches_locked(monotonic())
        state = _switch_states.get(device_id)
        return _copy_switch_state(state) if state is not None else None


def _start_switch(device_id, owner_session_id, value):
    with _switch_states_lock:
        now = monotonic()
        _prune_expired_switches_locked(now)
        existing = _switch_states.get(device_id)
        if existing is not None and existing["stage"] == "binding":
            raise XiaozhiAdapterError(
                "Agent switch binding is already in progress; inspect the binding before retrying"
            )
        if existing is None and len(_switch_states) >= _MAX_PENDING_SWITCHES:
            raise XiaozhiAdapterError("pending Agent switch limit reached")
        generation = _switch_generations.get(device_id, 0) + 1
        _switch_generations[device_id] = generation
        state = {
            **value,
            "generation": generation,
            "deadline": now + _PENDING_SWITCH_TTL_SECONDS,
            "owner_session_id": owner_session_id,
        }
        _switch_states[device_id] = state
        return _copy_switch_state(state)


def _transition_switch(device_id, expected, *, from_stage, to_stage, updates=None):
    with _switch_states_lock:
        _prune_expired_switches_locked(monotonic())
        current = _switch_states.get(device_id)
        if (
            current is None
            or current.get("generation") != expected.get("generation")
            or current.get("stage") != from_stage
        ):
            return None
        next_state = {**current, **(updates or {}), "stage": to_stage}
        _switch_states[device_id] = next_state
        return _copy_switch_state(next_state)


def _finish_switch(device_id, generation):
    with _switch_states_lock:
        current = _switch_states.get(device_id)
        if current is None or current.get("generation") != generation:
            return False
        _switch_states.pop(device_id, None)
        return True


def _cancel_switch(device_id):
    with _switch_states_lock:
        _prune_expired_switches_locked(monotonic())
        current = _switch_states.get(device_id)
        if current is None:
            return False
        if current["stage"] == "binding":
            raise XiaozhiAdapterError(
                "Agent switch binding is in progress and cannot be cancelled"
            )
        _switch_states.pop(device_id, None)
        _switch_generations[device_id] = max(
            _switch_generations.get(device_id, 0), current["generation"] + 1
        )
        return True


def _cancel_switch_for_exit(device_id):
    """Cancel a pending switch or tombstone an in-flight bind for rollback."""

    with _switch_states_lock:
        _prune_expired_switches_locked(monotonic())
        current = _switch_states.get(device_id)
        if current is None:
            return False, None
        if current["stage"] == "binding":
            current["cancel_requested"] = True
            return True, current.get("binding_done")
        _switch_states.pop(device_id, None)
        _switch_generations[device_id] = max(
            _switch_generations.get(device_id, 0), current["generation"] + 1
        )
        return True, None


def _take_switch_binding_completion(device_id, generation):
    """Linearize bind completion against exit and return its cancellation state."""

    with _switch_states_lock:
        current = _switch_states.get(device_id)
        if (
            current is None
            or current.get("generation") != generation
            or current.get("stage") != "binding"
        ):
            return None
        _switch_states.pop(device_id, None)
        return bool(current.get("cancel_requested")), current.get("binding_done")


def _switch_binding_cancel_requested(device_id, generation):
    with _switch_states_lock:
        current = _switch_states.get(device_id)
        return bool(
            current is not None
            and current.get("generation") == generation
            and current.get("stage") == "binding"
            and current.get("cancel_requested")
        )


async def _restore_binding_after_cancel(adapter, conn, device_id, previous):
    if previous.get("bound") is True:
        restored = await adapter.bind_session(
            conn.session_id,
            device_id=device_id,
            host=previous["host"],
            profile=previous["profile"],
            stored_session_id=previous["stored_session_id"],
        )
    else:
        restored = await adapter.unbind_session(
            conn.session_id,
            device_id=device_id,
        )
    if restored.get("status") != "ok":
        raise XiaozhiAdapterError("could not restore the binding cancelled by exit")


def _switch_guard(conn):
    device_id = _stable_device_id(conn)
    state = _active_switch(device_id)
    if state is None:
        return None
    return ActionResponse(
        action=Action.RESPONSE,
        result=_json_result(
            {
                "status": "switch_in_progress",
                "stage": state["stage"],
                "agent_id": state["agent_id"],
            }
        ),
        response=(
            "Agent 切换尚未完成，当前正文没有提交。"
            "请完成宿主或会话选择，或说取消切换。"
        ),
    )


async def _switch_to_agent_host(
    conn, *, device_id, switch_state, host, agent_id, notice=""
):
    try:
        listed = await _get_adapter().list_sessions(
            conn.session_id, host=host, profile=agent_id, limit=5
        )
    except Exception:
        _finish_switch(device_id, switch_state["generation"])
        raise
    sessions = [
        item
        for item in listed.get("sessions", [])
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and item.get("id")
    ]
    if not sessions:
        if not _finish_switch(device_id, switch_state["generation"]):
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result({"status": "conflict"}),
                response="该切换请求已失效，未改变当前绑定。",
            )
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(
                {"status": "not_found", "host": host, "agent_id": agent_id}
            ),
            response=(
                f"{notice}{host} 的 {agent_id} 还没有可恢复的已有对话；"
                "未改变当前绑定。如需新建会话，请明确提出。"
            ),
        )
    if sessions:
        pending = _transition_switch(
            device_id,
            switch_state,
            from_stage="resolving",
            to_stage="session",
            updates={
                "stage": "session",
                "host": host,
                "agent_id": agent_id,
                "sessions": sessions,
            },
        )
        if pending is None:
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result({"status": "conflict"}),
                response="该切换请求已失效，未改变当前绑定。",
            )
        options = "；".join(
            f"第{index}个，{item.get('title') or '未命名对话'}"
            for index, item in enumerate(sessions, 1)
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(
                {
                    "status": "clarification_required",
                    "stage": "session",
                    "host": host,
                    "agent_id": agent_id,
                    "sessions": [
                        {"index": index, "id": item["id"], "title": item.get("title")}
                        for index, item in enumerate(sessions, 1)
                    ],
                }
            ),
            response=(
                f"{notice}切换尚未完成。请选择已有对话："
                f"{options}；或说取消切换。"
            ),
        )

@register_function("agent_switch_agent", _AGENT_SWITCH_DESC, ToolType.SYSTEM_CTL)
async def agent_switch_agent(conn, agent_id, host=None):
    try:
        device_id = _stable_device_id(conn)
        candidate_hosts = _hosts_for_agent(agent_id)
        if not candidate_hosts:
            raise XiaozhiAdapterError("unknown Agent")
        if host is not None and host not in candidate_hosts:
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result(
                    {
                        "status": "clarification_required",
                        "agent_id": agent_id,
                        "candidate_hosts": candidate_hosts,
                    }
                ),
                response=f"{host} 中没有 {agent_id}。",
            )

        cancelled = await _get_adapter().cancel_prepared_for_device(device_id=device_id)
        notice = (
            "已取消原待确认操作。"
            if cancelled.get("cancelled_count", 0) > 0
            else ""
        )

        target_host = host
        if target_host is None:
            if len(candidate_hosts) == 1:
                target_host = candidate_hosts[0]
            else:
                _start_switch(
                    device_id,
                    conn.session_id,
                    {
                        "stage": "host",
                        "agent_id": agent_id,
                        "candidate_hosts": candidate_hosts,
                    },
                )
                return ActionResponse(
                    action=Action.RESPONSE,
                    result=_json_result(
                        {
                            "status": "clarification_required",
                            "agent_id": agent_id,
                            "candidate_hosts": candidate_hosts,
                        }
                    ),
                    response=(
                        f"{notice}切换尚未完成。{agent_id} "
                        "同时存在于 Hermes 和 DeepSeek Harness；"
                        "请说第一项 Hermes，或第二项 DeepSeek；也可以说取消切换。"
                    ),
                )
        switch_state = _start_switch(
            device_id,
            conn.session_id,
            {
                "stage": "resolving",
                "host": target_host,
                "agent_id": agent_id,
            },
        )
        return await _switch_to_agent_host(
            conn,
            device_id=device_id,
            switch_state=switch_state,
            host=target_host,
            agent_id=agent_id,
            notice=notice,
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_CONTINUE_SWITCH_DESC = {
    "type": "function",
    "function": {
        "name": "agent_continue_switch",
        "description": "继续当前设备尚未完成的 Agent 宿主或会话选择。",
        "parameters": {
            "type": "object",
            "properties": {"choice": {"type": "string"}},
            "required": ["choice"],
        },
    },
}


@register_function("agent_continue_switch", _AGENT_CONTINUE_SWITCH_DESC, ToolType.SYSTEM_CTL)
async def agent_continue_switch(conn, choice):
    try:
        device_id = _stable_device_id(conn)
        pending = _active_switch(device_id)
        if pending is None:
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result({"status": "not_found"}),
                response="当前没有等待选择的 Agent 切换。",
            )
        compact = "".join(str(choice).lower().split()).strip("。！？!?，,")
        if pending["stage"] == "host":
            if compact in {
                "第二",
                "第二个",
                "第二项",
                "2",
            } or any(
                term in compact
                for term in ("deepseek", "deepseekharness", "deepfi", "深度求索", "dsh")
            ):
                host = "deepseek-harness"
            elif compact in {"第一", "第一个", "第一项", "1"} or any(
                term in compact for term in ("hermes", "herme")
            ):
                host = "hermes"
            else:
                return ActionResponse(
                    action=Action.RESPONSE,
                    result=_json_result({"status": "clarification_required"}),
                    response=(
                        "切换尚未完成。请说第一项 Hermes，或第二项 DeepSeek；"
                        "也可以说取消切换。"
                    ),
                )
            claimed = _transition_switch(
                device_id,
                pending,
                from_stage="host",
                to_stage="resolving",
                updates={"host": host, "owner_session_id": conn.session_id},
            )
            if claimed is None:
                return ActionResponse(
                    action=Action.RESPONSE,
                    result=_json_result({"status": "conflict"}),
                    response="该切换选择已被处理，请查看当前绑定后再试。",
                )
            return await _switch_to_agent_host(
                conn,
                device_id=device_id,
                switch_state=claimed,
                host=host,
                agent_id=pending["agent_id"],
            )

        if pending["stage"] in {"resolving", "binding"}:
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result({"status": "switch_in_progress"}),
                response="Agent 切换仍在处理中，请稍候并查看当前绑定。",
            )
        ordinals = {
            "第一个": 1,
            "第一": 1,
            "1": 1,
            "第二个": 2,
            "第二": 2,
            "2": 2,
            "第三个": 3,
            "第三": 3,
            "3": 3,
            "第四个": 4,
            "第四": 4,
            "4": 4,
            "第五个": 5,
            "第五": 5,
            "5": 5,
        }
        selected_index = ordinals.get(compact)
        sessions = pending["sessions"]
        if selected_index is None or selected_index > len(sessions):
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result({"status": "clarification_required"}),
                response=(
                    f"切换尚未完成。请说第一个到第{len(sessions)}个，"
                    "或说取消切换。"
                ),
            )
        binding_done = asyncio.get_running_loop().create_future()
        claimed = _transition_switch(
            device_id,
            pending,
            from_stage="session",
            to_stage="binding",
            updates={
                "owner_session_id": conn.session_id,
                "cancel_requested": False,
                "binding_done": binding_done,
            },
        )
        if claimed is None:
            return ActionResponse(
                action=Action.RESPONSE,
                result=_json_result({"status": "conflict"}),
                response="该切换选择已被处理，请查看当前绑定后再试。",
            )
        adapter = _get_adapter()
        try:
            previous_binding = await adapter.show_binding(
                conn.session_id, device_id=device_id
            )
        except Exception as exc:
            completion = _take_switch_binding_completion(
                device_id, claimed["generation"]
            )
            if completion is not None:
                cancel_requested, waiter = completion
                if waiter is not None and not waiter.done():
                    waiter.set_result(
                        {
                            "status": (
                                "outcome_unknown" if cancel_requested else "failed"
                            ),
                            "error": str(exc),
                        }
                    )
            raise
        claimed_with_binding = _transition_switch(
            device_id,
            claimed,
            from_stage="binding",
            to_stage="binding",
            updates={"previous_binding": previous_binding},
        )
        if claimed_with_binding is None:
            raise XiaozhiAdapterError("Agent switch binding state was lost")
        claimed = claimed_with_binding
        if _switch_binding_cancel_requested(device_id, claimed["generation"]):
            completion = _take_switch_binding_completion(
                device_id, claimed["generation"]
            )
            if completion is not None:
                _, waiter = completion
                if waiter is not None and not waiter.done():
                    waiter.set_result({"status": "cancelled_and_restored"})
            return ActionResponse(
                action=Action.NONE,
                result=_json_result({"status": "cancelled"}),
                response="",
            )
        selected = claimed["sessions"][selected_index - 1]
        try:
            result = await adapter.bind_session(
                conn.session_id,
                device_id=device_id,
                host=claimed["host"],
                profile=claimed["agent_id"],
                stored_session_id=selected["id"],
            )
        except Exception as exc:
            completion = _take_switch_binding_completion(
                device_id, claimed["generation"]
            )
            if completion is not None:
                cancel_requested, waiter = completion
                if waiter is not None and not waiter.done():
                    waiter.set_result(
                        {
                            "status": (
                                "outcome_unknown" if cancel_requested else "failed"
                            ),
                            "error": str(exc),
                        }
                    )
            raise
        completion = _take_switch_binding_completion(device_id, claimed["generation"])
        if completion is None:
            raise XiaozhiAdapterError("Agent switch completion state was lost")
        cancel_requested, waiter = completion
        if cancel_requested:
            try:
                await _restore_binding_after_cancel(
                    adapter, conn, device_id, claimed["previous_binding"]
                )
            except Exception as exc:
                if waiter is not None and not waiter.done():
                    waiter.set_result(
                        {"status": "rollback_failed", "error": str(exc)}
                    )
                raise
            if waiter is not None and not waiter.done():
                waiter.set_result({"status": "cancelled_and_restored"})
            return ActionResponse(
                action=Action.NONE,
                result=_json_result({"status": "cancelled"}),
                response="",
            )
        if waiter is not None and not waiter.done():
            waiter.set_result({"status": "completed"})
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=(
                f"已切换到 {claimed['host']} 的 {claimed['agent_id']}，继续对话“"
                f"{result.get('title') or selected.get('title') or selected['id']}”。"
            ),
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_CANCEL_SWITCH_DESC = {
    "type": "function",
    "function": {
        "name": "agent_cancel_switch",
        "description": "取消尚未完成的 Agent 热切换，保留原绑定。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_cancel_switch", _AGENT_CANCEL_SWITCH_DESC, ToolType.SYSTEM_CTL)
async def agent_cancel_switch(conn):
    try:
        device_id = _stable_device_id(conn)
        removed = _cancel_switch(device_id)
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result({"status": "cancelled", "removed": removed}),
            response="已取消 Agent 切换，原绑定保持不变。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_LIST_SESSIONS_DESC = {
    "type": "function",
    "function": {
        "name": "agent_list_sessions",
        "description": "列出指定宿主和 Agent 中可绑定的已有对话，不创建新对话。",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "enum": _HOSTS},
                "agent_id": {"type": "string", "enum": _AGENT_IDS},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10},
            },
            "required": ["host", "agent_id"],
        },
    },
}


@register_function("agent_list_sessions", _AGENT_LIST_SESSIONS_DESC, ToolType.SYSTEM_CTL)
async def agent_list_sessions(conn, host, agent_id, limit=5):
    try:
        result = await _get_adapter().list_sessions(
            conn.session_id, host=host, profile=agent_id, limit=limit
        )
        sessions = result.get("sessions", [])
        if not isinstance(sessions, list) or not sessions:
            summary = f"{host} 的 {agent_id} 没有可绑定的已有对话。"
        else:
            lines = [
                f"{index}. {item.get('title') or '未命名对话'}，ID {item.get('id')}"
                for index, item in enumerate(sessions, 1)
                if isinstance(item, dict)
            ]
            summary = f"{host} 的 {agent_id} 最近对话：" + "；".join(lines)
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=summary,
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_BIND_DESC = {
    "type": "function",
    "function": {
        "name": "agent_bind_session",
        "description": "把当前稳定设备绑定到指定宿主、Agent 和一条已存在的对话。",
        "parameters": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "enum": _HOSTS},
                "agent_id": {"type": "string", "enum": _AGENT_IDS},
                "stored_session_id": {
                    "type": "string",
                    "description": "由 agent_list_sessions 返回的精确对话 ID。",
                },
            },
            "required": ["host", "agent_id", "stored_session_id"],
        },
    },
}


@register_function("agent_bind_session", _AGENT_BIND_DESC, ToolType.SYSTEM_CTL)
async def agent_bind_session(conn, host, agent_id, stored_session_id):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().bind_session(
            conn.session_id,
            device_id=_stable_device_id(conn),
            host=host,
            profile=agent_id,
            stored_session_id=stored_session_id,
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response=(
                f"已绑定到 {host} 的 {agent_id} 对话“"
                f"{result.get('title') or stored_session_id}”。"
            ),
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_SHOW_DESC = {
    "type": "function",
    "function": {
        "name": "agent_show_binding",
        "description": "查看当前设备绑定的宿主、Agent 和对话。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_show_binding", _AGENT_SHOW_DESC, ToolType.SYSTEM_CTL)
async def agent_show_binding(conn):
    try:
        result = await _get_adapter().show_binding(
            conn.session_id, device_id=_stable_device_id(conn)
        )
        response = (
            f"当前绑定：{result.get('host')} 的 {result.get('profile')}，"
            f"{result.get('title') or result.get('stored_session_id')}。"
            if result.get("bound") is True
            else "当前设备还没有绑定 Agent 对话。"
        )
        return ActionResponse(
            action=Action.RESPONSE, result=_json_result(result), response=response
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_UNBIND_DESC = {
    "type": "function",
    "function": {
        "name": "agent_unbind_session",
        "description": "解除当前设备的 Agent 对话绑定，不删除任何宿主中的聊天记录。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_unbind_session", _AGENT_UNBIND_DESC, ToolType.SYSTEM_CTL)
async def agent_unbind_session(conn):
    try:
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        result = await _get_adapter().unbind_session(
            conn.session_id, device_id=_stable_device_id(conn)
        )
        return ActionResponse(
            action=Action.RESPONSE,
            result=_json_result(result),
            response="已解除 Agent 对话绑定。" if result.get("removed") else "当前设备没有 Agent 对话绑定。",
        )
    except XiaozhiAdapterError as exc:
        return _failure(exc)


_AGENT_PREPARE_DESC = {
    "type": "function",
    "function": {
        "name": "agent_prepare_dispatch",
        "description": "准备把完整命令提交给当前设备已绑定的 Agent 对话，但暂不执行。",
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
}


_AGENT_DIRECT_DISPATCH_DESC = {
    "type": "function",
    "function": {
        "name": "agent_dispatch_bound",
        "description": (
            "把普通语音正文直接提交给当前设备已绑定的 Agent 对话。"
            "不得用于新会话、压缩、清除上下文或其他生命周期管理操作。"
        ),
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string"}},
            "required": ["prompt"],
        },
    },
}


@register_function("agent_dispatch_bound", _AGENT_DIRECT_DISPATCH_DESC, ToolType.SYSTEM_CTL)
async def agent_dispatch_bound(conn, prompt):
    cancel_event = None
    try:
        _apply_voice_session_policy(conn)
        guard = _switch_guard(conn)
        if guard is not None:
            return guard
        _publish_live_question(conn, prompt, replace=False)
        live_turn_id = _current_live_turn(conn, create=False)
        projector = _StreamingVoiceProjector()
        streaming_enabled = hasattr(getattr(conn, "tts", None), "tts_one_sentence")
        generation, cancel_event, sentence_id = _begin_streaming_voice_turn(conn)

        async def on_progress(text):
            if not _streaming_voice_turn_active(
                conn, generation, cancel_event, sentence_id
            ):
                return
            _publish_live_answer(conn, text, source="agent", turn_id=live_turn_id)
            if not streaming_enabled:
                return
            conn._lingxi_live_tts_turn_id = live_turn_id
            for chunk in projector.feed(text):
                if not _streaming_voice_turn_active(
                    conn, generation, cancel_event, sentence_id
                ):
                    return
                _queue_streaming_speech(
                    conn, chunk, sentence_id=sentence_id
                )

        _publish_control(conn, "thinking", operation="dispatch_bound_voice")
        dispatch_args = {
            "device_id": _stable_device_id(conn),
            "prompt": prompt,
        }
        if streaming_enabled:
            dispatch_args["on_progress"] = on_progress
        result = await _get_adapter().dispatch_bound(conn.session_id, **dispatch_args)
        turn_active = _streaming_voice_turn_active(
            conn, generation, cancel_event, sentence_id
        )
        if result.get("status") == "completed" and isinstance(result.get("output"), str):
            if turn_active:
                _publish_live_answer(
                    conn, result.get("output"), source="agent", turn_id=live_turn_id
                )
            if turn_active and streaming_enabled and projector.saw_progress:
                for chunk in projector.feed(result.get("output"), final=True):
                    if not _streaming_voice_turn_active(
                        conn, generation, cancel_event, sentence_id
                    ):
                        break
                    _queue_streaming_speech(
                        conn, chunk, sentence_id=sentence_id
                    )
        elif turn_active:
            _publish_live_answer(
                conn, _agent_response_text(result), source="agent", turn_id=live_turn_id
            )
        if turn_active:
            conn._lingxi_live_tts_turn_id = live_turn_id
        _publish_control(
            conn,
            "speaking" if result.get("status") == "completed" else "success",
            operation="dispatch_bound_voice",
        )
        if result.get("status") in {"cancelled", "interrupted"}:
            return ActionResponse(
                action=Action.NONE,
                result=_json_result(result),
                response="",
            )
        if not turn_active:
            return ActionResponse(
                action=Action.NONE,
                result=_json_result(result),
                response="",
            )
        if projector.queued:
            return ActionResponse(
                action=Action.NONE,
                result=_json_result(result),
                response="",
            )
        return _agent_result_response(result)
    except XiaozhiAdapterError as exc:
        _publish_control(conn, "error", operation="dispatch_bound_voice")
        if isinstance(cancel_event, asyncio.Event) and cancel_event.is_set():
            return ActionResponse(action=Action.NONE, result="", response="")
        return _failure(exc)


@register_function("agent_prepare_dispatch", _AGENT_PREPARE_DESC, ToolType.SYSTEM_CTL)
async def agent_prepare_dispatch(conn, prompt):
    return await hermes_prepare_dispatch(conn, prompt)


_AGENT_PREPARE_NEW_DESC = {
    "type": "function",
    "function": {
        "name": "agent_prepare_new_session",
        "description": "准备在当前绑定的 Agent 中开启空白新会话，但暂不创建。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_prepare_new_session", _AGENT_PREPARE_NEW_DESC, ToolType.SYSTEM_CTL)
async def agent_prepare_new_session(conn):
    return await hermes_prepare_new_session(conn)


_AGENT_PREPARE_COMPRESS_DESC = {
    "type": "function",
    "function": {
        "name": "agent_prepare_compress_context",
        "description": "准备压缩当前绑定会话的上下文；宿主不支持时会明确拒绝。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_prepare_compress_context", _AGENT_PREPARE_COMPRESS_DESC, ToolType.SYSTEM_CTL)
async def agent_prepare_compress_context(conn):
    return await hermes_prepare_compress_context(conn)


_AGENT_CONFIRM_DESC = {
    "type": "function",
    "function": {
        "name": "agent_confirm_dispatch",
        "description": "仅在用户明确确认后，执行当前设备会话唯一待确认的 Agent 操作。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_confirm_dispatch", _AGENT_CONFIRM_DESC, ToolType.SYSTEM_CTL)
async def agent_confirm_dispatch(conn):
    return await hermes_confirm_dispatch(conn)


_AGENT_CANCEL_DESC = {
    "type": "function",
    "function": {
        "name": "agent_cancel_dispatch",
        "description": "取消当前小智会话中尚未执行的 Agent 操作。",
        "parameters": {"type": "object", "properties": {}},
    },
}


@register_function("agent_cancel_dispatch", _AGENT_CANCEL_DESC, ToolType.SYSTEM_CTL)
async def agent_cancel_dispatch(conn):
    return await hermes_cancel_dispatch(conn)


# Capture the loaded runtime settings while xiaozhi-server imports plugins.
# Listener threads still wait for the first live connection/event loop.
if os.environ.get("LINGXI_CONTROL_ENABLED", "").strip().lower() in {
    "1",
    "true",
    "yes",
}:
    try:
        _get_control_runtime()
    except Exception:
        # A live tool call will retry and project only the exception type into
        # the server log; control UI startup must never remove voice tools.
        pass

_install_control_lifecycle_hooks()
