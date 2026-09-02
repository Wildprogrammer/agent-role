from __future__ import annotations

import shlex

import pytest

from agent_workflow_hub.ssh_operations.shells import (
    CmdShell,
    PosixShell,
    PowerShell,
    classify_probe,
    shell_adapter,
)


@pytest.mark.parametrize(
    ("probe", "expected"),
    [
        ("Linux", ("linux", "sh")),
        ("Darwin", ("macos", "sh")),
        ("Microsoft Windows [Version 10.0]", ("windows", "powershell")),
    ],
)
def test_probe_results_select_adapter(probe: str, expected: tuple[str, str]) -> None:
    assert classify_probe(probe) == expected


def test_shells_quote_untrusted_step_output() -> None:
    assert PosixShell().quote("x'; echo bad") == shlex.quote("x'; echo bad")
    assert PowerShell().quote("a'b") == "'a''b'"
    assert CmdShell().quote("a&b") == '"a^&b"'


def test_posix_wrapper_preserves_cwd_environment_and_sudo_contract() -> None:
    wrapped = PosixShell().wrap_exec(
        "printf ok", working_directory="/tmp/a b", environment={"MODE": "a'b"}, sudo=True
    )
    assert "sudo -S -p '' -- sh -lc" in wrapped
    elevated_body = shlex.split(wrapped)[-1]
    assert "cd '/tmp/a b'" in elevated_body
    assert "export MODE='a'\"'\"'b'" in elevated_body


def test_shell_adapter_rejects_windows_sudo() -> None:
    with pytest.raises(ValueError, match="needs-elevation"):
        PowerShell().wrap_exec("whoami", sudo=True)


def test_explicit_shell_and_os_must_be_compatible() -> None:
    assert isinstance(shell_adapter("linux", "bash"), PosixShell)
    with pytest.raises(ValueError, match="incompatible"):
        shell_adapter("windows", "bash")
