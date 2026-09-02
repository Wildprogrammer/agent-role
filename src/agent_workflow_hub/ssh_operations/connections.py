from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from .host_keys import HostKeyMismatch, KnownHostStore, make_tofu_client
from .models import SSHConfig, TargetConfig


class ConnectionManager:
    def __init__(
        self,
        config: SSHConfig,
        known_hosts: KnownHostStore | None = None,
        *,
        connect_callable: Callable[..., Awaitable[Any]] | None = None,
        retries: int = 1,
        retry_delay: float = 0.05,
        non_retryable: tuple[type[BaseException], ...] | None = None,
    ) -> None:
        self.config = config
        self.known_hosts = known_hosts or KnownHostStore(config.known_hosts)
        self._connect_callable = connect_callable
        self.retries = retries
        self.retry_delay = retry_delay
        self.non_retryable = non_retryable or self._default_non_retryable()
        self.close_order: tuple[str, ...] = ()

    @staticmethod
    def _default_non_retryable() -> tuple[type[BaseException], ...]:
        types: list[type[BaseException]] = [HostKeyMismatch]
        try:
            import asyncssh
        except ImportError:
            return tuple(types)
        for name in ("PermissionDenied", "HostKeyNotVerifiable"):
            candidate = getattr(asyncssh, name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                types.append(candidate)
        return tuple(types)

    def _connector(self) -> Callable[..., Awaitable[Any]]:
        if self._connect_callable is not None:
            return self._connect_callable
        try:
            import asyncssh
        except ImportError as exc:
            raise RuntimeError("AsyncSSH is not installed") from exc
        return asyncssh.connect

    def _chain(self, name: str) -> tuple[TargetConfig, ...]:
        chain: list[TargetConfig] = []
        current: str | None = name
        while current:
            try:
                target = self.config.targets[current]
            except KeyError as exc:
                raise ValueError(f"unknown SSH target: {current}") from exc
            chain.append(target)
            current = target.via
        chain.reverse()
        return tuple(chain)

    def _options(self, target: TargetConfig, tunnel: Any | None) -> dict[str, Any]:
        options: dict[str, Any] = {
            "port": target.port,
            "username": target.username,
            "connect_timeout": target.timeout_seconds,
            "tunnel": tunnel,
            # A truthy tuple of empty trust sets prevents AsyncSSH from falling
            # back to ~/.ssh/known_hosts while delegating each key decision to
            # the workflow's TOFU callback.
            "known_hosts": ((), (), ()),
            "client_factory": lambda: make_tofu_client(self.known_hosts, target.name),
            "agent_forwarding": target.forward_agent,
        }
        if target.auth in {"password", "auto"} and target.password is not None:
            options["password"] = target.password
        else:
            options["password"] = None
        if target.auth in {"key", "auto"} and target.private_key is not None:
            options["client_keys"] = [str(target.private_key)]
            options["passphrase"] = target.private_key_passphrase
            if target.auth == "key":
                options["agent_path"] = None
        elif target.auth == "password":
            options["client_keys"] = None
            options["agent_path"] = None
        elif target.auth == "agent":
            options["client_keys"] = []
        return options

    async def _open(self, target: TargetConfig, tunnel: Any | None) -> Any:
        connector = self._connector()
        options = self._options(target, tunnel)
        for attempt in range(self.retries + 1):
            try:
                return await connector(target.host, **options)
            except self.non_retryable:
                raise
            except OSError:
                if attempt >= self.retries:
                    raise
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        raise AssertionError("unreachable")

    @asynccontextmanager
    async def connect(self, name: str) -> AsyncIterator[Any]:
        opened: list[tuple[str, Any]] = []
        self.close_order = ()
        try:
            tunnel: Any | None = None
            for target in self._chain(name):
                connection = await self._open(target, tunnel)
                opened.append((target.name, connection))
                tunnel = connection
            yield opened[-1][1]
        finally:
            closed: list[str] = []
            for target_name, connection in reversed(opened):
                connection.close()
                try:
                    await connection.wait_closed()
                finally:
                    closed.append(target_name)
            self.close_order = tuple(closed)
