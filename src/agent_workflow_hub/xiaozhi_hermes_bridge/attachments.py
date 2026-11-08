"""Short-lived, server-local attachment staging for the XiaoAi MCP facade."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MIB = 1024 * 1024
MAX_ATTACHMENT_BYTES = 50 * MIB
MAX_HERMES_IMAGE_BYTES = 25 * MIB
MAX_ATTACHMENTS_PER_MESSAGE = 4
MAX_MESSAGE_ATTACHMENT_BYTES = 100 * MIB

_ATTACHMENT_ID_RE = re.compile(r"^att_[a-f0-9]{32}$")
_UPLOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._()\-\u4e00-\u9fff]+")

_ALLOWED_SUFFIXES: Mapping[str, tuple[str, str]] = {
    ".jpg": ("image/jpeg", "image"),
    ".jpeg": ("image/jpeg", "image"),
    ".png": ("image/png", "image"),
    ".webp": ("image/webp", "image"),
    ".pdf": ("application/pdf", "pdf"),
    ".txt": ("text/plain", "text"),
    ".md": ("text/markdown", "text"),
    ".csv": ("text/csv", "text"),
    ".json": ("application/json", "text"),
}


class AttachmentError(ValueError):
    """A public attachment request cannot be completed safely."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AttachmentBinding:
    device_id: str
    host: str
    profile: str
    stored_session_id: str
    binding_revision: int


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    attachment_id: str
    path: Path
    name: str
    media_type: str
    kind: str
    bytes: int
    sha256: str
    binding: AttachmentBinding
    expires_at: float

    def to_bridge_mapping(self) -> dict[str, object]:
        return {
            "attachment_id": self.attachment_id,
            "path": str(self.path),
            "name": self.name,
            "media_type": self.media_type,
            "kind": self.kind,
            "bytes": self.bytes,
            "sha256": self.sha256,
        }


@dataclass(slots=True)
class _UploadGrant:
    token: str
    attachment_id: str
    request_id: str
    binding: AttachmentBinding
    expires_at: float
    uploading: bool = False
    uploaded: bool = False


@dataclass(frozen=True, slots=True)
class UploadGrantView:
    attachment_id: str
    token: str
    expires_at: float
    binding: AttachmentBinding


class AttachmentStore:
    """Own upload grants and temporary files without persisting credentials."""

    def __init__(
        self,
        root: Path,
        *,
        max_attachment_bytes: int = MAX_ATTACHMENT_BYTES,
        upload_ttl_seconds: int = 600,
        attachment_ttl_seconds: int = 1800,
        max_grants: int = 64,
        max_attachments: int = 128,
    ) -> None:
        if not root.is_absolute():
            raise AttachmentError("attachment root must be absolute")
        if not 1 <= max_attachment_bytes <= MAX_ATTACHMENT_BYTES:
            raise AttachmentError("attachment byte limit is invalid")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._max_attachment_bytes = max_attachment_bytes
        self._upload_ttl_seconds = upload_ttl_seconds
        self._attachment_ttl_seconds = attachment_ttl_seconds
        self._max_grants = max_grants
        self._max_attachments = max_attachments
        self._grants: OrderedDict[str, _UploadGrant] = OrderedDict()
        self._grant_requests: dict[str, str] = {}
        self._attachments: OrderedDict[str, StagedAttachment] = OrderedDict()
        self._attachment_claims: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._remove_stale_files()

    @property
    def max_attachment_bytes(self) -> int:
        return self._max_attachment_bytes

    def _remove_stale_files(self) -> None:
        cutoff = time.time() - 3600
        for path in self.root.glob("att_*"):
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue

    async def prepare(
        self, *, request_id: str, binding: AttachmentBinding
    ) -> UploadGrantView:
        now = time.time()
        async with self._lock:
            self._prune_locked(now)
            prior_token = self._grant_requests.get(request_id)
            prior = self._grants.get(prior_token or "")
            if prior is not None:
                if prior.binding != binding:
                    raise AttachmentError(
                        "同一 request_id 不能用于不同的会话绑定。", status_code=409
                    )
                if prior.expires_at > now:
                    return self._view(prior)
            token = _token()
            grant = _UploadGrant(
                token=token,
                attachment_id=f"att_{uuid.uuid4().hex}",
                request_id=request_id,
                binding=binding,
                expires_at=now + self._upload_ttl_seconds,
            )
            self._grants[token] = grant
            self._grant_requests[request_id] = token
            while len(self._grants) > self._max_grants:
                _, removed = self._grants.popitem(last=False)
                if self._grant_requests.get(removed.request_id) == removed.token:
                    self._grant_requests.pop(removed.request_id, None)
            return self._view(grant)

    @staticmethod
    def _view(grant: _UploadGrant) -> UploadGrantView:
        return UploadGrantView(
            attachment_id=grant.attachment_id,
            token=grant.token,
            expires_at=grant.expires_at,
            binding=grant.binding,
        )

    async def inspect_grant(self, token: str) -> UploadGrantView:
        now = time.time()
        async with self._lock:
            self._prune_locked(now)
            grant = self._grant(token)
            if grant.expires_at <= now:
                raise AttachmentError("上传链接已过期。", status_code=410)
            if grant.uploaded:
                raise AttachmentError("上传链接已经使用。", status_code=409)
            return self._view(grant)

    async def upload(
        self,
        *,
        token: str,
        name: str,
        declared_media_type: str,
        receive: Any,
        content_length: int | None,
        timeout_seconds: int = 120,
        idle_timeout_seconds: float | None = None,
    ) -> StagedAttachment:
        clean_name, media_type, kind = _classify_name(name, declared_media_type)
        grant = await self._reserve_grant(token)
        attachment_id = grant.attachment_id
        temporary = self.root / f"{attachment_id}.uploading"
        final = self.root / f"{attachment_id}.bin"
        if content_length is not None and content_length > self._max_attachment_bytes:
            await self._release_grant(grant)
            raise AttachmentError("文件超过 50 MiB 限制。", status_code=413)
        if (
            content_length is not None
            and grant.binding.host == "hermes"
            and kind == "image"
            and content_length > MAX_HERMES_IMAGE_BYTES
        ):
            await self._release_grant(grant)
            raise AttachmentError(
                "Hermes 单张图片最大支持 25 MiB。", status_code=413
            )
        digest = hashlib.sha256()
        size = 0
        head = bytearray()
        deadline = time.monotonic() + timeout_seconds
        try:
            with temporary.open("xb") as stream:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AttachmentError("上传超时。", status_code=408)
                    try:
                        read_timeout = (
                            min(remaining, idle_timeout_seconds)
                            if idle_timeout_seconds is not None else remaining
                        )
                        message = await asyncio.wait_for(receive(), timeout=read_timeout)
                    except asyncio.TimeoutError:
                        raise AttachmentError("上传超时。", status_code=408) from None
                    message_type = message.get("type")
                    if message_type == "http.disconnect":
                        raise AttachmentError("上传连接已断开。", status_code=499)
                    if message_type != "http.request":
                        raise AttachmentError("上传数据流无效。")
                    chunk = message.get("body", b"")
                    if not isinstance(chunk, bytes):
                        raise AttachmentError("上传数据无效。")
                    size += len(chunk)
                    if size > self._max_attachment_bytes:
                        raise AttachmentError("文件超过 50 MiB 限制。", status_code=413)
                    if len(head) < 8192:
                        head.extend(chunk[: 8192 - len(head)])
                    digest.update(chunk)
                    stream.write(chunk)
                    if not message.get("more_body", False):
                        break
                stream.flush()
                os.fsync(stream.fileno())
            if size == 0:
                raise AttachmentError("文件不能为空。")
            if (
                grant.binding.host == "hermes"
                and kind == "image"
                and size > MAX_HERMES_IMAGE_BYTES
            ):
                raise AttachmentError(
                    "Hermes 单张图片最大支持 25 MiB。", status_code=413
                )
            _validate_magic(kind, media_type, bytes(head), temporary)
            temporary.replace(final)
            record = StagedAttachment(
                attachment_id=attachment_id,
                path=final,
                name=clean_name,
                media_type=media_type,
                kind=kind,
                bytes=size,
                sha256=digest.hexdigest(),
                binding=grant.binding,
                expires_at=time.time() + self._attachment_ttl_seconds,
            )
            async with self._lock:
                grant.uploading = False
                grant.uploaded = True
                self._attachments[attachment_id] = record
                while len(self._attachments) > self._max_attachments:
                    _, removed = self._attachments.popitem(last=False)
                    self._attachment_claims.pop(removed.attachment_id, None)
                    _unlink(removed.path)
            return record
        except BaseException:
            _unlink(temporary)
            _unlink(final)
            await self._release_grant(grant)
            raise

    async def _reserve_grant(self, token: str) -> _UploadGrant:
        now = time.time()
        async with self._lock:
            self._prune_locked(now)
            grant = self._grant(token)
            if grant.expires_at <= now:
                raise AttachmentError("上传链接已过期。", status_code=410)
            if grant.uploaded or grant.uploading:
                raise AttachmentError("上传链接已经使用。", status_code=409)
            grant.uploading = True
            return grant

    async def _release_grant(self, grant: _UploadGrant) -> None:
        async with self._lock:
            current = self._grants.get(grant.token)
            if current is grant and not current.uploaded:
                current.uploading = False

    def _grant(self, token: str) -> _UploadGrant:
        if _UPLOAD_TOKEN_RE.fullmatch(token) is None:
            raise AttachmentError("上传凭证无效。", status_code=401)
        for candidate, grant in self._grants.items():
            if hmac.compare_digest(candidate, token):
                return grant
        raise AttachmentError("上传凭证无效或已过期。", status_code=401)

    async def resolve(
        self,
        attachment_ids: Sequence[str],
        *,
        binding: AttachmentBinding,
        request_id: str,
    ) -> tuple[StagedAttachment, ...]:
        if len(attachment_ids) > MAX_ATTACHMENTS_PER_MESSAGE:
            raise AttachmentError("每条消息最多携带 4 个附件。")
        if len(set(attachment_ids)) != len(attachment_ids):
            raise AttachmentError("附件 ID 不能重复。")
        now = time.time()
        async with self._lock:
            self._prune_locked(now)
            records: list[StagedAttachment] = []
            for attachment_id in attachment_ids:
                if _ATTACHMENT_ID_RE.fullmatch(attachment_id) is None:
                    raise AttachmentError("附件 ID 格式无效。")
                record = self._attachments.get(attachment_id)
                if record is None or record.expires_at <= now:
                    raise AttachmentError("附件不存在、未上传完成或已经过期。", status_code=410)
                if record.binding != binding:
                    raise AttachmentError("附件与当前会话绑定不匹配。", status_code=409)
                if record.path.is_symlink() or not record.path.is_file():
                    raise AttachmentError("附件临时文件已经不可用。", status_code=410)
                records.append(record)
            if sum(item.bytes for item in records) > MAX_MESSAGE_ATTACHMENT_BYTES:
                raise AttachmentError("单条消息的附件总量不能超过 100 MiB。")
            for attachment_id in attachment_ids:
                claimed_by = self._attachment_claims.get(attachment_id)
                if claimed_by is not None and not hmac.compare_digest(
                    claimed_by, request_id
                ):
                    raise AttachmentError(
                        "附件已经归属于另一条发送请求。", status_code=409
                    )
            for attachment_id in attachment_ids:
                self._attachment_claims[attachment_id] = request_id
            return tuple(records)

    async def remove(self, attachment_ids: Sequence[str]) -> None:
        paths: list[Path] = []
        async with self._lock:
            for attachment_id in attachment_ids:
                record = self._attachments.pop(attachment_id, None)
                self._attachment_claims.pop(attachment_id, None)
                if record is not None:
                    paths.append(record.path)
        for path in paths:
            _unlink(path)

    def _prune_locked(self, now: float) -> None:
        expired_tokens = [
            token
            for token, grant in self._grants.items()
            if grant.expires_at <= now and not grant.uploading
        ]
        for token in expired_tokens:
            grant = self._grants.pop(token)
            if self._grant_requests.get(grant.request_id) == token:
                self._grant_requests.pop(grant.request_id, None)
        expired_ids = [
            attachment_id
            for attachment_id, record in self._attachments.items()
            if record.expires_at <= now
        ]
        for attachment_id in expired_ids:
            self._attachment_claims.pop(attachment_id, None)
            _unlink(self._attachments.pop(attachment_id).path)

    async def cleanup_expired(self) -> None:
        """Remove expired in-memory records even while the public endpoint is idle."""

        async with self._lock:
            self._prune_locked(time.time())


def _token() -> str:
    import base64

    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")


def _sanitize_name(name: str) -> str:
    leaf = Path(name.replace("\\", "/")).name.strip().strip(".")
    leaf = _SAFE_NAME_RE.sub("_", leaf)[:160]
    if not leaf or leaf in {".", ".."}:
        raise AttachmentError("文件名无效。")
    return leaf


def _classify_name(name: str, declared_media_type: str) -> tuple[str, str, str]:
    clean = _sanitize_name(name)
    suffix = Path(clean).suffix.lower()
    allowed = _ALLOWED_SUFFIXES.get(suffix)
    if allowed is None:
        raise AttachmentError("不支持这个文件类型。")
    media_type, kind = allowed
    declared = declared_media_type.split(";", 1)[0].strip().lower()
    aliases = {media_type}
    if kind == "text":
        aliases.add("text/plain")
        aliases.add("application/octet-stream")
    if declared and declared not in aliases:
        raise AttachmentError("文件扩展名与声明类型不一致。")
    return clean, media_type, kind


def _validate_magic(kind: str, media_type: str, head: bytes, path: Path) -> None:
    valid = True
    if media_type == "image/jpeg":
        valid = head.startswith(b"\xff\xd8\xff")
    elif media_type == "image/png":
        valid = head.startswith(b"\x89PNG\r\n\x1a\n")
    elif media_type == "image/webp":
        valid = len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    elif media_type == "application/pdf":
        valid = head.startswith(b"%PDF-")
    elif kind == "text":
        valid = _is_utf8_text(path)
    if not valid:
        raise AttachmentError("文件内容与允许的类型不匹配。")


def _is_utf8_text(path: Path) -> bool:
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(64 * 1024):
                if b"\x00" in chunk:
                    return False
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except (OSError, UnicodeDecodeError):
        return False
    return True


def _unlink(path: Path) -> None:
    try:
        if path.is_file() and not path.is_symlink():
            path.unlink()
    except OSError:
        return


__all__ = [
    "AttachmentBinding",
    "AttachmentError",
    "AttachmentStore",
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENTS_PER_MESSAGE",
    "MAX_HERMES_IMAGE_BYTES",
    "StagedAttachment",
    "UploadGrantView",
]
