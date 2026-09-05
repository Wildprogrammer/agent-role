from __future__ import annotations

from pathlib import Path

import pytest

from agent_workflow_hub.specialized_agent_deployment.cli import build_parser, main


def test_cli_exposes_deployment_and_local_wheel_preparation() -> None:
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert set(choices) == {"preview", "apply", "verify", "runtime-wheels"}
    for forbidden in ("install", "upgrade", "delete", "uninstall", "clean", "set-default"):
        with pytest.raises(SystemExit):
            parser.parse_args([forbidden])


def test_cli_requires_confirmation_sha_for_apply(tmp_path: Path) -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "apply",
                "--hub-root",
                str(tmp_path.resolve()),
                "--manifest",
                str((tmp_path / "manifest.json").resolve()),
            ]
        )


def test_cli_rejects_relative_paths() -> None:
    assert main(["preview", "--hub-root", "relative", "--request", "request.json"]) == 2
