from __future__ import annotations

import asyncio
import base64
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

    @staticmethod
    def _clip_output(value: Any, limit: int) -> tuple[str, bool, int]:
        raw = str(value or "").encode("utf-8", errors="replace")
        truncated = len(raw) > limit
        clipped = raw[:limit].decode("utf-8", errors="ignore")
        return clipped, truncated, len(clipped.encode("utf-8"))

    @staticmethod
    def _pid_file(token: str) -> str:
        return f"/tmp/agent-workflow-hub-exec-{token}.pid"

    def _managed_posix_command(self, command: str, token: str) -> str:
        pid_file = self.shell.quote(self._pid_file(token))
        nested = self.shell.quote(command)
        return (
            f"__awh_pid_file={pid_file}; umask 077; "
            "if command -v setsid >/dev/null 2>&1; then "
            f"exec 3<&0; setsid sh -c {nested} <&3 & __awh_pid=$!; exec 3<&-; "
            "printf '%s\\n' \"$__awh_pid\" > \"$__awh_pid_file\"; "
            "wait \"$__awh_pid\"; __awh_status=$?; "
            "rm -f \"$__awh_pid_file\"; exit \"$__awh_status\"; "
            f"else {command}; fi"
        )

    async def _cleanup_posix_execution(
        self, token: str, *, sudo: bool
    ) -> tuple[str, int | None, bool, bool | None]:
        pid_file = self.shell.quote(self._pid_file(token))
        cleanup = (
            f"__awh_pid_file={pid_file}; "
            "test -r \"$__awh_pid_file\" || exit 4; "
            "IFS= read -r __awh_pid < \"$__awh_pid_file\"; "
            "case \"$__awh_pid\" in ''|*[!0-9]*) exit 5;; esac; "
            "printf '%s\\n' \"$__awh_pid\"; "
            "kill -TERM -- \"-$__awh_pid\" 2>/dev/null || true; "
            "__awh_i=0; "
            "while kill -0 -- \"-$__awh_pid\" 2>/dev/null && "
            "test \"$__awh_i\" -lt 20; do "
            "sleep 0.1; __awh_i=$((__awh_i + 1)); done; "
            "if kill -0 -- \"-$__awh_pid\" 2>/dev/null; then "
            "kill -KILL -- \"-$__awh_pid\" 2>/dev/null || true; sleep 0.1; fi; "
            "if kill -0 -- \"-$__awh_pid\" 2>/dev/null; then exit 3; fi; "
            "rm -f \"$__awh_pid_file\"; exit 0"
        )
        wrapped = self.shell.wrap_exec(cleanup, sudo=sudo)
        options: dict[str, Any] = {"check": False, "timeout": 5.0}
        if sudo and self.sudo_password is not None:
            options["input"] = self.sudo_password + "\n"
        try:
            result = await self.connection.run(wrapped, **options)
        except Exception:
            return "unknown", None, True, None
        remote_pid: int | None = None
        first_line = str(getattr(result, "stdout", "") or "").strip().splitlines()
        if first_line:
            try:
                remote_pid = int(first_line[0])
            except ValueError:
                remote_pid = None
        exit_code = getattr(result, "exit_status", None)
        if exit_code == 0:
            return "timed_out_cleaned", remote_pid, True, True
        if exit_code == 3:
            return "timed_out_still_running", remote_pid, True, False
        return "unknown", remote_pid, True, None

    async def _close_process(self, process: Any, *, abort: bool) -> None:
        if abort:
            try:
                process.terminate()
            except (AttributeError, ProcessLookupError):
                pass
        else:
            try:
                process.stdin.write(self.shell.exit_command + "\n")
                process.stdin.write_eof()
            except (AttributeError, BrokenPipeError):
                pass
        if not hasattr(process, "wait_closed"):
            return
        try:
            await asyncio.wait_for(process.wait_closed(), timeout=2.0)
            return
        except (asyncio.TimeoutError, TimeoutError):
            pass
        try:
            process.kill()
        except (AttributeError, ProcessLookupError):
            return
        try:
            await asyncio.wait_for(process.wait_closed(), timeout=1.0)
        except (asyncio.TimeoutError, TimeoutError):
            return

    async def exec(
        self,
        command: str,
        *,
        timeout: float | None = None,
        working_directory: str | None = None,
        environment: Mapping[str, str] | None = None,
        secret_environment: Mapping[str, str] | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        started = time.monotonic()
        try:
            if secret_environment:
                command = self.shell.wrap_secret_environment(
                    command, tuple(secret_environment)
                )
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
        token: str | None = None
        if timeout is not None and self.shell.remote_os in {"linux", "macos"}:
            token = secrets.token_hex(16)
            wrapped = self._managed_posix_command(wrapped, token)
        try:
            options: dict[str, Any] = {"check": False, "timeout": timeout}
            input_lines: list[str] = []
            if sudo and self.sudo_password is not None:
                input_lines.append(self.sudo_password)
            input_lines.extend(
                base64.b64encode(value.encode("utf-8")).decode("ascii")
                for value in (secret_environment or {}).values()
            )
            if input_lines:
                options["input"] = "\n".join(input_lines) + "\n"
            result = await self.connection.run(wrapped, **options)
        except asyncio.CancelledError:
            if token is not None:
                await asyncio.shield(
                    self._cleanup_posix_execution(token, sudo=sudo)
                )
            raise
        except (asyncio.TimeoutError, TimeoutError):
            if token is not None:
                state, remote_pid, attempted, verified = (
                    await self._cleanup_posix_execution(token, sudo=sudo)
                )
            else:
                state, remote_pid, attempted, verified = (
                    "unknown", None, False, None
                )
            return CommandResult(
                "failed", duration_seconds=time.monotonic() - started, error="timeout",
                execution_state=state, remote_pid=remote_pid,
                cleanup_attempted=attempted, cleanup_verified=verified,
            )
        exit_code = getattr(result, "exit_status", None)
        signal = getattr(result, "exit_signal", None)
        stdout, stdout_truncated, captured_stdout = self._clip_output(
            getattr(result, "stdout", ""), self.output_limit
        )
        stderr, stderr_truncated, captured_stderr = self._clip_output(
            getattr(result, "stderr", ""), self.output_limit
        )
        if exit_code == 0 and signal is None:
            status = "partial" if stdout_truncated or stderr_truncated else "success"
        else:
            status = "failed"
        return CommandResult(
            status=status,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            signal=str(signal) if signal else None,
            duration_seconds=time.monotonic() - started,
            execution_state="completed",
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            captured_stdout_bytes=captured_stdout,
            captured_stderr_bytes=captured_stderr,
            output_limit_bytes=self.output_limit,
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

    async def run_steps(
        self, steps: Sequence[StepSpec], *, default_timeout: float | None = None
    ) -> tuple[CommandResult, ...]:
        use_pty = any(step.use_pty for step in steps)
        process = await self.connection.create_process(
            term_type="xterm" if use_pty else None, encoding="utf-8"
        )
        completed: dict[str, CommandResult] = {}
        results: list[CommandResult] = []
        abort_process = False
        active_token: str | None = None
        active_sudo = False
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
                active_token = token
                active_sudo = step.sudo
                if self.shell.remote_os in {"linux", "macos"}:
                    wrapped = self._managed_posix_command(wrapped, token)
                process.stdin.write(self.shell.frame(wrapped, token) + "\n")
                if step.sudo and self.sudo_password is not None:
                    process.stdin.write(self.sudo_password + "\n")
                stdout_marker = f"__AWH_{token}_END__"
                stderr_marker = f"__AWH_{token}_ERR_END__"
                stdout_task: asyncio.Task[tuple[str, int | None]] | None = None
                stderr_task: asyncio.Task[tuple[str, int | None]] | None = None
                step_timeout = (
                    step.timeout_seconds
                    if step.timeout_seconds is not None
                    else default_timeout
                )
                try:
                    stdout_task = asyncio.create_task(
                        self._read_framed(process.stdout, stdout_marker)
                    )
                    if use_pty:
                        stdout, exit_code = await asyncio.wait_for(
                            stdout_task, timeout=step_timeout
                        )
                        stderr = ""
                    else:
                        stderr_task = asyncio.create_task(
                            self._read_framed(process.stderr, stderr_marker)
                        )
                        (stdout, exit_code), (stderr, _) = await asyncio.wait_for(
                            asyncio.gather(
                                stdout_task,
                                stderr_task,
                            ),
                            timeout=step_timeout,
                        )
                    status = "success" if exit_code == 0 else "failed"
                    result = CommandResult(
                        status, stdout, stderr, exit_code,
                        duration_seconds=time.monotonic() - started,
                        step_id=step.id,
                    )
                except (asyncio.TimeoutError, TimeoutError):
                    if self.shell.remote_os in {"linux", "macos"}:
                        state, remote_pid, attempted, verified = (
                            await self._cleanup_posix_execution(token, sudo=step.sudo)
                        )
                    else:
                        state, remote_pid, attempted, verified = (
                            "unknown", None, False, None
                        )
                    result = CommandResult(
                        "failed", step_id=step.id, error="timeout",
                        duration_seconds=time.monotonic() - started,
                        execution_state=state, remote_pid=remote_pid,
                        cleanup_attempted=attempted, cleanup_verified=verified,
                    )
                    abort_process = True
                except RuntimeError as exc:
                    if self.shell.remote_os in {"linux", "macos"}:
                        state, remote_pid, attempted, verified = (
                            await self._cleanup_posix_execution(token, sudo=step.sudo)
                        )
                        state = {
                            "timed_out_cleaned": "aborted_cleaned",
                            "timed_out_still_running": "aborted_still_running",
                        }.get(state, state)
                    else:
                        state, remote_pid, attempted, verified = (
                            "unknown", None, False, None
                        )
                    result = CommandResult(
                        "failed", step_id=step.id, error=str(exc),
                        duration_seconds=time.monotonic() - started,
                        execution_state=state, remote_pid=remote_pid,
                        cleanup_attempted=attempted, cleanup_verified=verified,
                    )
                    abort_process = True
                finally:
                    for task in (stdout_task, stderr_task):
                        if task is not None and not task.done():
                            task.cancel()
                results.append(result)
                if result.status == "success":
                    completed[step.id] = result
                elif result.error is not None or step.on_failure == "stop":
                    break
                active_token = None
        except asyncio.CancelledError:
            abort_process = True
            if (
                active_token is not None
                and self.shell.remote_os in {"linux", "macos"}
            ):
                await asyncio.shield(
                    self._cleanup_posix_execution(active_token, sudo=active_sudo)
                )
            raise
        finally:
            await self._close_process(process, abort=abort_process)
        return tuple(results)
