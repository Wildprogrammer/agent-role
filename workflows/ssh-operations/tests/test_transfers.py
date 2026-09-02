from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_workflow_hub.ssh_operations.transfers import (
    HighImpactConfirmationRequired,
    SCPService,
    SFTPService,
)


class FakeSFTP:
    def __init__(self):
        self.calls = []
        self.files = {"/data/item.txt": b"old"}

    async def remove(self, path):
        self.calls.append(("remove", path))
        del self.files[path]

    async def rmdir(self, path):
        self.calls.append(("rmdir", path))

    async def mkdir(self, path):
        self.calls.append(("mkdir", path))

    async def rename(self, oldpath, newpath):
        self.calls.append(("rename", oldpath, newpath))

    async def chmod(self, path, mode):
        self.calls.append(("chmod", path, mode))

    async def symlink(self, oldpath, newpath):
        self.calls.append(("symlink", oldpath, newpath))

    async def readlink(self, path):
        self.calls.append(("readlink", path))
        return "/target"

    async def listdir(self, path):
        return ["a", "b"]

    async def stat(self, path):
        return {"path": path, "follow": True}

    async def lstat(self, path):
        return {"path": path, "follow": False}


class FakeConnection:
    def __init__(self, sftp):
        self.sftp = sftp

    async def start_sftp_client(self):
        return self.sftp


@pytest.mark.anyio
async def test_sftp_delete_requires_one_high_impact_marker() -> None:
    sftp = FakeSFTP()
    service = SFTPService(FakeConnection(sftp))
    with pytest.raises(HighImpactConfirmationRequired):
        await service.remove("/data/item.txt", confirmed_high_impact=False)
    await service.remove("/data/item.txt", confirmed_high_impact=True)
    assert sftp.calls == [("remove", "/data/item.txt")]


@pytest.mark.anyio
async def test_sftp_common_management_functions_are_direct() -> None:
    sftp = FakeSFTP()
    service = SFTPService(FakeConnection(sftp))
    assert await service.listdir("/data") == ["a", "b"]
    assert (await service.stat("/data/a"))["follow"] is True
    assert (await service.lstat("/data/a"))["follow"] is False
    await service.mkdir("/new")
    await service.rename("/old", "/new")
    await service.chmod("/new", 0o750)
    await service.symlink("/target", "/link")
    assert await service.readlink("/link") == "/target"


@pytest.mark.anyio
async def test_scp_uses_connection_tuple_and_never_claims_resume(tmp_path: Path) -> None:
    local = tmp_path / "a.txt"
    local.write_text("a", encoding="utf-8")
    calls = []

    async def fake_scp(source, destination, **kwargs):
        calls.append((source, destination, kwargs))

    connection = object()
    result = await SCPService(connection, scp_callable=fake_scp).upload(
        [local], "/remote/", recurse=True, preserve=True
    )
    assert result.status == "success"
    assert result.resume_supported is False
    assert calls[0][1] == (connection, "/remote/")


@pytest.mark.anyio
async def test_scp_download_reports_final_progress_bytes(tmp_path: Path) -> None:
    async def fake_scp(source, destination, **kwargs):
        progress = kwargs["progress_handler"]
        progress(b"/remote/a.txt", b"a.txt", 2, 5)
        progress(b"/remote/a.txt", b"a.txt", 5, 5)
        progress(b"/remote/b.txt", b"b.txt", 3, 3)

    result = await SCPService(object(), scp_callable=fake_scp).download(
        ["/remote/a.txt", "/remote/b.txt"], tmp_path, recurse=True
    )

    assert result.status == "success"
    assert result.bytes_transferred == 8
    assert result.resume_supported is False


@pytest.mark.anyio
async def test_scp_interruption_is_partial_without_resume(tmp_path: Path) -> None:
    local = tmp_path / "a.txt"
    local.write_text("a", encoding="utf-8")

    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    result = await SCPService(object(), scp_callable=cancelled).upload(
        [local], "/remote/"
    )
    assert result.status == "partial"
    assert result.resume_supported is False
