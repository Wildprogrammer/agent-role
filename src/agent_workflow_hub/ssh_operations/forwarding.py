from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


@dataclass
class ForwardHandle:
    listener: Any
    ready: Mapping[str, Any]
    closed: bool = False

    async def wait_closed(self) -> None:
        await self.listener.wait_closed()
        self.closed = True

    async def close(self) -> None:
        if self.closed:
            return
        self.listener.close()
        await self.listener.wait_closed()
        self.closed = True


class ForwardService:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @staticmethod
    def _ready(mode: str, host: str, listener: Any) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "status": "ready",
                "mode": mode,
                "listen_host": host,
                "listen_port": int(listener.get_port()),
            }
        )

    async def local(
        self,
        listen_host: str,
        listen_port: int,
        destination_host: str,
        destination_port: int,
    ) -> ForwardHandle:
        listener = await self.connection.forward_local_port(
            listen_host, listen_port, destination_host, destination_port
        )
        return ForwardHandle(listener, self._ready("local", listen_host, listener))

    async def remote(
        self,
        listen_host: str,
        listen_port: int,
        destination_host: str,
        destination_port: int,
    ) -> ForwardHandle:
        listener = await self.connection.forward_remote_port(
            listen_host, listen_port, destination_host, destination_port
        )
        return ForwardHandle(listener, self._ready("remote", listen_host, listener))

    async def socks(self, listen_host: str, listen_port: int) -> ForwardHandle:
        listener = await self.connection.forward_socks(listen_host, listen_port)
        return ForwardHandle(listener, self._ready("socks", listen_host, listener))
