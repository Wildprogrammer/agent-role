from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable


class HighImpactConfirmationRequired(PermissionError):
    pass


@dataclass(frozen=True)
class TransferResult:
    status: str
    files_completed: tuple[str, ...] = ()
    bytes_transferred: int = 0
    resume_supported: bool = True
    error: str | None = None


class SFTPService:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        self._sftp: Any | None = None

    async def _client(self) -> Any:
        if self._sftp is None:
            self._sftp = await self.connection.start_sftp_client()
        return self._sftp

    async def close(self) -> None:
        if self._sftp is not None:
            self._sftp.exit()
            if hasattr(self._sftp, "wait_closed"):
                await self._sftp.wait_closed()
            self._sftp = None

    async def listdir(self, path: str) -> Any:
        return await (await self._client()).listdir(path)

    async def stat(self, path: str) -> Any:
        return await (await self._client()).stat(path)

    async def lstat(self, path: str) -> Any:
        return await (await self._client()).lstat(path)

    async def read(self, path: str) -> bytes:
        handle = await (await self._client()).open(path, "rb")
        try:
            return await handle.read()
        finally:
            await handle.close()

    async def write(self, path: str, data: bytes, *, overwrite: bool = False) -> int:
        if not overwrite and await self._exists(path):
            raise FileExistsError(path)
        handle = await (await self._client()).open(path, "wb")
        try:
            await handle.write(data)
            return len(data)
        finally:
            await handle.close()

    async def mkdir(self, path: str) -> None:
        await (await self._client()).mkdir(path)

    async def rename(self, oldpath: str, newpath: str) -> None:
        await (await self._client()).rename(oldpath, newpath)

    move = rename

    async def chmod(self, path: str, mode: int) -> None:
        await (await self._client()).chmod(path, mode)

    async def symlink(self, oldpath: str, newpath: str) -> None:
        await (await self._client()).symlink(oldpath, newpath)

    async def readlink(self, path: str) -> str:
        return await (await self._client()).readlink(path)

    async def remove(self, path: str, *, confirmed_high_impact: bool) -> None:
        if not confirmed_high_impact:
            raise HighImpactConfirmationRequired("remote delete requires one confirmation")
        await (await self._client()).remove(path)

    async def rmdir(self, path: str, *, confirmed_high_impact: bool) -> None:
        if not confirmed_high_impact:
            raise HighImpactConfirmationRequired("remote delete requires one confirmation")
        await (await self._client()).rmdir(path)

    async def _exists(self, path: str) -> bool:
        try:
            await (await self._client()).stat(path)
            return True
        except Exception as exc:
            if (
                isinstance(exc, FileNotFoundError)
                or "NoSuchFile" in type(exc).__name__
            ):
                return False
            raise

    async def upload(
        self,
        source: Path,
        destination: str,
        *,
        request_id: str,
        overwrite: bool = False,
        resume: bool = True,
        chunk_size: int = 256 * 1024,
    ) -> TransferResult:
        source = source.resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        if not overwrite and await self._exists(destination):
            raise FileExistsError(destination)
        temporary = f"{destination}.agent-workflow-hub-{request_id}.part"
        offset = 0
        if resume and await self._exists(temporary):
            attributes = await self.stat(temporary)
            offset = int(getattr(attributes, "size", 0) or 0)
            if offset > source.stat().st_size:
                raise ValueError("remote partial is larger than local source")
        sftp = await self._client()
        remote = await sftp.open(temporary, "ab" if offset else "wb")
        transferred = offset
        try:
            with source.open("rb") as local:
                local.seek(offset)
                while chunk := local.read(chunk_size):
                    await remote.write(chunk)
                    transferred += len(chunk)
        except asyncio.CancelledError:
            return TransferResult("partial", bytes_transferred=transferred)
        finally:
            await remote.close()
        if overwrite and await self._exists(destination):
            try:
                await sftp.posix_rename(temporary, destination)
            except (AttributeError, OSError):
                await sftp.remove(destination)
                await sftp.rename(temporary, destination)
        else:
            await sftp.rename(temporary, destination)
        return TransferResult("success", (str(source),), transferred)

    async def download(
        self,
        source: str,
        destination: Path,
        *,
        request_id: str,
        overwrite: bool = False,
        resume: bool = True,
        chunk_size: int = 256 * 1024,
    ) -> TransferResult:
        destination = destination.resolve()
        if destination.exists() and not overwrite:
            raise FileExistsError(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            destination.name + f".agent-workflow-hub-{request_id}.part"
        )
        offset = temporary.stat().st_size if resume and temporary.exists() else 0
        remote = await (await self._client()).open(source, "rb")
        transferred = offset
        try:
            if offset:
                await remote.seek(offset)
            with temporary.open("ab" if offset else "wb") as local:
                while chunk := await remote.read(chunk_size):
                    local.write(chunk)
                    transferred += len(chunk)
        except asyncio.CancelledError:
            return TransferResult("partial", bytes_transferred=transferred)
        finally:
            await remote.close()
        os.replace(temporary, destination)
        return TransferResult("success", (source,), transferred)


class SCPService:
    def __init__(
        self,
        connection: Any,
        *,
        scp_callable: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.connection = connection
        self._scp_callable = scp_callable

    def _scp(self) -> Callable[..., Awaitable[Any]]:
        if self._scp_callable is not None:
            return self._scp_callable
        try:
            import asyncssh
        except ImportError as exc:
            raise RuntimeError("AsyncSSH is not installed") from exc
        return asyncssh.scp

    async def upload(
        self,
        sources: Iterable[Path],
        destination: str,
        *,
        recurse: bool = False,
        preserve: bool = False,
    ) -> TransferResult:
        local_sources = tuple(Path(item).resolve() for item in sources)
        try:
            await self._scp()(
                local_sources,
                (self.connection, destination),
                recurse=recurse,
                preserve=preserve,
            )
        except asyncio.CancelledError:
            return TransferResult("partial", resume_supported=False, error="cancelled")
        except Exception as exc:
            return TransferResult("partial", resume_supported=False, error=str(exc))
        return TransferResult(
            "success", tuple(str(item) for item in local_sources),
            sum(item.stat().st_size for item in local_sources if item.is_file()),
            resume_supported=False,
        )

    async def download(
        self,
        sources: Iterable[str],
        destination: Path,
        *,
        recurse: bool = False,
        preserve: bool = False,
    ) -> TransferResult:
        remote_sources = tuple((self.connection, item) for item in sources)
        progress: dict[tuple[Any, Any], int] = {}

        def record_progress(
            source_path: Any,
            destination_path: Any,
            bytes_copied: int,
            _total_bytes: int,
        ) -> None:
            progress[(source_path, destination_path)] = bytes_copied

        try:
            await self._scp()(
                remote_sources,
                destination.resolve(),
                recurse=recurse,
                preserve=preserve,
                progress_handler=record_progress,
            )
        except asyncio.CancelledError:
            return TransferResult(
                "partial",
                bytes_transferred=sum(progress.values()),
                resume_supported=False,
                error="cancelled",
            )
        except Exception as exc:
            return TransferResult(
                "partial",
                bytes_transferred=sum(progress.values()),
                resume_supported=False,
                error=str(exc),
            )
        return TransferResult(
            "success",
            tuple(item[1] for item in remote_sources),
            sum(progress.values()),
            resume_supported=False,
        )
