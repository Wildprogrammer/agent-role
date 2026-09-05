"""Bounded argv-only command execution for host adapters."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_INSTALLERS = frozenset(("pnpm", "npm", "npx", "pip", "pip3", "uv"))


class CommandExecutionError(RuntimeError):
    """Raised when a command violates policy or cannot be observed safely."""


@dataclass(frozen=True, kw_only=True)
class CommandResult:
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str


def _validate_argv(argv: object) -> tuple[str, ...]:
    if type(argv) is not tuple or not argv:
        raise CommandExecutionError("command must be a non-empty argv tuple")
    if len(argv) > 128:
        raise CommandExecutionError("command has too many arguments")
    if not all(
        type(item) is str and bool(item) and "\x00" not in item and len(item) <= 8192
        for item in argv
    ):
        raise CommandExecutionError("command contains an invalid argument")
    return argv


def _is_preview_read(argv: tuple[str, ...]) -> bool:
    if argv[0].casefold() == "git" and len(argv) >= 5 and argv[1] == "-C":
        return argv[3:] in (
            ("remote", "get-url", "origin"),
            ("rev-parse", "HEAD"),
            ("status", "--porcelain"),
        )
    if argv[0].casefold() != "hermes":
        return False
    if argv[-1] == "--help":
        return True
    if argv == ("hermes", "--version") or argv == (
        "hermes",
        "profile",
        "list",
    ):
        return True
    if len(argv) >= 5 and argv[1] == "--profile":
        remainder = argv[3:]
        if remainder == ("config", "path"):
            return True
        if len(remainder) == 3 and remainder[:2] == ("config", "get"):
            return True
        if len(remainder) >= 2 and remainder[:2] == ("skills", "list"):
            return True
    return False


class CommandRunner:
    """Execute one finite command without a shell."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 20.0,
        max_output_bytes: int = 65_536,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 120:
            raise CommandExecutionError("timeout must be in (0, 120] seconds")
        if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 1_048_576:
            raise CommandExecutionError("max_output_bytes is outside the finite range")
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    def run(
        self,
        argv: tuple[str, ...],
        *,
        phase: Literal["preview", "apply", "verify"],
    ) -> CommandResult:
        checked = _validate_argv(argv)
        if phase not in ("preview", "apply", "verify"):
            raise CommandExecutionError("unsupported command phase")
        executable = Path(checked[0]).name.casefold().removesuffix(".exe")
        if executable in _INSTALLERS:
            raise CommandExecutionError("package managers are not supported by this workflow")
        if phase == "preview" and not _is_preview_read(checked):
            raise CommandExecutionError("command is not allowed during read-only preview")
        try:
            completed = subprocess.run(
                checked,
                shell=False,
                check=False,
                capture_output=True,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommandExecutionError(f"command execution failed: {exc}") from None
        if type(completed.stdout) is not bytes or type(completed.stderr) is not bytes:
            raise CommandExecutionError("command output must be captured as bytes")
        if (
            len(completed.stdout) > self._max_output_bytes
            or len(completed.stderr) > self._max_output_bytes
        ):
            raise CommandExecutionError("command output exceeded the finite limit")
        try:
            stdout = completed.stdout.decode("utf-8", errors="strict")
            stderr = completed.stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CommandExecutionError("command output is not strict UTF-8") from None
        if type(completed.returncode) is not int:
            raise CommandExecutionError("command exit code is invalid")
        return CommandResult(
            argv=checked,
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
        )


__all__ = ["CommandExecutionError", "CommandResult", "CommandRunner"]
