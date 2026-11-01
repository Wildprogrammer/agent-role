"""Deterministic bound-Agent router with optional local or remote LLM modes."""

from __future__ import annotations

from copy import deepcopy
import json
import os
import re
from threading import Lock
from time import monotonic
from types import SimpleNamespace
import uuid

import httpx
from openai import OpenAI

from core.providers.llm.base import LLMProviderBase


_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000}
_SPOKEN_INTEGER_PATTERN = r"(?:[+-]?\d+|[负正]?[零〇一二两三四五六七八九十百千]+)"


def _parse_spoken_integer(value):
    text = str(value)
    if re.fullmatch(r"[+-]?\d+", text):
        return int(text)
    sign = -1 if text.startswith("负") else 1
    if text[:1] in {"负", "正"}:
        text = text[1:]
    total = 0
    digit = 0
    for character in text:
        if character in _CHINESE_DIGITS:
            digit = _CHINESE_DIGITS[character]
        elif character in _CHINESE_UNITS:
            total += (digit or 1) * _CHINESE_UNITS[character]
            digit = 0
        else:
            raise ValueError("invalid spoken integer")
    return sign * (total + digit)


class LLMProvider(LLMProviderBase):
    def __init__(self, config):
        self.mode = config.get("mode", "local")
        self.model_name = config.get("model_name")
        self.max_tokens = int(config.get("max_tokens", 512))
        self._extra_body = None
        if self.mode == "bound_agent":
            self.model_name = self.model_name or "bound-agent-router"
            self.base_url = None
            self.client = None
        elif self.mode == "local":
            base_url = config.get("base_url", "http://127.0.0.1:11434")
            self.base_url = base_url if base_url.endswith("/v1") else f"{base_url}/v1"
            # Windows proxy settings must not intercept the loopback Ollama call.
            self.client = OpenAI(
                base_url=self.base_url,
                api_key="ollama",
                http_client=httpx.Client(trust_env=False),
            )
        elif self.mode == "remote":
            api_key_env = config.get("api_key_env")
            if not isinstance(api_key_env, str) or not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]{0,127}", api_key_env
            ):
                raise ValueError("remote gateway LLM requires a valid api_key_env")
            api_key = os.environ.get(api_key_env)
            if not api_key:
                raise ValueError(f"remote gateway LLM environment variable is absent: {api_key_env}")
            base_url = config.get("base_url")
            if not isinstance(base_url, str) or not base_url.startswith("https://"):
                raise ValueError("remote gateway LLM requires an HTTPS base_url")
            self.base_url = base_url.rstrip("/")
            if config.get("disable_thinking", True):
                self._extra_body = {"thinking": {"type": "disabled"}}
            self.client = OpenAI(
                base_url=self.base_url,
                api_key=api_key,
                http_client=httpx.Client(trust_env=True),
            )
        else:
            raise ValueError("gateway LLM mode must be bound_agent, local, or remote")
        self._pending_forwarding: dict[str, float] = {}
        self._pending_forwarding_lock = Lock()

    @staticmethod
    def _message_text(value):
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    decoded = json.loads(stripped)
                except (TypeError, ValueError):
                    decoded = None
                if isinstance(decoded, dict) and isinstance(
                    decoded.get("content"), str
                ):
                    return decoded["content"]
            return value
        if isinstance(value, dict) and isinstance(value.get("content"), str):
            return value["content"]
        return ""

    @staticmethod
    def _without_thinking(dialogue):
        messages = deepcopy(dialogue)
        for message in reversed(messages):
            content = LLMProvider._message_text(message.get("content"))
            if message.get("role") == "user" and content:
                message["content"] = f"/no_think {content}"
                break
        return messages

    @staticmethod
    def _latest_user_text(dialogue):
        for message in reversed(dialogue):
            if message.get("role") == "tool":
                return ""
            content = LLMProvider._message_text(message.get("content"))
            if message.get("role") == "user" and content:
                return content.strip()
        return ""

    def _model_dialogue(self, dialogue):
        if self.mode == "local":
            return self._without_thinking(dialogue)
        return deepcopy(dialogue)

    @classmethod
    def _incomplete_forwarding_request(cls, dialogue):
        raw_user_text = cls._latest_user_text(dialogue)
        _, incomplete = cls._extract_forwarding_prompt(raw_user_text)
        return incomplete

    def _arm_pending_forwarding(self, session_id):
        with self._pending_forwarding_lock:
            now = monotonic()
            self._pending_forwarding = {
                key: armed_at
                for key, armed_at in self._pending_forwarding.items()
                if now - armed_at <= 30
            }
            self._pending_forwarding[session_id] = now

    def _consume_pending_forwarding(self, session_id):
        with self._pending_forwarding_lock:
            now = monotonic()
            self._pending_forwarding = {
                key: armed_at
                for key, armed_at in self._pending_forwarding.items()
                if now - armed_at <= 30
            }
            armed_at = self._pending_forwarding.pop(session_id, None)
        return armed_at is not None

    @staticmethod
    def _extract_forwarding_prompt(raw_user_text):
        command = re.match(
            r"^\s*(?:交给|让|请)\s*", raw_user_text, flags=re.IGNORECASE
        )
        if command is None:
            return None, False
        verb = command.group(0).strip().lower()
        remainder = raw_user_text[command.end() :]
        targets = [
            r"当前\s*hermes\s*profile(?=$|[^A-Za-z0-9_])",
            r"当前\s*智能体",
            r"当前\s*profile(?=$|[^A-Za-z0-9_])",
            r"当前\s*agent(?=$|[^A-Za-z0-9_])",
            r"hermes(?=$|[^A-Za-z0-9_])",
            r"当前(?=$|[\s：:,，;；])",
        ]
        if verb == "交给":
            targets.insert(2, r"智能体")
        for target in targets:
            matched = re.match(target, remainder, flags=re.IGNORECASE)
            if matched is None:
                continue
            body = re.sub(
                r"^[\s：:,，;；]+", "", remainder[matched.end() :]
            ).strip()
            if re.search(r"[A-Za-z0-9\u3400-\u9fff]", body):
                return body, False
            return None, True
        compact_remainder = re.sub(
            r"[\s：:,，;；。.!！?？]+$", "", "".join(remainder.lower().split())
        )
        incomplete_targets = {
            "当前",
            "当前智能",
            "当前只能",
            "当前只能提",
            "当前hermes",
            "当前hermesprofile",
            "当前profile",
            "当前agent",
            "hermes",
        }
        if verb == "交给":
            incomplete_targets.add("智能体")
        return None, compact_remainder in incomplete_targets

    @staticmethod
    def _forced_gateway_call(
        dialogue, functions, continuation_prompt=None, *, direct_bound=False
    ):
        """Build deterministic calls for gateway control and active-profile input."""
        available = {
            item.get("function", {}).get("name"): item.get("function", {})
            for item in functions or []
            if isinstance(item, dict)
        }
        def preferred(generic, legacy):
            return generic if generic in available else legacy

        def bound_dispatch_tool():
            if direct_bound and "agent_dispatch_bound" in available:
                return "agent_dispatch_bound"
            return preferred("agent_prepare_dispatch", "hermes_prepare_dispatch")

        def explicit_host(text):
            if any(
                term in text
                for term in (
                    "deepseek",
                    "deepseekharness",
                    "deepfi",
                    "深度求索",
                    "dsh",
                )
            ):
                return "deepseek-harness"
            if any(term in text for term in ("hermes", "herme")):
                return "hermes"
            return None

        def enum_values(tool_name, parameter_name):
            schema = available.get(tool_name, {}).get("parameters", {})
            values = schema.get("properties", {}).get(parameter_name, {}).get("enum", [])
            return [value for value in values if isinstance(value, str)]

        def matching_agent(text):
            values = enum_values("agent_switch_agent", "agent_id")
            direct = [value for value in values if value.lower() in text]
            if len(direct) == 1:
                return direct[0]
            aliases = {
                "游戏": "game-assistant",
                "游戏": "game-assistant",
                "专家": "specialist",
                "资料检索": "research-agent",
                "对话记录": "conversation-agent",
                "对话记录": "conversation-agent",
                "对话智能体": "conversation-agent",
                "助手智能体": "assistant-agent",
            }
            matched = {
                agent_id
                for alias, agent_id in aliases.items()
                if alias in text and agent_id in values
            }
            return next(iter(matched)) if len(matched) == 1 else None

        def matching_switch_command(text):
            values = enum_values("agent_switch_agent", "agent_id")
            aliases = {
                "游戏": "game-assistant",
                "游戏": "game-assistant",
                "专家": "specialist",
                "资料检索": "research-agent",
                "对话记录": "conversation-agent",
                "对话记录": "conversation-agent",
                "对话智能体": "conversation-agent",
                "助手智能体": "assistant-agent",
            }
            names = [(value.lower(), value) for value in values]
            names.extend(
                (alias, agent_id)
                for alias, agent_id in aliases.items()
                if agent_id in values
            )
            matched = set()
            for polite in ("", "请", "帮我"):
                for verb in ("切换", "切换到", "切换成", "换到", "换成"):
                    for host_label in (
                        "",
                        "hermes",
                        "herme",
                        "deepseek",
                        "deepseekharness",
                        "deepfi",
                        "深度求索",
                        "dsh",
                    ):
                        for name, agent_id in names:
                            for suffix in ("", "profile", "智能体", "agent"):
                                for ending in ("", "吧"):
                                    if text == (
                                        f"{polite}{verb}{host_label}{name}{suffix}{ending}"
                                    ):
                                        matched.add(agent_id)
            return next(iter(matched)) if len(matched) == 1 else None

        def is_bind_command(text):
            return re.fullmatch(
                r"(?:请|帮我)?绑定(?:到)?(?:hermes|deepseek|深度求索|dsh)?"
                r".+?(?:的)?(?:对话|会话)(?:id)?[：:=]?"
                r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}(?:吧)?",
                text,
                flags=re.IGNORECASE,
            ) is not None
        raw_user_text = ""
        latest_user_index = None
        for index in range(len(dialogue) - 1, -1, -1):
            message = dialogue[index]
            if message.get("role") == "tool":
                # A tool already ran for the latest user turn. Let the model
                # summarize its result instead of forcing the same tool again.
                return None
            content = LLMProvider._message_text(message.get("content"))
            if message.get("role") == "user" and content:
                raw_user_text = content.strip()
                latest_user_index = index
                break
        user_text = raw_user_text.lower()
        compact = "".join(user_text.split()).strip("。！？!?，,")
        forwarding_prompt, incomplete_forwarding = LLMProvider._extract_forwarding_prompt(
            raw_user_text
        )
        prior_assistant_text = ""
        if latest_user_index is not None:
            prior_assistant_text = next(
                (
                    LLMProvider._message_text(message.get("content"))
                    for message in reversed(dialogue[:latest_user_index])
                    if message.get("role") == "assistant"
                ),
                "",
            )
        pending_switch = "切换尚未完成" in prior_assistant_text
        pending_confirmation = (
            "请明确说" in prior_assistant_text
            and "确认" in prior_assistant_text
            and "才会执行" in prior_assistant_text
        )

        selected = None
        arguments = {}
        if compact in {
            "停止",
            "停下",
            "闭嘴",
            "别说了",
            "不要说了",
            "停止播报",
            "停止回答",
        } and "lingxi_stop_speaking" in available:
            selected = "lingxi_stop_speaking"
        elif compact in {
            "退出",
            "退出会话",
            "退出对话",
            "结束会话",
            "结束对话",
        } and "lingxi_exit_conversation" in available:
            selected = "lingxi_exit_conversation"
        elif pending_switch:
            if compact in {"取消", "取消切换", "不切换了", "算了"}:
                selected = "agent_cancel_switch"
            else:
                selected = "agent_continue_switch"
                arguments = {"choice": raw_user_text}
        elif pending_confirmation:
            if compact in {
                "确认",
                "确认执行",
                "执行",
                "执行吧",
                "好的确认",
                "确认开启新会话",
                "确认新建会话",
                "确认压缩上下文",
            }:
                selected = preferred("agent_confirm_dispatch", "hermes_confirm_dispatch")
            elif compact in {
                "取消",
                "取消执行",
                "不要执行",
                "不要确认",
                "算了",
            }:
                selected = preferred("agent_cancel_dispatch", "hermes_cancel_dispatch")
            else:
                # P1 owns the utterance while a lifecycle operation is pending.
                # The selected server tool re-checks the adapter's authoritative
                # pending state before it responds, so volume/catalog commands
                # cannot strand or silently replace the confirmation gate.
                selected = "agent_continue_pending_confirmation"
                arguments = {"choice": raw_user_text}
        elif compact in {
            "当前音量",
            "当前音量是多少",
            "音量是多少",
            "音量多少",
            "现在音量是多少",
            "声音多大",
            "当前声音多大",
        } and "self_audio_speaker_get_volume" in available:
            selected = "self_audio_speaker_get_volume"
        elif compact in {"静音", "设为静音", "打开静音"} and (
            "self_audio_speaker_set_volume_verified" in available
        ):
            selected = "self_audio_speaker_set_volume_verified"
            arguments = {"volume": 0}
        elif compact in {"最大音量", "音量最大", "声音最大", "音量调到最大"} and (
            "self_audio_speaker_set_volume_verified" in available
        ):
            selected = "self_audio_speaker_set_volume_verified"
            arguments = {"volume": 100}
        elif compact in {"最小音量", "音量最小", "声音最小", "音量调到最小"} and (
            "self_audio_speaker_set_volume_verified" in available
        ):
            selected = "self_audio_speaker_set_volume_verified"
            arguments = {"volume": 0}
        else:
            absolute_volume = re.fullmatch(
                r"(?:请|帮我)?(?:把)?(?:音量|声音)(?:设置|设|调|调节|调成|调到|设置为|设为)"
                rf"(?:百分之)?({_SPOKEN_INTEGER_PATTERN})(?:%|个百分点)?(?:吧)?",
                compact,
            )
            increase_volume = re.fullmatch(
                r"(?:请|帮我)?(?:把)?(?:音量|声音)?"
                rf"(?:提高|增加|调大|升高)(?:百分之)?({_SPOKEN_INTEGER_PATTERN})(?:%|个百分点)?(?:吧)?",
                compact,
            )
            decrease_volume = re.fullmatch(
                r"(?:请|帮我)?(?:把)?(?:音量|声音)?"
                rf"(?:降低|减少|调小|减小)(?:百分之)?({_SPOKEN_INTEGER_PATTERN})(?:%|个百分点)?(?:吧)?",
                compact,
            )
            signed_relative_volume = re.fullmatch(
                rf"(?:请|帮我)?(?:把)?(?:音量|声音)(?:调整|调节|变化|改动)"
                rf"([+-]\d+)(?:个百分点|%)?(?:吧)?",
                compact,
            )
            louder = compact in {
                "声音大一点",
                "声音大点",
                "音量大一点",
                "音量大点",
                "调大一点",
                "调大点",
            }
            quieter = compact in {
                "声音小一点",
                "声音小点",
                "音量小一点",
                "音量小点",
                "调小一点",
                "调小点",
            }
            if absolute_volume is not None:
                volume = _parse_spoken_integer(absolute_volume.group(1))
                if not 0 <= volume <= 100 and "lingxi_reject_volume" in available:
                    selected = "lingxi_reject_volume"
                    arguments = {"volume": volume}
                elif "self_audio_speaker_set_volume_verified" in available:
                    selected = "self_audio_speaker_set_volume_verified"
                    arguments = {"volume": volume}
            elif (increase_volume is not None or louder) and (
                "self_audio_speaker_adjust_volume" in available
            ):
                delta = (
                    _parse_spoken_integer(increase_volume.group(1))
                    if increase_volume
                    else 10
                )
                selected = "self_audio_speaker_adjust_volume"
                arguments = {"delta": max(-100, min(delta, 100))}
            elif (decrease_volume is not None or quieter) and (
                "self_audio_speaker_adjust_volume" in available
            ):
                delta = (
                    _parse_spoken_integer(decrease_volume.group(1))
                    if decrease_volume
                    else 10
                )
                selected = "self_audio_speaker_adjust_volume"
                arguments = {"delta": max(-100, min(-delta, 100))}
            elif signed_relative_volume is not None and (
                "self_audio_speaker_adjust_volume" in available
            ):
                delta = int(signed_relative_volume.group(1))
                selected = "self_audio_speaker_adjust_volume"
                arguments = {"delta": max(-100, min(delta, 100))}

        if selected is not None:
            pass
        elif continuation_prompt is not None:
            selected = bound_dispatch_tool()
            arguments = {"prompt": continuation_prompt}
        elif forwarding_prompt is not None and not incomplete_forwarding:
            # An explicit forwarding prefix owns the entire remainder as
            # message content.  Management words inside that body (for
            # example "列出 Profile" or "开启新会话") must not be
            # reinterpreted as gateway-control commands.
            selected = bound_dispatch_tool()
            arguments = {"prompt": forwarding_prompt}
        elif compact in {
            "开启新会话",
            "新建会话",
            "开始新会话",
            "重新开始会话",
            "开启空白会话",
        } or re.fullmatch(
            r"(?:请|帮我)?(?:开启|新建|创建|开始|重新开始|新开)(?:一个)?(?:空白)?(?:新)?会话(?:吧)?",
            compact,
        ):
            selected = preferred("agent_prepare_new_session", "hermes_prepare_new_session")
        elif compact in {
            "压缩上下文",
            "压缩当前上下文",
            "压缩当前会话",
            "整理上下文",
        } or re.fullmatch(
            r"(?:请|帮我)?(?:压缩|整理)(?:一下)?(?:当前)?(?:会话)?上下文(?:吧)?",
            compact,
        ):
            selected = preferred(
                "agent_prepare_compress_context", "hermes_prepare_compress_context"
            )
        elif compact in {
            "清除上下文",
            "清空上下文",
            "重置上下文",
            "忘掉之前的内容",
            "忘掉之前的对话",
            "从头开始",
            "重新来过",
        } or re.fullmatch(
            r"(?:请|帮我)?(?:清除|清空|重置)(?:一下)?(?:当前)?(?:会话)?上下文(?:吧)?",
            compact,
        ):
            selected = "hermes_clarify_clear_context"
        elif compact in {"当前绑定", "查看绑定", "hermes绑定", "绑定了哪个对话"} or (
            ("当前" in compact or "现在" in compact)
            and any(term in compact for term in ("profile", "profi", "智能体", "agent"))
            and any(term in compact for term in ("哪个", "什么", "哪里", "哪一个"))
        ):
            selected = preferred("agent_show_binding", "hermes_show_binding")
        elif compact in {"解除绑定", "取消绑定", "解绑hermes", "解绑对话"}:
            selected = preferred("agent_unbind_session", "hermes_unbind_session")
        elif (
            (
                "列出" in compact
                or "有哪些" in compact
                or "有几个" in compact
                or "查询" in compact
                or "可用" in compact
                or "列表" in compact
            )
            and (
                "agent" in compact
                or "智能体" in compact
                or "preset" in compact
                or ("profile" in compact and "hermes" not in compact)
            )
            and "对话" not in compact
            and "会话" not in compact
        ):
            selected = "agent_list_agents"
        elif ("对话" in compact or "会话" in compact) and (
            "列出" in compact or "有哪些" in compact or "列表" in compact
        ):
            selected = preferred("agent_list_sessions", "hermes_list_sessions")
            parameter_name = "agent_id" if selected == "agent_list_sessions" else "profile"
            profile_schema = available.get(selected, {}).get("parameters", {})
            profile_schema = profile_schema.get("properties", {}).get(parameter_name, {})
            matches = [
                profile
                for profile in profile_schema.get("enum", [])
                if isinstance(profile, str) and profile.lower() in compact
            ]
            if len(matches) != 1:
                return None
            arguments = {parameter_name: matches[0], "limit": 5}
            if selected == "agent_list_sessions":
                host = explicit_host(compact)
                if host is None:
                    return None
                arguments["host"] = host
        elif is_bind_command(compact):
            selected = preferred("agent_bind_session", "hermes_bind_session")
            parameter_name = "agent_id" if selected == "agent_bind_session" else "profile"
            profile_schema = available.get(selected, {}).get("parameters", {})
            profile_schema = profile_schema.get("properties", {}).get(parameter_name, {})
            matches = [
                profile
                for profile in profile_schema.get("enum", [])
                if isinstance(profile, str) and profile.lower() in compact
            ]
            session_match = re.search(
                r"(?:id\s*[：:=]?\s*|(?:会话|对话)(?:\s*id)?\s*[：:=]?\s*)"
                r"([A-Za-z0-9][A-Za-z0-9._:-]{3,127})",
                raw_user_text,
                flags=re.IGNORECASE,
            )
            if len(matches) != 1 or session_match is None:
                return None
            arguments = {
                parameter_name: matches[0],
                "stored_session_id": session_match.group(1),
            }
            if selected == "agent_bind_session":
                host = explicit_host(compact)
                if host is None:
                    return None
                arguments["host"] = host
        elif (agent_id := matching_switch_command(compact)) is not None:
            if "agent_switch_agent" in available:
                selected = "agent_switch_agent"
                arguments = {"agent_id": agent_id}
                host = explicit_host(compact)
                if host is not None:
                    arguments["host"] = host
            elif agent_id in enum_values("hermes_switch_profile", "profile"):
                selected = "hermes_switch_profile"
                arguments = {"profile": agent_id}
        elif (
            ("列出" in compact or "有哪些" in compact or "可用" in compact or "列表" in compact)
            and ("profile" in compact or "hermes" in compact)
        ):
            selected = "hermes_list_profiles"
        else:
            if latest_user_index is not None and any(
                message.get("role") == "assistant"
                and isinstance(LLMProvider._message_text(message.get("content")), str)
                and re.search(
                    r"已(?:绑定|切换)到\s+.+?(?:的)?(?:对话|，继续对话)",
                    LLMProvider._message_text(message.get("content")),
                    flags=re.IGNORECASE,
                )
                for message in dialogue[:latest_user_index]
            ):
                # Compatibility for model-backed modes: once a durable binding
                # succeeded in this local conversation, keep follow-up speech
                # on the gateway instead of letting the chat model invent an
                # answer. ``bound_agent`` does not depend on this history and
                # routes every otherwise-unmatched utterance directly below.
                selected = bound_dispatch_tool()
                arguments = {"prompt": raw_user_text}
            elif direct_bound and raw_user_text:
                selected = bound_dispatch_tool()
                arguments = {"prompt": raw_user_text}

        if selected in available:
            return SimpleNamespace(
                id=f"call_gateway_{uuid.uuid4().hex}",
                function=SimpleNamespace(
                    name=selected,
                    arguments=json.dumps(arguments, ensure_ascii=False),
                ),
            )
        return None

    def response(self, session_id, dialogue, **kwargs):
        if self.mode == "bound_agent":
            yield "本次语音会话已结束，再见。"
            return
        request = dict(
            model=self.model_name,
            messages=self._model_dialogue(dialogue),
            stream=False,
            max_tokens=self.max_tokens,
        )
        if self._extra_body is not None:
            request["extra_body"] = self._extra_body
        completion = self.client.chat.completions.create(**request)
        content = completion.choices[0].message.content
        if content:
            yield content

    def response_with_functions(self, session_id, dialogue, functions=None, **kwargs):
        if self._incomplete_forwarding_request(dialogue):
            self._arm_pending_forwarding(session_id)
            yield "请在“交给当前智能体”后说出完整问题。", None
            return
        continuation_prompt = None
        if self._consume_pending_forwarding(session_id):
            raw_user_text = self._latest_user_text(dialogue)
            compact = "".join(raw_user_text.lower().split()).strip("。！？!?，,")
            if compact in {"取消", "不用了", "算了"}:
                yield "已取消。", None
                return
            explicit_prompt, _ = self._extract_forwarding_prompt(raw_user_text)
            if explicit_prompt is None:
                continuation_prompt = raw_user_text
        forced_call = self._forced_gateway_call(
            dialogue,
            functions,
            continuation_prompt=continuation_prompt,
            direct_bound=getattr(self, "mode", "local") == "bound_agent",
        )
        if forced_call is not None:
            if forced_call.function.name in {
                "agent_confirm_dispatch",
                "hermes_confirm_dispatch",
            }:
                yield "已确认，正在处理，请稍候。", None
            yield None, [forced_call]
            return
        if self.mode == "bound_agent":
            yield "当前语音没有可路由的正文。", None
            return
        request = {
            "model": self.model_name,
            "messages": self._model_dialogue(dialogue),
            "tools": functions,
            "stream": False,
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        if self._extra_body is not None:
            request["extra_body"] = self._extra_body
        completion = self.client.chat.completions.create(
            **request,
        )
        message = completion.choices[0].message
        yield message.content, message.tool_calls
