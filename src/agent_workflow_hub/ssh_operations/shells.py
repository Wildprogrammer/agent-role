from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Mapping


def classify_probe(output: str) -> tuple[str, str]:
    normalized = output.casefold()
    if "windows" in normalized:
        return "windows", "powershell"
    if "darwin" in normalized:
        return "macos", "sh"
    if "linux" in normalized:
        return "linux", "sh"
    raise ValueError("unable to classify remote OS")


@dataclass(frozen=True)
class ShellAdapter:
    name: str
    remote_os: str

    def quote(self, value: str) -> str:
        raise NotImplementedError

    def wrap_exec(
        self,
        command: str,
        *,
        working_directory: str | None = None,
        environment: Mapping[str, str] | None = None,
        sudo: bool = False,
    ) -> str:
        raise NotImplementedError

    def frame(self, command: str, token: str) -> str:
        raise NotImplementedError

    @property
    def exit_command(self) -> str:
        return "exit"


class PosixShell(ShellAdapter):
    def __init__(self, name: str = "sh", remote_os: str = "linux") -> None:
        super().__init__(name, remote_os)

    def quote(self, value: str) -> str:
        return shlex.quote(value)

    def wrap_exec(
        self,
        command: str,
        *,
        working_directory: str | None = None,
        environment: Mapping[str, str] | None = None,
        sudo: bool = False,
    ) -> str:
        prefixes: list[str] = []
        if working_directory:
            prefixes.append(f"cd {self.quote(working_directory)}")
        for key, value in (environment or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid environment name: {key}")
            prefixes.append(f"export {key}={self.quote(value)}")
        body = " && ".join((*prefixes, command)) if prefixes else command
        if sudo:
            body = f"sudo -S -p '' -- sh -lc {self.quote(body)}"
        return body

    def frame(self, command: str, token: str) -> str:
        return (
            f"printf '%s\\n' '__AWH_{token}_BEGIN__'; "
            f"{command}; __awh_status=$?; "
            f"printf '%s\\n' '__AWH_{token}_ERR_END__' >&2; "
            f"printf '%s:%s\\n' '__AWH_{token}_END__' \"$__awh_status\""
        )


class PowerShell(ShellAdapter):
    def __init__(self) -> None:
        super().__init__("powershell", "windows")

    def quote(self, value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def wrap_exec(
        self,
        command: str,
        *,
        working_directory: str | None = None,
        environment: Mapping[str, str] | None = None,
        sudo: bool = False,
    ) -> str:
        if sudo:
            raise ValueError("needs-elevation: Windows SSH sessions use current privileges")
        prefixes: list[str] = []
        if working_directory:
            prefixes.append(f"Set-Location -LiteralPath {self.quote(working_directory)}")
        for key, value in (environment or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid environment name: {key}")
            prefixes.append(f"$env:{key}={self.quote(value)}")
        return "; ".join((*prefixes, command)) if prefixes else command

    def frame(self, command: str, token: str) -> str:
        return (
            f"Write-Output '__AWH_{token}_BEGIN__'; "
            f"& {{ {command} }}; $__awh_status=$LASTEXITCODE; "
            f"if ($null -eq $__awh_status) {{$__awh_status=0}}; "
            f"[Console]::Error.WriteLine('__AWH_{token}_ERR_END__'); "
            f"Write-Output ('__AWH_{token}_END__:' + $__awh_status)"
        )


class CmdShell(ShellAdapter):
    def __init__(self) -> None:
        super().__init__("cmd", "windows")

    def quote(self, value: str) -> str:
        escaped = value.replace("^", "^^")
        for char in "&|<>()":
            escaped = escaped.replace(char, "^" + char)
        return f'"{escaped}"'

    def wrap_exec(
        self,
        command: str,
        *,
        working_directory: str | None = None,
        environment: Mapping[str, str] | None = None,
        sudo: bool = False,
    ) -> str:
        if sudo:
            raise ValueError("needs-elevation: Windows SSH sessions use current privileges")
        prefixes: list[str] = []
        if working_directory:
            prefixes.append(f"cd /d {self.quote(working_directory)}")
        for key, value in (environment or {}).items():
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError(f"invalid environment name: {key}")
            prefixes.append(f'set "{key}={value}"')
        return " && ".join((*prefixes, command)) if prefixes else command

    def frame(self, command: str, token: str) -> str:
        return (
            f"echo __AWH_{token}_BEGIN__ & {command} & set __awh_status=%errorlevel% & "
            f"echo __AWH_{token}_ERR_END__ 1>&2 & "
            f"echo __AWH_{token}_END__:%__awh_status%"
        )


def shell_adapter(remote_os: str, shell: str) -> ShellAdapter:
    if remote_os == "windows":
        if shell in {"auto", "powershell"}:
            return PowerShell()
        if shell == "cmd":
            return CmdShell()
        raise ValueError(f"shell {shell} is incompatible with Windows")
    if remote_os in {"linux", "macos"}:
        if shell in {"auto", "sh", "bash", "zsh"}:
            return PosixShell("sh" if shell == "auto" else shell, remote_os)
        raise ValueError(f"shell {shell} is incompatible with {remote_os}")
    raise ValueError(f"unsupported remote OS: {remote_os}")
