from __future__ import annotations

import asyncio
import hashlib
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge import xiaozhi_server_adapter
from agent_workflow_hub.xiaozhi_hermes_bridge.xiaozhi_server_adapter import (
    BridgeProcessClient,
    BridgeProcessSettings,
    XiaozhiAdapterError,
    XiaozhiHermesAdapter,
)


@dataclass
class FakeClient:
    active_profile: str = "default"
    calls: list[dict[str, object]] = field(default_factory=list)

    async def request(
        self,
        operation: str,
        *,
        session_id: str,
        profile: str | None = None,
        input: str | None = None,
        confirmed: bool | None = None,
        device_id: str | None = None,
        stored_session_id: str | None = None,
        limit: int | None = None,
        idempotency_key: str | None = None,
        host: str | None = None,
        binding_revision: int | None = None,
    ) -> Mapping[str, object]:
        self.calls.append(
            {
                "operation": operation,
                "session_id": session_id,
                "profile": profile,
                "input": input,
                "confirmed": confirmed,
                "device_id": device_id,
                "stored_session_id": stored_session_id,
                "limit": limit,
                "idempotency_key": idempotency_key,
                "host": host,
                "binding_revision": binding_revision,
            }
        )
        if operation == "switch_profile":
            assert profile is not None
            self.active_profile = profile
            return {"status": "ok", "active_profile": profile}
        if operation == "list_profiles":
            return {"status": "ok", "active_profile": self.active_profile}
        if operation == "list_agents":
            return {
                "status": "ok",
                "agents": [
                    {"host": "hermes", "id": "default", "name": "default"},
                    {
                        "host": "deepseek-harness",
                        "id": "assistant-agent",
                        "name": "assistant-agent",
                    },
                ],
            }
        if operation == "show_binding":
            return {
                "status": "ok",
                "bound": True,
                "host": "hermes",
                "profile": "research-agent",
                "stored_session_id": "stored-1",
                "binding_revision": 1,
            }
        if operation == "bind_session":
            return {"status": "ok", "bound": True}
        if operation == "unbind_session":
            return {"status": "ok", "removed": True}
        if operation in {"dispatch_shared", "dispatch_bound_voice"}:
            return {"status": "completed", "output": "shared answer"}
        if operation == "create_session":
            return {
                "status": "completed",
                "profile": "research-agent",
                "stored_session_id": "stored-new",
                "runtime_session_id": "runtime-new",
            }
        if operation == "compress_session":
            return {
                "status": "completed",
                "profile": "research-agent",
                "stored_session_id": "stored-compressed",
                "runtime_session_id": "runtime-1",
                "compression_status": "compressed",
            }
        return {
            "status": "completed",
            "active_profile": self.active_profile,
            "run_id": "run-1",
            "output": "done",
        }


def test_prepare_and_confirm_is_two_step_and_attests_only_on_confirm() -> None:
    async def exercise() -> None:
        client = FakeClient(active_profile="assistant-agent")
        adapter = XiaozhiHermesAdapter(client)

        prepared = await adapter.prepare_dispatch("session-a", " inspect project ")
        assert prepared["status"] == "confirmation_required"
        assert [call["operation"] for call in client.calls] == ["list_profiles"]

        result = await adapter.confirm_dispatch("session-a")
        assert result["status"] == "completed"
        assert [call["operation"] for call in client.calls] == [
            "list_profiles",
            "list_profiles",
            "dispatch",
        ]
        assert client.calls[-1]["input"] == "inspect project"
        assert client.calls[-1]["confirmed"] is True

        with pytest.raises(XiaozhiAdapterError, match="no pending"):
            await adapter.confirm_dispatch("session-a")

    asyncio.run(exercise())


def test_legacy_confirmation_uses_the_same_180_second_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        now = {"value": 100.0}
        monkeypatch.setattr(
            xiaozhi_server_adapter.time,
            "monotonic",
            lambda: now["value"],
        )
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        prepared = await adapter.prepare_dispatch("legacy-session", "inspect")
        assert prepared["expires_in_seconds"] == 180
        calls_before_confirmation = len(client.calls)

        now["value"] += 180.001
        with pytest.raises(XiaozhiAdapterError, match="已过期，本次未提交"):
            await adapter.confirm_dispatch("legacy-session")
        assert len(client.calls) == calls_before_confirmation

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "prompt",
    [
        "交给当前",
        "交给当前智能体",
        "交给当前智能",
        "交给当前 Profile",
        "让当前agentic解释",
        "请 Hermes",
        "请Hermesian解释",
        "交给当前；",
        "；！？",
    ],
)
def test_prepare_rejects_routing_prefix_without_message(prompt: str) -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        with pytest.raises(
            XiaozhiAdapterError, match="缺少正文|contain message text"
        ):
            await adapter.prepare_shared_dispatch(
                "socket-session", device_id="esp32-s3", prompt=prompt
            )
        with pytest.raises(
            XiaozhiAdapterError, match="缺少正文|contain message text"
        ):
            await adapter.prepare_dispatch("legacy-session", prompt)

        assert client.calls == []

    asyncio.run(exercise())


def test_prepare_defensively_extracts_complete_forwarding_prefix() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        await adapter.prepare_shared_dispatch(
            "socket-session",
            device_id="esp32-s3",
            prompt="交给当前智能体：今天有执行自动同步任务吗？",
        )
        await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )

        assert client.calls[-1]["input"] == "今天有执行自动同步任务吗？"

    asyncio.run(exercise())


def test_shared_prepare_and_confirm_uses_stable_device_binding() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        prepared = await adapter.prepare_shared_dispatch(
            "socket-session", device_id="esp32-s3", prompt=" list projects "
        )
        assert prepared["stored_session_id"] == "stored-1"

        result = await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )

        assert result["output"] == "shared answer"
        assert [call["operation"] for call in client.calls] == [
            "show_binding",
            "show_binding",
            "dispatch_shared",
        ]
        assert client.calls[-1]["device_id"] == "esp32-s3"
        assert client.calls[-1]["confirmed"] is True

    asyncio.run(exercise())


def test_bound_dispatch_sends_once_without_forging_confirmation() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        result = await adapter.dispatch_bound(
            "fresh-socket-session",
            device_id="esp32-s3",
            prompt="交给当前智能体： 现在有哪些 RAG 项目？ ",
        )

        assert result["status"] == "completed"
        assert [call["operation"] for call in client.calls] == [
            "show_binding",
            "dispatch_bound_voice",
        ]
        dispatch = client.calls[-1]
        assert dispatch["input"] == "现在有哪些 RAG 项目？"
        assert dispatch["confirmed"] is None
        assert dispatch["device_id"] == "esp32-s3"
        assert dispatch["host"] == "hermes"
        assert dispatch["profile"] == "research-agent"
        assert dispatch["stored_session_id"] == "stored-1"
        assert dispatch["binding_revision"] == 1
        assert adapter._pending == {}

    asyncio.run(exercise())


@pytest.mark.parametrize("changed", ["device_id", "host", "profile", "stored_session_id", "binding_revision"])
def test_bound_dispatch_rejects_attachment_binding_drift(changed: str) -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)
        expected = xiaozhi_server_adapter.AttachmentBinding(
            "esp32-s3", "hermes", "research-agent", "stored-1", 1
        )
        expected = replace(expected, **{changed: 2 if changed == "binding_revision" else "other"})
        result = await adapter.dispatch_bound(
            "photo-session", device_id="esp32-s3", prompt="看看这张照片",
            expected_binding=expected, attachments=({"attachment_id": "photo"},),
        )
        assert result["status"] == "rejected"
        assert [call["operation"] for call in client.calls] == ["show_binding"]
        assert adapter._pending == {}

    asyncio.run(exercise())


def test_bound_dispatch_retains_matching_frozen_binding() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)
        expected = xiaozhi_server_adapter.AttachmentBinding(
            "esp32-s3", "hermes", "research-agent", "stored-1", 1
        )
        result = await adapter.dispatch_bound(
            "photo-session", device_id="esp32-s3", prompt="看看这张照片",
            expected_binding=expected,
        )
        assert result["status"] == "completed"
        assert client.calls[-1]["binding_revision"] == expected.binding_revision
        assert client.calls[-1]["stored_session_id"] == expected.stored_session_id

    asyncio.run(exercise())


def test_bound_dispatch_forwards_progress_and_uses_independent_cancel_client() -> None:
    class StreamingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            progress = kwargs.pop("on_progress", None)
            result = await super().request(operation, **kwargs)
            if operation == "dispatch_bound_voice":
                assert progress is not None
                callback_result = progress("第一句。")
                if callback_result is not None:
                    await callback_result
                self.started.set()
                await self.release.wait()
                return {"status": "cancelled", "output": "第一句。"}
            return result

    class ControlClient(FakeClient):
        async def request(self, operation: str, **kwargs):
            await super().request(operation, **kwargs)
            assert operation == "cancel_bound_voice"
            return {"status": "cancelled", "operation": operation}

    async def exercise() -> None:
        dispatch = StreamingClient()
        control = ControlClient()
        adapter = XiaozhiHermesAdapter(dispatch, control_client=control)
        progress: list[str] = []
        task = asyncio.create_task(
            adapter.dispatch_bound(
                "socket-session",
                device_id="esp32-s3",
                prompt="长任务",
                on_progress=progress.append,
            )
        )
        await dispatch.started.wait()

        cancelled = await adapter.cancel_active_for_device(
            "stop-session", device_id="esp32-s3"
        )
        assert adapter._pending["socket-session"].state == "cancelling"
        assert adapter._pending["socket-session"].cancel_complete is True
        with pytest.raises(XiaozhiAdapterError, match="already has an Agent operation"):
            await adapter.dispatch_bound(
                "next-session", device_id="esp32-s3", prompt="下一轮不得抢跑"
            )
        dispatch.release.set()
        result = await task

        assert cancelled["status"] == "cancelled"
        assert result["status"] == "cancelled"
        assert progress == ["第一句。"]
        assert [call["operation"] for call in control.calls] == [
            "cancel_bound_voice"
        ]
        assert adapter._pending == {}

    asyncio.run(exercise())


def test_stdio_cancellation_discards_unread_frames_before_reuse() -> None:
    child = r'''
import json, sys, time
for raw in sys.stdin:
    request = json.loads(raw)
    common = {
        "schema_version": "1.0",
        "operation": request["operation"],
        "session_id": request["session_id"],
    }
    if request["operation"] == "dispatch_bound_voice":
        print(json.dumps({**common, "status": "progress", "output": "first"}), flush=True)
        time.sleep(30)
        print(json.dumps({**common, "status": "completed", "output": "late"}), flush=True)
    else:
        print(json.dumps({**common, "status": "ok"}), flush=True)
'''

    async def exercise() -> None:
        client = BridgeProcessClient(
            BridgeProcessSettings(
                command=(sys.executable, "-u", "-c", child),
                caller_id="test-client",
                response_timeout_seconds=35,
            )
        )
        progress_seen = asyncio.Event()

        def on_progress(_text: str) -> None:
            progress_seen.set()

        task = asyncio.create_task(
            client.request(
                "dispatch_bound_voice",
                session_id="cancelled-session",
                on_progress=on_progress,
            )
        )
        await asyncio.wait_for(progress_seen.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._process is None

        result = await client.request("list_profiles", session_id="fresh-session")
        assert result["status"] == "ok"
        assert result["session_id"] == "fresh-session"
        await client.close()

    asyncio.run(exercise())


def test_stdio_rejects_a_frame_for_another_request() -> None:
    child = r'''
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({
    "schema_version": "1.0",
    "status": "ok",
    "operation": request["operation"],
    "session_id": "wrong-session",
}), flush=True)
'''

    async def exercise() -> None:
        client = BridgeProcessClient(
            BridgeProcessSettings(
                command=(sys.executable, "-u", "-c", child),
                caller_id="test-client",
                response_timeout_seconds=5,
            )
        )
        with pytest.raises(XiaozhiAdapterError, match="different request"):
            await client.request("list_profiles", session_id="expected-session")
        assert client._process is None

    asyncio.run(exercise())


def test_cancelled_dispatch_is_retained_as_outcome_unknown() -> None:
    class BlockingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.never = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "dispatch_bound_voice":
                self.started.set()
                await self.never.wait()
            return result

    async def exercise() -> None:
        client = BlockingClient()
        adapter = XiaozhiHermesAdapter(client)
        task = asyncio.create_task(
            adapter.dispatch_bound(
                "cancelled-socket", device_id="esp32-s3", prompt="写入中"
            )
        )
        await client.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        pending = adapter._pending["cancelled-socket"]
        assert pending.state == "outcome_unknown"
        with pytest.raises(XiaozhiAdapterError, match="already has an Agent operation"):
            await adapter.dispatch_bound(
                "fresh-socket", device_id="esp32-s3", prompt="不得自动重放"
            )

    asyncio.run(exercise())


def test_cancel_tombstone_waits_when_data_finishes_before_control() -> None:
    class StreamingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "dispatch_bound_voice":
                self.started.set()
                await self.release.wait()
            return result

    class DelayedControlClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            await super().request(operation, **kwargs)
            self.started.set()
            await self.release.wait()
            return {"status": "requested", "operation": operation}

    async def exercise() -> None:
        data = StreamingClient()
        control = DelayedControlClient()
        adapter = XiaozhiHermesAdapter(data, control_client=control)
        dispatch_task = asyncio.create_task(
            adapter.dispatch_bound(
                "socket-session", device_id="esp32-s3", prompt="长任务"
            )
        )
        await data.started.wait()
        cancel_task = asyncio.create_task(
            adapter.cancel_active_for_device(
                "stop-session", device_id="esp32-s3"
            )
        )
        await control.started.wait()

        data.release.set()
        await dispatch_task
        pending = adapter._pending["socket-session"]
        assert pending.state == "cancelling"
        assert pending.data_complete is True
        with pytest.raises(XiaozhiAdapterError, match="already has an Agent operation"):
            await adapter.dispatch_bound(
                "next-session", device_id="esp32-s3", prompt="不得抢跑"
            )

        control.release.set()
        cancelled = await cancel_task
        assert cancelled["status"] == "requested"
        assert adapter._pending == {}

    asyncio.run(exercise())


def test_unknown_cancel_outcome_retains_tombstone_after_data_finishes() -> None:
    class DataClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "dispatch_bound_voice":
                self.started.set()
                await self.release.wait()
            return result

    class UnknownControlClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            await super().request(operation, **kwargs)
            self.started.set()
            await self.release.wait()
            return {"status": "outcome_unknown", "operation": operation}

    async def exercise() -> None:
        data = DataClient()
        control = UnknownControlClient()
        adapter = XiaozhiHermesAdapter(data, control_client=control)
        dispatch_task = asyncio.create_task(
            adapter.dispatch_bound(
                "socket-session", device_id="esp32-s3", prompt="长任务"
            )
        )
        await data.started.wait()
        cancel_task = asyncio.create_task(
            adapter.cancel_active_for_device(
                "stop-session", device_id="esp32-s3"
            )
        )
        await control.started.wait()
        data.release.set()
        await dispatch_task
        control.release.set()
        result = await cancel_task

        assert result["status"] == "outcome_unknown"
        pending = adapter._pending["socket-session"]
        assert pending.state == "outcome_unknown"
        assert pending.data_complete is True
        with pytest.raises(XiaozhiAdapterError, match="already has an Agent operation"):
            await adapter.dispatch_bound(
                "next-session", device_id="esp32-s3", prompt="不得抢跑"
            )

    asyncio.run(exercise())


def test_bound_dispatch_is_blocked_while_lifecycle_confirmation_is_pending() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_new_session(
            "socket-session-a", device_id="esp32-s3"
        )

        with pytest.raises(XiaozhiAdapterError, match="already has an Agent operation"):
            await adapter.dispatch_bound(
                "socket-session-b", device_id="esp32-s3", prompt="不得提交到旧绑定"
            )

        assert not any(
            call["operation"] == "dispatch_bound_voice" for call in client.calls
        )

    asyncio.run(exercise())


def test_concurrent_bound_dispatches_write_at_most_once() -> None:
    class BlockingDispatchClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.dispatch_started = asyncio.Event()
            self.release_dispatch = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "dispatch_bound_voice":
                self.dispatch_started.set()
                await self.release_dispatch.wait()
            return result

    async def exercise() -> None:
        client = BlockingDispatchClient()
        adapter = XiaozhiHermesAdapter(client)

        first = asyncio.create_task(
            adapter.dispatch_bound(
                "socket-session-a", device_id="esp32-s3", prompt="只提交一次"
            )
        )
        await client.dispatch_started.wait()
        second = asyncio.create_task(
            adapter.dispatch_bound(
                "socket-session-b", device_id="esp32-s3", prompt="重复事件"
            )
        )
        await asyncio.sleep(0)
        client.release_dispatch.set()
        results = await asyncio.gather(first, second, return_exceptions=True)

        assert sum(isinstance(result, XiaozhiAdapterError) for result in results) == 1
        assert len(
            [call for call in client.calls if call["operation"] == "dispatch_bound_voice"]
        ) == 1

    asyncio.run(exercise())


def test_unknown_bound_dispatch_is_retained_and_never_blindly_replayed() -> None:
    class UnknownClient(FakeClient):
        async def request(self, operation: str, **kwargs):
            if operation == "dispatch_bound_voice":
                await super().request(operation, **kwargs)
                raise TimeoutError("host result is unknown")
            return await super().request(operation, **kwargs)

    async def exercise() -> None:
        client = UnknownClient()
        adapter = XiaozhiHermesAdapter(client)

        with pytest.raises(TimeoutError, match="unknown"):
            await adapter.dispatch_bound(
                "socket-session-a", device_id="esp32-s3", prompt="可能已提交"
            )
        assert adapter._pending["socket-session-a"].state == "outcome_unknown"

        with pytest.raises(XiaozhiAdapterError, match="reconcile"):
            await adapter.dispatch_bound(
                "socket-session-b", device_id="esp32-s3", prompt="不得自动重放"
            )
        assert len(
            [call for call in client.calls if call["operation"] == "dispatch_bound_voice"]
        ) == 1

    asyncio.run(exercise())


def test_device_switch_cancels_only_prepared_agent_operations() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_shared_dispatch(
            "socket-session", device_id="esp32-s3", prompt="list projects"
        )

        pending = await adapter.pending_for_device(device_id="esp32-s3")
        assert pending["pending"] is True
        assert pending["pending_operation"] == "dispatch_shared"
        assert pending["pending_state"] == "prepared"

        cancelled = await adapter.cancel_prepared_for_device(device_id="esp32-s3")
        assert cancelled["cancelled_count"] == 1
        assert (await adapter.pending_for_device(device_id="esp32-s3"))["pending"] is False
        with pytest.raises(XiaozhiAdapterError, match="no pending"):
            await adapter.confirm_shared_dispatch(
                "socket-session", device_id="esp32-s3"
            )

        await adapter.prepare_shared_dispatch(
            "socket-session", device_id="esp32-s3", prompt="list projects"
        )
        adapter._pending["socket-session"] = replace(
            adapter._pending["socket-session"], state="outcome_unknown"
        )
        with pytest.raises(XiaozhiAdapterError, match="reconcile"):
            await adapter.cancel_prepared_for_device(device_id="esp32-s3")
        assert adapter._pending["socket-session"].state == "outcome_unknown"
        unknown = await adapter.pending_for_device(device_id="esp32-s3")
        assert unknown["pending"] is True
        assert unknown["pending_state"] == "outcome_unknown"

    asyncio.run(exercise())


def test_shared_confirmation_allows_voice_retry_and_expires_after_180_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        now = {"value": 100.0}
        monkeypatch.setattr(
            xiaozhi_server_adapter.time,
            "monotonic",
            lambda: now["value"],
        )
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        prepared = await adapter.prepare_shared_dispatch(
            "socket-session",
            device_id="esp32-s3",
            prompt="查询每日同步任务时间",
        )
        assert prepared["expires_in_seconds"] == 180

        now["value"] += 180.0
        result = await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )
        assert result["status"] == "completed"
        assert client.calls[-1]["operation"] == "dispatch_shared"

        await adapter.prepare_shared_dispatch(
            "socket-session",
            device_id="esp32-s3",
            prompt="查询另一个任务",
        )
        calls_before_expired_confirmation = len(client.calls)
        now["value"] += 180.001
        with pytest.raises(
            XiaozhiAdapterError,
            match="已过期，本次未提交.*重新说出完整请求",
        ) as expired:
            await adapter.confirm_shared_dispatch(
                "socket-session", device_id="esp32-s3"
            )
        assert "Hermes" not in str(expired.value)
        assert "DeepSeek" not in str(expired.value)
        assert len(client.calls) == calls_before_expired_confirmation

    asyncio.run(exercise())


def test_shared_prepare_enforces_one_pending_operation_per_device() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        await adapter.prepare_shared_dispatch(
            "socket-session-a", device_id="esp32-s3", prompt="first"
        )
        with pytest.raises(XiaozhiAdapterError, match="for this device"):
            await adapter.prepare_shared_dispatch(
                "socket-session-b", device_id="esp32-s3", prompt="second"
            )

        dispatch_calls = [
            call for call in client.calls if call["operation"] == "dispatch_shared"
        ]
        assert dispatch_calls == []

    asyncio.run(exercise())


def test_rebind_from_another_socket_invalidates_prepared_device_operation() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_shared_dispatch(
            "socket-session-a", device_id="esp32-s3", prompt="old target"
        )

        await adapter.bind_session(
            "socket-session-b",
            device_id="esp32-s3",
            host="hermes",
            profile="default",
            stored_session_id="stored-2",
        )

        with pytest.raises(XiaozhiAdapterError, match="no pending"):
            await adapter.confirm_shared_dispatch(
                "socket-session-a", device_id="esp32-s3"
            )
        assert not any(
            call["operation"] == "dispatch_shared" for call in client.calls
        )

    asyncio.run(exercise())


def test_expired_shared_prepare_is_pruned_before_reprepare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        now = {"value": 100.0}
        monkeypatch.setattr(
            xiaozhi_server_adapter.time,
            "monotonic",
            lambda: now["value"],
        )
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        await adapter.prepare_shared_dispatch(
            "socket-session-a", device_id="esp32-s3", prompt="expired"
        )
        now["value"] += 180.001
        prepared = await adapter.prepare_shared_dispatch(
            "socket-session-b", device_id="esp32-s3", prompt="replacement"
        )
        assert prepared["status"] == "confirmation_required"

        result = await adapter.confirm_shared_dispatch(
            "socket-session-b", device_id="esp32-s3"
        )
        assert result["status"] == "completed"
        assert client.calls[-1]["input"] == "replacement"

    asyncio.run(exercise())


def test_concurrent_shared_confirmation_dispatches_at_most_once() -> None:
    class BlockingBindingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.show_binding_calls = 0
            self.confirmation_reached_binding_check = asyncio.Event()
            self.release_binding_check = asyncio.Event()

        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "show_binding":
                self.show_binding_calls += 1
                if self.show_binding_calls == 2:
                    self.confirmation_reached_binding_check.set()
                    await self.release_binding_check.wait()
            return result

    async def exercise() -> None:
        client = BlockingBindingClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_shared_dispatch(
            "socket-session", device_id="esp32-s3", prompt="once"
        )

        first = asyncio.create_task(
            adapter.confirm_shared_dispatch("socket-session", device_id="esp32-s3")
        )
        await client.confirmation_reached_binding_check.wait()
        second = asyncio.create_task(
            adapter.confirm_shared_dispatch("socket-session", device_id="esp32-s3")
        )
        await asyncio.sleep(0)
        client.release_binding_check.set()
        results = await asyncio.gather(
            first,
            second,
            return_exceptions=True,
        )

        assert sum(isinstance(result, XiaozhiAdapterError) for result in results) == 1
        dispatch_calls = [
            call for call in client.calls if call["operation"] == "dispatch_shared"
        ]
        assert len(dispatch_calls) == 1

    asyncio.run(exercise())


def test_agent_catalog_and_deepseek_binding_include_explicit_host() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        catalog = await adapter.list_agents("socket-session")
        await adapter.list_sessions(
            "socket-session",
            host="deepseek-harness",
            profile="assistant-agent",
            limit=5,
        )
        await adapter.bind_session(
            "socket-session",
            device_id="esp32-s3",
            host="deepseek-harness",
            profile="assistant-agent",
            stored_session_id="stored-1",
        )

        assert len(catalog["agents"]) == 2
        assert client.calls[1]["host"] == "deepseek-harness"
        assert client.calls[2]["host"] == "deepseek-harness"

    asyncio.run(exercise())


def test_switch_or_cancel_invalidates_pending_command() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_dispatch("session-a", "first")
        await adapter.switch_profile("session-a", "assistant-agent")
        with pytest.raises(XiaozhiAdapterError, match="no pending"):
            await adapter.confirm_dispatch("session-a")

        await adapter.prepare_dispatch("session-a", "second")
        cancelled = await adapter.cancel_dispatch("session-a")
        assert cancelled["cancelled"] is True
        with pytest.raises(XiaozhiAdapterError, match="no pending"):
            await adapter.confirm_dispatch("session-a")

    asyncio.run(exercise())


def test_second_prepare_cannot_replace_the_command_awaiting_confirmation() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        await adapter.prepare_shared_dispatch(
            "socket-session", device_id="esp32-s3", prompt="first"
        )
        with pytest.raises(XiaozhiAdapterError, match="already exists"):
            await adapter.prepare_shared_dispatch(
                "socket-session", device_id="esp32-s3", prompt="second"
            )
        result = await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )

        assert result["output"] == "shared answer"
        assert client.calls[-1]["input"] == "first"

    asyncio.run(exercise())


def test_host_drift_rejects_confirmation_before_dispatch() -> None:
    class HostDriftClient(FakeClient):
        host = "hermes"

        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "show_binding":
                return {**result, "host": self.host}
            return result

    async def exercise() -> None:
        client = HostDriftClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_shared_dispatch(
            "socket-session", device_id="esp32-s3", prompt="first"
        )
        client.host = "deepseek-harness"

        with pytest.raises(XiaozhiAdapterError, match="binding changed"):
            await adapter.confirm_shared_dispatch(
                "socket-session", device_id="esp32-s3"
            )
        assert all(call["operation"] != "dispatch_shared" for call in client.calls)

    asyncio.run(exercise())


def test_prepare_and_confirm_new_session_is_two_step_and_idempotent() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        prepared = await adapter.prepare_new_session(
            "socket-session", device_id="esp32-s3"
        )
        assert prepared["operation"] == "prepare_new_session"
        assert [call["operation"] for call in client.calls] == ["show_binding"]

        result = await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )
        assert result["stored_session_id"] == "stored-new"
        assert [call["operation"] for call in client.calls] == [
            "show_binding",
            "show_binding",
            "create_session",
        ]
        assert client.calls[-1]["confirmed"] is True
        assert isinstance(client.calls[-1]["idempotency_key"], str)

        with pytest.raises(XiaozhiAdapterError, match="no pending"):
            await adapter.confirm_shared_dispatch(
                "socket-session", device_id="esp32-s3"
            )

    asyncio.run(exercise())


def test_prepare_and_confirm_compress_revalidates_binding() -> None:
    async def exercise() -> None:
        client = FakeClient()
        adapter = XiaozhiHermesAdapter(client)

        prepared = await adapter.prepare_compress_context(
            "socket-session", device_id="esp32-s3"
        )
        assert prepared["operation"] == "prepare_compress_context"

        result = await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )
        assert result["compression_status"] == "compressed"
        assert [call["operation"] for call in client.calls] == [
            "show_binding",
            "show_binding",
            "compress_session",
        ]

    asyncio.run(exercise())


def test_pending_compression_cannot_be_confirmed_twice() -> None:
    class PendingClient(FakeClient):
        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "compress_session":
                return {"status": "pending", "operation": "compress_session"}
            return result

    async def exercise() -> None:
        client = PendingClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_compress_context(
            "socket-session", device_id="esp32-s3"
        )

        result = await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )
        assert result["status"] == "pending"

        with pytest.raises(XiaozhiAdapterError, match="pending"):
            await adapter.confirm_shared_dispatch(
                "socket-session", device_id="esp32-s3"
            )
        assert [call["operation"] for call in client.calls].count("compress_session") == 1

    asyncio.run(exercise())


def test_unknown_lifecycle_result_cannot_be_confirmed_twice() -> None:
    class UnknownClient(FakeClient):
        async def request(self, operation: str, **kwargs):
            result = await super().request(operation, **kwargs)
            if operation == "create_session":
                return {"status": "outcome_unknown", "operation": "create_session"}
            return result

    async def exercise() -> None:
        client = UnknownClient()
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_new_session("socket-session", device_id="esp32-s3")

        result = await adapter.confirm_shared_dispatch(
            "socket-session", device_id="esp32-s3"
        )
        assert result["status"] == "outcome_unknown"

        with pytest.raises(XiaozhiAdapterError, match="outcome_unknown"):
            await adapter.confirm_shared_dispatch(
                "socket-session", device_id="esp32-s3"
            )
        assert [call["operation"] for call in client.calls].count("create_session") == 1

    asyncio.run(exercise())


def test_profile_drift_rejects_confirmation_without_dispatch() -> None:
    async def exercise() -> None:
        client = FakeClient(active_profile="default")
        adapter = XiaozhiHermesAdapter(client)
        await adapter.prepare_dispatch("session-a", "work")
        client.active_profile = "assistant-agent"

        with pytest.raises(XiaozhiAdapterError, match="active profile changed"):
            await adapter.confirm_dispatch("session-a")
        assert all(call["operation"] != "dispatch" for call in client.calls)

    asyncio.run(exercise())


def test_settings_require_existing_absolute_files_and_exact_digest(tmp_path: Path) -> None:
    python_path = tmp_path / "python.exe"
    script_path = tmp_path / "bridge.py"
    config_path = tmp_path / "bridge.json"
    for path in (python_path, script_path, config_path):
        path.write_text("placeholder", encoding="utf-8")
    environment = {
        "XIAOZHI_HERMES_BRIDGE_PYTHON": str(python_path),
        "XIAOZHI_HERMES_BRIDGE_SCRIPT": str(script_path),
        "XIAOZHI_HERMES_BRIDGE_CONFIG": str(config_path),
        "XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256": "a" * 64,
        "XIAOZHI_HERMES_CALLER_ID": "xiaozhi-server-v0.9.7",
    }

    settings = BridgeProcessSettings.from_environment(environment)

    assert settings.caller_id == "xiaozhi-server-v0.9.7"
    assert settings.response_timeout_seconds > 660
    assert settings.command[-1] == "a" * 64
    with pytest.raises(XiaozhiAdapterError, match="SHA-256"):
        BridgeProcessSettings.from_environment(
            environment | {"XIAOZHI_HERMES_BRIDGE_CONFIG_SHA256": "bad"}
        )


def test_real_private_stdio_lists_and_switches_profiles(tmp_path: Path) -> None:
    async def exercise() -> None:
        config = tmp_path / "bridge-config.json"
        config.write_text(
            """{
  "schema_version": "1.0",
  "trusted_caller_id": "xiaozhi-server-test",
  "default_profile": "default",
  "profiles": [
    {"name": "default", "api_server_base_url": "http://127.0.0.1:8642/p/default", "api_key_env": "SYNTHETIC_KEY"},
    {"name": "assistant-agent", "api_server_base_url": "http://127.0.0.1:8642/p/assistant-agent", "api_key_env": "SYNTHETIC_KEY"}
  ],
  "dispatch_enabled": false,
  "require_confirmation": true,
  "request_timeout_seconds": 2,
  "poll_interval_seconds": 1,
  "max_polls": 1,
  "max_input_chars": 100
}
""",
            encoding="utf-8",
        )
        digest = hashlib.sha256(config.read_bytes()).hexdigest()
        script = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "xiaozhi_hermes_bridge.py"
        )
        client = BridgeProcessClient(
            BridgeProcessSettings(
                command=(
                    sys.executable,
                    str(script),
                    "serve-stdio",
                    "--config",
                    str(config),
                    "--confirmed-config-sha256",
                    digest,
                ),
                caller_id="xiaozhi-server-test",
                response_timeout_seconds=5,
            )
        )
        try:
            listed = await client.request("list_profiles", session_id="web-session")
            switched = await client.request(
                "switch_profile", session_id="web-session", profile="assistant-agent"
            )
            assert listed["profiles"] == ["default", "assistant-agent"]
            assert switched["active_profile"] == "assistant-agent"
        finally:
            await client.close()

    asyncio.run(exercise())
