from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_workflow_hub.lan_file_sharing import cli
from agent_workflow_hub.lan_file_sharing.runtime import RuntimeInspection


ROOT = Path(__file__).resolve().parents[3]
READY = RuntimeInspection(
    status="ready",
    executable="<PINNED_DUFS>",
    version="0.46.0",
    sha256="1" * 64,
    expected_sha256="1" * 64,
    asset_url="https://example.invalid/dufs.zip",
)


def test_parser_exposes_only_thin_adapter_commands() -> None:
    parser = cli.build_parser()
    subparsers = next(
        action for action in parser._actions if hasattr(action, "choices") and action.choices
    )

    assert set(subparsers.choices) == {
        "doctor",
        "install",
        "run",
        "serve-basic",
        "tunnel",
    }


def test_serve_basic_runs_python_http_server_without_resolving_dufs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        cli,
        "_asset",
        lambda *_args, **_kwargs: pytest.fail("Dufs must not be resolved"),
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    assert cli.main(
        [
            "serve-basic",
            "--directory",
            str(tmp_path),
            "--bind",
            "127.0.0.1",
            "--port",
            "8765",
        ],
        process_runner=runner,
    ) == 0

    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "http.server",
                "8765",
                "--bind",
                "127.0.0.1",
                "--directory",
                str(tmp_path),
            ],
            {"check": False, "shell": False},
        )
    ]


def test_tunnel_runs_user_managed_ngrok_with_native_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_asset",
        lambda *_args, **_kwargs: pytest.fail("Dufs must not be resolved"),
    )
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    assert cli.main(
        [
            "tunnel",
            "--port",
            "8765",
            "--",
            "--url",
            "https://files.example.test",
            "--traffic-policy-file",
            "<POLICY_PATH>",
        ],
        process_runner=runner,
        which=lambda name: "<NGROK>" if name == "ngrok" else None,
    ) == 0

    assert calls == [
        (
            [
                "<NGROK>",
                "http",
                "8765",
                "--url",
                "https://files.example.test",
                "--traffic-policy-file",
                "<POLICY_PATH>",
            ],
            {"check": False, "shell": False},
        )
    ]


def test_tunnel_reports_missing_ngrok_without_starting_process(
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []

    exit_code = cli.main(
        ["tunnel", "--port", "8765"],
        process_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        which=lambda _name: None,
    )

    assert exit_code == 3
    assert calls == []
    assert json.loads(capsys.readouterr().err)["category"] == "needs_dependency"


@pytest.mark.parametrize(
    "native",
    [
        ["--authtoken", "<SECRET_SENTINEL>"],
        ["--authtoken=<SECRET_SENTINEL>"],
    ],
)
def test_tunnel_rejects_authtoken_argv_without_leaking_it(
    native: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[object] = []

    exit_code = cli.main(
        ["tunnel", "--port", "8765", "--", *native],
        process_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        which=lambda _name: "<NGROK>",
    )

    captured = capsys.readouterr()
    assert exit_code == 4
    assert calls == []
    assert "<SECRET_SENTINEL>" not in captured.err
    assert json.loads(captured.err)["category"] == "invalid_arguments"


def test_foreground_interrupt_returns_standard_exit_code_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt

    assert cli.main(
        [
            "serve-basic",
            "--directory",
            str(tmp_path),
            "--bind",
            "127.0.0.1",
            "--port",
            "8765",
        ],
        process_runner=interrupted,
    ) == 130
    assert capsys.readouterr().err == ""


def test_doctor_reports_runtime_and_installation_without_running_dufs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "inspect_runtime", lambda *_: READY)
    monkeypatch.setattr(
        cli,
        "installation_candidate",
        lambda *_, **__: {"archive_sha256": "2" * 64},
    )

    assert cli.main(
        ["doctor", "--hub-root", str(ROOT)],
        system="windows",
        machine="x86_64",
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ready"
    assert report["runtime"]["executable"] == "<PINNED_DUFS>"
    assert report["installation"]["archive_sha256"] == "2" * 64


def test_install_uses_the_explicit_archive_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def fake_install(hub_root, asset, *, confirmed_asset_sha256):
        captured.update(
            hub_root=hub_root,
            asset=asset,
            confirmed_asset_sha256=confirmed_asset_sha256,
        )
        return READY

    monkeypatch.setattr(cli, "install_runtime", fake_install)
    digest = "a" * 64

    assert cli.main(
        [
            "install",
            "--hub-root",
            str(ROOT),
            "--confirmed-asset-sha256",
            digest,
        ],
        system="windows",
        machine="x86_64",
    ) == 0

    assert captured["hub_root"] == ROOT
    assert captured["confirmed_asset_sha256"] == digest
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_run_forwards_native_dufs_argv_without_reinterpreting_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "inspect_runtime", lambda *_: READY)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    native = [
        "<SHARE_PATH>",
        "--bind",
        "0.0.0.0",
        "--auth",
        "user:pass@/:rw",
        "--allow-all",
        "--allow-delete",
        "--allow-search",
        "--allow-symlink",
        "--allow-archive",
        "--allow-hash",
        "--enable-cors",
        "--render-spa",
        "--tls-cert",
        "<CERT_PATH>",
        "--tls-key",
        "<KEY_PATH>",
        "--log-format",
        "",
    ]

    assert cli.main(
        ["run", "--hub-root", str(ROOT), "--", *native],
        process_runner=runner,
        system="windows",
        machine="x86_64",
    ) == 0

    assert calls == [
        (
            ["<PINNED_DUFS>", *native],
            {"check": False, "shell": False},
        )
    ]


def test_run_supports_native_config_and_returns_dufs_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "inspect_runtime", lambda *_: READY)

    assert cli.main(
        ["run", "--hub-root", str(ROOT), "--", "--config", "<CONFIG_PATH>"],
        process_runner=lambda *_, **__: SimpleNamespace(returncode=17),
        system="windows",
        machine="x86_64",
    ) == 17


def test_run_refuses_to_start_when_the_pinned_runtime_is_not_ready(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = RuntimeInspection(
        status="needs_dependency",
        executable="<PINNED_DUFS>",
        version=None,
        sha256=None,
        expected_sha256="1" * 64,
        asset_url="https://example.invalid/dufs.zip",
    )
    monkeypatch.setattr(cli, "inspect_runtime", lambda *_: missing)
    calls: list[object] = []

    exit_code = cli.main(
        ["run", "--hub-root", str(ROOT), "--", "--version"],
        process_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        system="windows",
        machine="x86_64",
    )

    assert exit_code == 3
    assert calls == []
    assert json.loads(capsys.readouterr().err)["category"] == "needs_dependency"


def test_run_rejects_nul_without_starting_dufs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "inspect_runtime", lambda *_: READY)
    calls: list[object] = []

    exit_code = cli.main(
        ["run", "--hub-root", str(ROOT), "--", "bad\0argument"],
        process_runner=lambda *args, **kwargs: calls.append((args, kwargs)),
        system="windows",
        machine="x86_64",
    )

    assert exit_code == 4
    assert calls == []
    assert json.loads(capsys.readouterr().err)["category"] == "invalid_arguments"
