from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from agent_workflow_hub.xiaozhi_hermes_bridge.attachments import (
    AttachmentBinding,
    AttachmentError,
    AttachmentStore,
)


def binding(*, revision: int = 1, host: str = "hermes") -> AttachmentBinding:
    return AttachmentBinding(
        device_id="xiaoai-phone",
        host=host,
        profile="default",
        stored_session_id="stored-1",
        binding_revision=revision,
    )


def receiver(*chunks: bytes):
    messages = [
        {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks) - 1,
        }
        for index, chunk in enumerate(chunks)
    ]

    async def receive() -> Mapping[str, object]:
        return messages.pop(0)

    return receive


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["idle", "total", "disconnect"])
async def test_interrupted_upload_cleans_partial_and_allows_manual_retry(tmp_path, mode):
    store = AttachmentStore(tmp_path)
    grant = await store.prepare(request_id="interrupted", binding=binding())
    calls = 0

    async def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"type": "http.request", "body": b"partial", "more_body": True}
        if mode == "disconnect":
            return {"type": "http.disconnect"}
        if mode == "idle":
            await asyncio.Event().wait()
        await asyncio.sleep(0.01)
        return {"type": "http.request", "body": b"x", "more_body": True}

    with pytest.raises(AttachmentError) as error:
        await store.upload(token=grant.token, name="note.txt", declared_media_type="text/plain",
                           receive=receive, content_length=None,
                           timeout_seconds=0.15 if mode == "total" else 2,
                           idle_timeout_seconds=1 if mode == "total" else 0.05)
    assert error.value.status_code == (499 if mode == "disconnect" else 408)
    assert list(tmp_path.iterdir()) == []
    # Recovery is explicit; the store never replays a request itself.
    record = await store.upload(token=grant.token, name="note.txt", declared_media_type="text/plain",
                                receive=receiver(b"retry"), content_length=5)
    assert record.path.read_bytes() == b"retry"


@pytest.mark.asyncio
async def test_idle_deadline_is_per_read_and_default_timeout_is_preserved(tmp_path):
    import inspect
    assert inspect.signature(AttachmentStore.upload).parameters["timeout_seconds"].default == 120
    assert inspect.signature(AttachmentStore.upload).parameters["idle_timeout_seconds"].default is None
    store = AttachmentStore(tmp_path)
    grant = await store.prepare(request_id="slow-progress", binding=binding())
    chunks = receiver(*([b"x"] * 8))
    async def receive():
        await asyncio.sleep(0.04)
        return await chunks()
    record = await store.upload(token=grant.token, name="note.txt", declared_media_type="text/plain",
                                receive=receive, content_length=8, timeout_seconds=3,
                                idle_timeout_seconds=0.25)
    assert record.path.read_bytes() == b"xxxxxxxx"


@pytest.mark.asyncio
async def test_stream_limit_rejects_without_leaving_a_file(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_attachment_bytes=8)
    grant = await store.prepare(request_id="upload-1", binding=binding())

    with pytest.raises(AttachmentError, match="50 MiB"):
        await store.upload(
            token=grant.token,
            name="note.txt",
            declared_media_type="text/plain",
            receive=receiver(b"1234", b"56789"),
            content_length=None,
        )

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_exact_limit_and_binding_guard(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_attachment_bytes=8)
    grant = await store.prepare(
        request_id="upload-1", binding=binding(host="deepseek-harness")
    )
    record = await store.upload(
        token=grant.token,
        name="note.txt",
        declared_media_type="text/plain",
        receive=receiver(b"12345678"),
        content_length=8,
    )

    assert record.bytes == 8
    assert record.sha256
    with pytest.raises(AttachmentError, match="绑定不匹配"):
        await store.resolve(
            [record.attachment_id],
            binding=binding(host="deepseek-harness", revision=2),
            request_id="send-1",
        )
    assert await store.resolve(
        [record.attachment_id],
        binding=binding(host="deepseek-harness"),
        request_id="send-1",
    ) == (record,)


@pytest.mark.asyncio
async def test_magic_type_and_filename_are_checked(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_attachment_bytes=1024)
    grant = await store.prepare(request_id="upload-1", binding=binding())
    with pytest.raises(AttachmentError, match="内容"):
        await store.upload(
            token=grant.token,
            name="photo.png",
            declared_media_type="image/png",
            receive=receiver(b"not a png"),
            content_length=9,
        )

    second = await store.prepare(request_id="upload-2", binding=binding())
    with pytest.raises(AttachmentError, match="文件类型"):
        await store.upload(
            token=second.token,
            name="payload.exe",
            declared_media_type="application/octet-stream",
            receive=receiver(b"MZ"),
            content_length=2,
        )


@pytest.mark.asyncio
async def test_one_time_grant_cannot_be_reused(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_attachment_bytes=1024)
    grant = await store.prepare(request_id="upload-1", binding=binding())
    await store.upload(
        token=grant.token,
        name="note.txt",
        declared_media_type="text/plain",
        receive=receiver(b"ok"),
        content_length=2,
    )
    replayed = await store.prepare(request_id="upload-1", binding=binding())
    assert replayed.attachment_id == grant.attachment_id
    assert replayed.token == grant.token
    with pytest.raises(AttachmentError, match="已经使用"):
        await store.upload(
            token=grant.token,
            name="note.txt",
            declared_media_type="text/plain",
            receive=receiver(b"again"),
            content_length=5,
        )


@pytest.mark.asyncio
async def test_attachment_is_claimed_by_one_send_request(tmp_path: Path) -> None:
    store = AttachmentStore(tmp_path, max_attachment_bytes=1024)
    grant = await store.prepare(request_id="upload-1", binding=binding())
    record = await store.upload(
        token=grant.token,
        name="note.txt",
        declared_media_type="text/plain",
        receive=receiver(b"ok"),
        content_length=2,
    )

    assert await store.resolve(
        [record.attachment_id], binding=binding(), request_id="send-1"
    ) == (record,)
    assert await store.resolve(
        [record.attachment_id], binding=binding(), request_id="send-1"
    ) == (record,)
    with pytest.raises(AttachmentError, match="另一条发送请求"):
        await store.resolve(
            [record.attachment_id], binding=binding(), request_id="send-2"
        )


@pytest.mark.asyncio
async def test_idle_cleanup_removes_expired_attachment(tmp_path: Path) -> None:
    store = AttachmentStore(
        tmp_path,
        max_attachment_bytes=1024,
        attachment_ttl_seconds=0,
    )
    grant = await store.prepare(request_id="upload-1", binding=binding())
    record = await store.upload(
        token=grant.token,
        name="note.txt",
        declared_media_type="text/plain",
        receive=receiver(b"ok"),
        content_length=2,
    )

    await store.cleanup_expired()

    assert not record.path.exists()
