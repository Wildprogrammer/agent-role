from __future__ import annotations

import asyncio
import re
import secrets
import time
from typing import Any, Mapping, Sequence

from .models import CommandResult, StepSpec
from .shells import ShellAdapter

_REFERENCE = re.compile(r"\$\{steps\.([A-Za-z0-9_.-]+)\.stdout\}")


class CommandExecutor:
    def __init__(
        self,
        connection: Any,
        shell: ShellAdapter,
        *,
        sudo_password: str | None = None,
        output_limit: int = 1024 * 1024,
    ) -> None:
        self.connection = connection
        self.shell = shell
        self.sudo_password = sudo_password
        self.output_limit = output_limit

    async def exec(
        self,
        command: str,
        *,
        timeout: float | None = None,
        working_directory: str | None = None,
        environment: Mapping[str, str] | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        started = time.monotonic()
        try:
            wrapped = self.shell.wrap_exec(
                command,
                working_directory=working_directory,
                environment=environment,
                sudo=sudo,
            )
        except ValueError as exc:
            if "needs-elevation" in str(exc):
                return CommandResult(
                    "needs-elevation", duration_seconds=time.monotonic() - started,
                    error="needs-elevation",
                )
            raise
        try:
            options: dict[str, Any] = {"check": False, "timeout": timeout}
            if sudo and self.sudo_password is not None:
                options["input"] = self.sudo_password + "\n"
            result = await self.connection.run(wrapped, **options)
        except (asyncio.TimeoutError, TimeoutError):
            return CommandResult(
                "failed", duration_seconds=time.monotonic() - started, error="timeout"
            )
        exit_code = getattr(result, "exit_status", None)
        signal = getattr(result, "exit_signal", None)
        status = "success" if exit_code == 0 and signal is None else "failed"
        return CommandResult(
            status=status,
            stdout=str(getattr(result, "stdout", "") or "")[: self.output_limit],
            stderr=str(getattr(result, "stderr", "") or "")[: self.output_limit],
            exit_code=exit_code,
            signal=str(signal) if signal else None,
            duration_seconds=time.monotonic() - started,
        )

    def substitute_references(
        self, command: str, completed: Mapping[str, Any], *, allowed: set[str]
    ) -> str:
        def replace(match: re.Match[str]) -> str:
            step_id = match.group(1)
            if step_id not in allowed or step_id not in completed:
                raise ValueError(f"step reference is not a completed dependency: {step_id}")
            return self.shell.quote(str(completed[step_id].stdout))

        return _REFERENCE.sub(replace, command)

    async def _read_framed(self, stream: Any, marker: str) -> tuple[str, int | None]:
        chunks: list[str] = []
        size = 0
        status: int | None = None
        begin_marker = marker.replace("_END__", "_BEGIN__")
        error_marker = marker.replace("_END__", "_ERR_END__")
        while True:
            line = await stream.readline()
            if not line:
                raise RuntimeError("remote shell closed before result marker")
            text = str(line)
            stripped = text.rstrip("\r\n")
            if stripped == begin_marker:
                continue
            if stripped == error_marker:
                continue
            if stripped == marker:
                break
            if stripped.startswith(marker + ":"):
                try:
                    status = int(stripped.split(":", 1)[1])
                except ValueError:
                    raise RuntimeError("invalid remote status marker") from None
                break
            encoded = len(text.encode("utf-8", errors="replace"))
            if size + encoded > self.output_limit:
                remaining = max(0, self.output_limit - size)
                chunks.append(text.encode("utf-8")[:remaining].decode("utf-8", errors="ignore"))
                raise RuntimeError("remote step output limit exceeded")
            size += encoded
            chunks.append(text)
        return "".join(chunks), status

    async def run_steps(self, steps: Sequence[StepSpec]) -> tuple[CommandResult, ...]:
        use_pty = any(step.use_pty for step in steps)
        process = await self.connection.create_process(
            term_type="xterm" if use_pty else None, encoding="utf-8"
        )
        completed: dict[str, CommandResult] = {}
        results: list[CommandResult] = []
        try:
            for step in steps:
                started = time.monotonic()
                try:
                    command = self.substitute_references(
                        step.command, completed, allowed=set(step.depends_on)
                    )
                    wrapped = self.shell.wrap_exec(
                        command,
                        working_directory=step.working_directory,
                        environment=step.environment,
                        sudo=step.sudo,
                    )
                except ValueError as exc:
                    status = "needs-elevation" if "needs-elevation" in str(exc) else "failed"
                    result = CommandResult(
                        status, step_id=step.id, error=str(exc),
                        duration_seconds=time.monotonic() - started,
                    )
                    results.append(result)
                    if step.on_failure == "stop":
                        break
                    continue
                token = secrets.token_hex(16)
                process.stdin.write(self.shell.frame(wrapped, token) + "\n")
                if step.sudo and self.sudo_password is not None:
                    process.stdin.write(self.sudo_password + "\n")
                stdout_marker = f"__AWH_{token}_END__"
                stderr_marker = f"__AWH_{token}_ERR_END__"
                try:
                    stdout_task = self._read_framed(process.stdout, stdout_marker)
                    if use_pty:
                        stdout, exit_code = await asyncio.wait_for(
                            stdout_task, timeout=step.timeout_seconds
                        )
                        stderr = ""
                    else:
                        (stdout, exit_code), (stderr, _) = await asyncio.wait_for(
                            asyncio.gather(
                                stdout_task,
                                self._read_framed(process.stderr, stderr_marker),
                            ),
                            timeout=step.timeout_seconds,
                        )
                    status = "success" if exit_code == 0 else "failed"
                    result = CommandResult(
                        status, stdout, stderr, exit_code,
                        duration_seconds=time.monotonic() - started,
                        step_id=step.id,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    result = CommandResult(
                        "failed", step_id=step.id, error="timeout",
                        duration_seconds=time.monotonic() - started,
                    )
                except RuntimeError as exc:
                    result = CommandResult(
                        "failed", step_id=step.id, error=str(exc),
                        duration_seconds=time.monotonic() - started,
                    )
                results.append(result)
                if result.status == "success":
                    completed[step.id] = result
                elif step.on_failure == "stop":
                    break
        finally:
            try:
                process.stdin.write(self.shell.exit_command + "\n")
                process.stdin.write_eof()
            except (AttributeError, BrokenPipeError):
                pass
            if hasattr(process, "wait_closed"):
                await process.wait_closed()
        return tuple(results)
