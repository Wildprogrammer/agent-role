from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_workflow_hub.ssh_operations.config import ConfigError, load_config, load_request


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "ssh.ini"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def test_password_is_literal_and_sudo_defaults_to_login_password(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [ssh]
        known_hosts = known_hosts

        [target:linux]
        host = 127.0.0.1
        username = tester
        auth = password
        password = P%word!
        sudo_password =
        """,
    )
    config = load_config(path.resolve(), environ={})
    target = config.targets["linux"]
    assert target.password == "P%word!"
    assert target.sudo_password == "P%word!"
    assert config.known_hosts == tmp_path / "known_hosts"


def test_environment_password_and_direct_password_conflict(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [target:linux]
        host = example.test
        username = tester
        password = direct
        password_env = SSH_PASSWORD
        """,
    )
    with pytest.raises(ConfigError, match="password.*conflict"):
        load_config(path.resolve(), environ={"SSH_PASSWORD": "environment"})


def test_jump_cycle_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [target:a]
        host = a.test
        username = tester
        via = b

        [target:b]
        host = b.test
        username = tester
        via = a
        """,
    )
    with pytest.raises(ConfigError, match="jump cycle"):
        load_config(path.resolve(), environ={})


def test_groups_and_explicit_platform_values_are_parsed(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [target:win]
        host = win.test
        port = 2222
        username = tester
        auth = agent
        remote_os = windows
        shell = powershell
        forward_agent = false

        [target:mac]
        host = mac.test
        username = tester
        remote_os = macos
        shell = zsh

        [group:desktop]
        targets = win, mac
        max_parallel = 2
        """,
    )
    config = load_config(path.resolve(), environ={})
    assert config.targets["win"].port == 2222
    assert config.targets["win"].remote_os == "windows"
    assert config.targets["win"].shell == "powershell"
    assert config.groups["desktop"].targets == ("win", "mac")
    assert config.groups["desktop"].max_parallel == 2


def test_shared_environment_ini_ignores_non_ssh_sections(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [environment]
        name = autotest-platform-validated
        timeout_seconds = 5

        [target.mysql]
        host = mysql.test
        port = 3306
        username = database-user
        password = database-password

        [ssh]
        known_hosts = ssh-known-hosts

        [target:wwwang-lan]
        host = 192.0.2.25
        username = wwwang
        auth = password
        password = ssh-password
        remote_os = auto
        """,
    )

    config = load_config(path.resolve(), environ={})

    assert set(config.targets) == {"wwwang-lan"}
    assert config.targets["wwwang-lan"].host == "192.0.2.25"
    assert config.known_hosts == tmp_path / "ssh-known-hosts"


def test_shared_environment_ini_keeps_ssh_sections_strict(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [environment]
        name = autotest-platform-validated

        [target:wwwang-lan]
        host = 192.0.2.25
        username = wwwang
        unsupported_ssh_option = true
        """,
    )

    with pytest.raises(ConfigError, match=r"unknown field in target:wwwang-lan"):
        load_config(path.resolve(), environ={})


def test_private_key_is_resolved_relative_to_ini(tmp_path: Path) -> None:
    key = tmp_path / "keys" / "id_ed25519"
    key.parent.mkdir()
    key.write_text("fixture", encoding="utf-8")
    path = _write(
        tmp_path,
        """
        [target:keyed]
        host = key.test
        username = tester
        auth = key
        private_key = keys/id_ed25519
        """,
    )
    assert load_config(path.resolve(), environ={}).targets["keyed"].private_key == key


def test_request_requires_absolute_existing_path_and_rejects_unknown_fields(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="absolute"):
        load_request(Path("request.json"))

    path = tmp_path / "request.json"
    path.write_text(json.dumps({"operation": "exec", "unexpected": True}), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown field"):
        load_request(path.resolve())


def test_request_parses_related_steps(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(
        json.dumps(
            {
                "operation": "run-steps",
                "targets": ["linux"],
                "steps": [
                    {"id": "one", "command": "pwd"},
                    {
                        "id": "two",
                        "command": "printf %s ${steps.one.stdout}",
                        "depends_on": ["one"],
                        "environment": {"MODE": "test"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    request = load_request(path.resolve())
    assert request.operation == "run-steps"
    assert request.steps[1].depends_on == ("one",)
    assert dict(request.steps[1].environment) == {"MODE": "test"}


def test_request_accepts_transfer_fields_and_stable_request_id(tmp_path: Path) -> None:
    path = tmp_path / "transfer.json"
    path.write_text(
        json.dumps(
            {
                "operation": "upload",
                "request_id": "upload-42",
                "targets": ["linux"],
                "source": "C:/data/file.txt",
                "destination": "/data/file.txt",
                "mode": "sftp",
                "overwrite": True,
                "resume": True,
                "content": "optional-write-content",
                "encoding": "utf-8",
            }
        ),
        encoding="utf-8",
    )
    request = load_request(path.resolve())
    assert request.request_id == "upload-42"
    assert request.parameters["mode"] == "sftp"
