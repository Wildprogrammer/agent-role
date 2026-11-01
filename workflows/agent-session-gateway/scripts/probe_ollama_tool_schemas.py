"""Probe which Xiaozhi tool/message combinations an Ollama model accepts."""

from __future__ import annotations

import json
import os

import httpx

from plugins_func.loadplugins import auto_import_modules
from plugins_func.register import all_function_registry


NAMES = [
    "hermes_list_profiles",
    "hermes_switch_profile",
    "hermes_prepare_dispatch",
    "hermes_confirm_dispatch",
    "hermes_cancel_dispatch",
    "get_lunar",
    "handle_exit_intent",
]

DIRECT_ANSWER_TOOL = {
    "type": "function",
    "function": {
        "name": "direct_answer",
        "description": "当用户的请求不匹配其他任何工具时，可用此选项直接回复。将回复内容写在response参数里。",
        "parameters": {
            "type": "object",
            "properties": {
                "response": {
                    "type": "string",
                    "description": "你回复用户的完整内容",
                }
            },
            "required": ["response"],
        },
    },
}


def _messages(*, include_fewshot=False):
    messages = [
        {
            "role": "system",
            "content": os.environ.get(
                "XIAOZHI_PROBE_SYSTEM_PROMPT",
                "你是本机小智语音网关。回答简短、准确，优先使用已提供的 Hermes 工具。",
            ),
        }
    ]
    if include_fewshot:
        messages.extend(
            [
                {"role": "user", "content": "给我讲个故事吧"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "fewshot_da_001",
                            "function": {
                                "arguments": '{"response": "好呀，你想听什么类型的呀？童话、冒险还是搞笑的？选一个我给你开讲~"}',
                                "name": "direct_answer",
                            },
                            "type": "function",
                            "index": 0,
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "fewshot_da_001",
                    "content": "已直接回复",
                },
                {"role": "user", "content": "拜拜"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "fewshot_exit_001",
                            "function": {
                                "arguments": '{"say_goodbye": "再见，下次再聊~"}',
                                "name": "handle_exit_intent",
                            },
                            "type": "function",
                            "index": 0,
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "fewshot_exit_001",
                    "content": "退出意图已处理",
                },
                {"role": "assistant", "content": "再见，下次再聊~"},
            ]
        )
    messages.append({"role": "user", "content": "/no_think 列出 Hermes Profile"})
    return messages


def _request(tools, *, include_fewshot=False):
    with httpx.Client(trust_env=False) as client:
        response = client.post(
            "http://127.0.0.1:11434/v1/chat/completions",
            json={
                "model": "qwen3:1.7b",
                "messages": _messages(include_fewshot=include_fewshot),
                "stream": False,
                "tools": tools,
            },
            timeout=120,
        )
    return {"status": response.status_code, "body": response.text[:500]}


def main() -> int:
    auto_import_modules("plugins_func.functions")
    tools = {name: all_function_registry[name].description for name in NAMES}
    all_tools = list(tools.values())
    result = {
        "individual": {name: _request([tool]) for name, tool in tools.items()},
        "cumulative": {},
        "server_shape": {
            "all_plus_direct_answer": _request(all_tools + [DIRECT_ANSWER_TOOL]),
            "all_plus_direct_answer_and_fewshot": _request(
                all_tools + [DIRECT_ANSWER_TOOL], include_fewshot=True
            ),
        },
    }
    current = []
    for name in NAMES:
        current.append(tools[name])
        result["cumulative"][name] = _request(current)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
