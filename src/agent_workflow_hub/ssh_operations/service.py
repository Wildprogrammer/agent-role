from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Awaitable, Callable, Iterable, Mapping

from .commands import CommandExecutor
from .models import CommandResult, OperationResult, SSHConfig, StepSpec
from .shells import classify_probe, shell_adapter
from .transfers import HighImpactConfirmationRequired


def classify_operation(
    operation: str,
    *,
    sudo: bool = False,
    overwrite: bool = False,
    explicit_high_impact: bool = False,
) -> str:
    del sudo, overwrite
    if explicit_high_impact or operation.casefold() in {
        "delete", "remove", "rmdir", "recursive-delete"
    }:
        return "high-impact"
    return "ordinary"


def redact_values(value: Any, secrets: Iterable[str]) -> Any:
    active = tuple(secret for secret in secrets if secret)
    if isinstance(value, str):
        for secret in active:
            value = value.replace(secret, "***")
        return value
    if isinstance(value, Mapping):
        return {key: redact_values(item, active) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_values(item, active) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_values(item, active) for item in value)
    return value


class SSHOperationsService:
    def __init__(
        self,
        config: SSHConfig | None = None,
        connection_manager: Any | None = None,
        *,
        operation_worker: Callable[[str], Awaitable[CommandResult]] | None = None,
    ) -> None:
        self.config = config
        self.connection_manager = connection_manager
        self.operation_worker = operation_worker

    @staticmethod
    def require_confirmation(
        operation: str,
        *,
        confirmed: bool,
        explicit_high_impact: bool = False,
    ) -> None:
        if (
            classify_operation(operation, explicit_high_impact=explicit_high_impact)
            == "high-impact"
            and not confirmed
        ):
            raise HighImpactConfirmationRequired(
                "high-impact operation requires one confirmation"
            )

    def resolve_targets(self, names: Iterable[str]) -> tuple[str, ...]:
        if self.config is None:
            return tuple(names)
        resolved: list[str] = []
        for name in names:
            if name in self.config.targets:
                resolved.append(name)
            elif name in self.config.groups:
                resolved.extend(self.config.groups[name].targets)
            else:
                raise ValueError(f"unknown SSH target or group: {name}")
        return tuple(dict.fromkeys(resolved))

    async def _detect_adapter(self, connection: Any, target_name: str) -> Any:
        assert self.config is not None
        target = self.config.targets[target_name]
        remote_os = target.remote_os
        shell = target.shell
        explicit_platform = remote_os != "auto" or shell != "auto"
        if remote_os == "auto":
            windows = await connection.run(
                "cmd.exe /d /s /c ver", check=False, timeout=target.timeout_seconds
            )
            windows_text = f"{getattr(windows, 'stdout', '')}{getattr(windows, 'stderr', '')}"
            if getattr(windows, "exit_status", 1) == 0 and "windows" in windows_text.casefold():
                remote_os, detected_shell = classify_probe(windows_text)
            else:
                unix = await connection.run(
                    "uname -s", check=False, timeout=target.timeout_seconds
                )
                if getattr(unix, "exit_status", 1) != 0:
                    raise RuntimeError("unable to detect remote OS")
                remote_os, detected_shell = classify_probe(str(getattr(unix, "stdout", "")))
            if shell == "auto":
                shell = detected_shell
        adapter = shell_adapter(remote_os, shell)
        if explicit_platform:
            probes = {
                "powershell": (
                    "powershell -NoProfile -NonInteractive -Command "
                    '"Get-Command powershell -ErrorAction Stop | Out-Null"'
                ),
                "cmd": 'cmd.exe /d /s /c "where cmd.exe >nul"',
                "sh": "sh -lc 'command -v sh >/dev/null'",
                "bash": "bash -lc 'command -v bash >/dev/null'",
                "zsh": "zsh -lc 'command -v zsh >/dev/null'",
            }
            probe = await connection.run(
                probes[adapter.name], check=False, timeout=target.timeout_seconds
            )
            if getattr(probe, "exit_status", 1) != 0:
                raise RuntimeError(
                    f"configured remote shell is unavailable: {adapter.name}"
                )
        return adapter

    async def _exec_target(
        self,
        target_name: str,
        command: str,
        *,
        timeout: float | None,
        sudo: bool,
    ) -> CommandResult:
        if self.operation_worker is not None:
            return await self.operation_worker(target_name)
        if self.config is None or self.connection_manager is None:
            raise RuntimeError("SSH service is not configured")
        target = self.config.targets[target_name]
        async with self.connection_manager.connect(target_name) as connection:
            adapter = await self._detect_adapter(connection, target_name)
            return await CommandExecutor(
                connection, adapter, sudo_password=target.sudo_password
            ).exec(command, timeout=timeout or target.timeout_seconds, sudo=sudo)

    async def exec_many(
        self,
        targets: Iterable[str],
        command: str,
        *,
        max_parallel: int = 1,
        timeout: float | None = None,
        sudo: bool = False,
        explicit_high_impact: bool = False,
        confirmed_high_impact: bool = False,
    ) -> OperationResult:
        self.require_confirmation(
            "exec",
            confirmed=confirmed_high_impact,
            explicit_high_impact=explicit_high_impact,
        )
        ordered = self.resolve_targets(targets)
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        semaphore = asyncio.Semaphore(max_parallel)

        async def run_one(target_name: str) -> CommandResult:
            async with semaphore:
                try:
                    result = await self._exec_target(
                        target_name, command, timeout=timeout, sudo=sudo
                    )
                    return replace(result, target=target_name)
                except asyncio.CancelledError:
                    return CommandResult("cancelled", target=target_name, error="cancelled")
                except Exception as exc:
                    return CommandResult("failed", target=target_name, error=str(exc))

        results = tuple(await asyncio.gather(*(run_one(name) for name in ordered)))
        successes = sum(item.status == "success" for item in results)
        if successes == len(results):
            status = "success"
        elif successes:
            status = "partial"
        elif results and all(item.status == "cancelled" for item in results):
            status = "cancelled"
        else:
            status = "failed"
        return OperationResult(status, results)

    async def run_steps_many(
        self,
        targets: Iterable[str],
        steps: tuple[StepSpec, ...],
        *,
        max_parallel: int = 1,
    ) -> OperationResult:
        if self.config is None or self.connection_manager is None:
            raise RuntimeError("SSH service is not configured")
        ordered = self.resolve_targets(targets)
        semaphore = asyncio.Semaphore(max_parallel)

        async def run_one(target_name: str) -> OperationResult:
            async with semaphore:
                try:
                    target = self.config.targets[target_name]
                    async with self.connection_manager.connect(target_name) as connection:
                        adapter = await self._detect_adapter(connection, target_name)
                        results = await CommandExecutor(
                            connection, adapter, sudo_password=target.sudo_password
                        ).run_steps(steps)
                    status = "success" if results and all(
                        item.status == "success" for item in results
                    ) else "failed"
                    return OperationResult(
                        status,
                        tuple(replace(item, target=target_name) for item in results),
                    )
                except asyncio.CancelledError:
                    return OperationResult(
                        "cancelled",
                        (CommandResult("cancelled", target=target_name, error="cancelled"),),
                    )
                except Exception as exc:
                    return OperationResult(
                        "failed",
                        (CommandResult("failed", target=target_name, error=str(exc)),),
                    )

        per_target = tuple(await asyncio.gather(*(run_one(name) for name in ordered)))
        flat = tuple(item for result in per_target for item in result.results)
        successes = sum(result.status == "success" for result in per_target)
        status = (
            "success" if successes == len(per_target)
            else "partial" if successes
            else "failed"
        )
        return OperationResult(status, flat)
