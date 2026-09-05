from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
import yaml

from agent_workflow_hub.specialized_agent_deployment.contracts import DeploymentRequest
from agent_workflow_hub.specialized_agent_deployment.runner import (
    CommandExecutionError,
    CommandResult,
)


def enablement_module():
    return importlib.import_module(
        "agent_workflow_hub.specialized_agent_deployment.hosts.hermes_enablement"
    )


def request(tmp_path: Path, *, enablement: object | None) -> DeploymentRequest:
    host_options = {} if enablement is None else {"enablement": enablement}
    return DeploymentRequest(
        schema_version="1.0",
        deployment_id="hermes-enablement-fixture",
        agent_id="fixture-agent",
        display_name="Fixture Agent",
        purpose="test deployment",
        host="hermes",
        mode="create",
        primary_workflow="primary-flow",
        related_workflows=(),
        auxiliary_skills=(),
        workdir=str((tmp_path / "work").resolve()),
        config_refs=(),
        host_options=host_options,
    )


def full_options(*platforms: str) -> dict[str, object]:
    return {
        "mode": "full",
        "source_profile": "active",
        "model_strategy": "managed-fields",
        "env_strategy": "full",
        "platforms": list(platforms),
        "gateway_strategy": "multiplex-routes",
        "external_resources": "check_only",
        "behavior_check": "readiness_only",
    }


def prepare_profiles(tmp_path: Path) -> tuple[Path, str]:
    profiles_root = (tmp_path / "hermes" / "profiles").resolve()
    source = profiles_root / "source-profile"
    target = profiles_root / "fixture-agent"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    (source / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {
                    "default": "fixture-model",
                    "provider": "fixture-provider",
                    "base_url": "https://model.invalid/v1",
                    "api_key": "SYNTHETIC_ENABLEMENT_TOKEN_MODEL",
                    "api_mode": "responses",
                },
                "platforms": {"telegram": {"enabled": True}},
                "gateway": {"unrelated": "keep"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (source / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=SYNTHETIC_ENABLEMENT_TOKEN_TELEGRAM\n"
        "WEIXIN_TOKEN=SYNTHETIC_ENABLEMENT_TOKEN_WEIXIN\n",
        encoding="utf-8",
    )
    (target / "config.yaml").write_text(
        yaml.safe_dump(
            {"model": {"target_only": "keep"}, "agent": {"max_turns": 80}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    profile_list = " ◆source-profile fixture-model running alias —\n"
    return profiles_root, profile_list


def test_parse_enablement_is_optional_and_normalizes_platforms(tmp_path: Path) -> None:
    module = enablement_module()
    assert module.parse_enablement_request(request(tmp_path, enablement=None)) is None

    parsed = module.parse_enablement_request(
        request(tmp_path, enablement=full_options("weixin", "telegram"))
    )

    assert parsed.platforms == ("telegram", "weixin")
    assert parsed.source_profile == "active"


@pytest.mark.parametrize(
    "bad",
    [
        full_options(),
        full_options("Telegram"),
        full_options("telegram", "telegram"),
        full_options("telegram") | {"gateway_strategy": "second-gateway"},
        full_options("telegram") | {"unexpected": True},
    ],
)
def test_parse_enablement_rejects_ambiguous_or_unsupported_scope(
    tmp_path: Path, bad: dict[str, object]
) -> None:
    module = enablement_module()
    with pytest.raises(module.HermesEnablementError):
        module.parse_enablement_request(request(tmp_path, enablement=bad))


def test_projection_copies_managed_model_env_and_plans_single_gateway_routes(
    tmp_path: Path,
) -> None:
    module = enablement_module()
    profiles_root, profile_list = prepare_profiles(tmp_path)
    requested = request(
        tmp_path, enablement=full_options("weixin", "telegram")
    )

    projection = module.build_enablement_projection(
        requested,
        profiles_root=profiles_root,
        profile_list_output=profile_list,
    )

    target = yaml.safe_load(projection.target_config_bytes)
    gateway = yaml.safe_load(projection.gateway_config_bytes)
    assert target["model"] == {
        "target_only": "keep",
        "default": "fixture-model",
        "provider": "fixture-provider",
        "base_url": "https://model.invalid/v1",
        "api_key": "SYNTHETIC_ENABLEMENT_TOKEN_MODEL",
        "api_mode": "responses",
    }
    assert target["agent"] == {
        "max_turns": 80,
        "system_prompt_file": "AGENTS.md",
    }
    assert target["terminal"]["cwd"] == requested.workdir
    assert target["platforms"]["telegram"]["enabled"] is False
    assert target["platforms"]["weixin"]["enabled"] is False
    assert gateway["gateway"]["unrelated"] == "keep"
    assert gateway["gateway"]["multiplex_profiles"] is True
    assert gateway["gateway"]["profile_routes"] == [
        {
            "name": "agent-workflow-hub-hermes-enablement-fixture-telegram",
            "platform": "telegram",
            "profile": "fixture-agent",
            "enabled": True,
        },
        {
            "name": "agent-workflow-hub-hermes-enablement-fixture-weixin",
            "platform": "weixin",
            "profile": "fixture-agent",
            "enabled": True,
        },
    ]
    assert projection.env_bytes.startswith(b"TELEGRAM_BOT_TOKEN=")
    redacted = json.dumps(projection.redacted_facts(), ensure_ascii=False)
    assert "SYNTHETIC_ENABLEMENT_TOKEN_" not in redacted
    assert "SYNTHETIC_ENABLEMENT_TOKEN_" not in repr(projection)
    assert projection.redacted_facts()["model_fields"] == [
        "default",
        "provider",
        "base_url",
        "api_key",
        "api_mode",
    ]


def test_projection_blocks_unknown_route_that_can_override_selected_platform(
    tmp_path: Path,
) -> None:
    module = enablement_module()
    profiles_root, profile_list = prepare_profiles(tmp_path)
    source_config = profiles_root / "source-profile" / "config.yaml"
    source = yaml.safe_load(source_config.read_text(encoding="utf-8"))
    source["gateway"]["profile_routes"] = [
        {
            "name": "somebody-else",
            "platform": "telegram",
            "chat_id": "123",
            "profile": "other-profile",
            "enabled": True,
        }
    ]
    source_config.write_text(yaml.safe_dump(source, sort_keys=False), encoding="utf-8")

    with pytest.raises(module.HermesEnablementError, match="route conflict"):
        module.build_enablement_projection(
            request(tmp_path, enablement=full_options("telegram")),
            profiles_root=profiles_root,
            profile_list_output=profile_list,
        )


def test_projection_rejects_source_profile_equal_to_target(tmp_path: Path) -> None:
    module = enablement_module()
    profiles_root, _ = prepare_profiles(tmp_path)
    with pytest.raises(module.HermesEnablementError, match="target Profile"):
        module.build_enablement_projection(
            request(tmp_path, enablement=full_options("telegram")),
            profiles_root=profiles_root,
            profile_list_output=" ◆fixture-agent fixture-model running alias —\n",
        )


def test_invalid_yaml_error_does_not_echo_secret_content(tmp_path: Path) -> None:
    module = enablement_module()
    profiles_root, profile_list = prepare_profiles(tmp_path)
    (profiles_root / "source-profile" / "config.yaml").write_text(
        "model: [SYNTHETIC_ENABLEMENT_TOKEN_BROKEN\n",
        encoding="utf-8",
    )

    with pytest.raises(module.HermesEnablementError) as caught:
        module.build_enablement_projection(
            request(tmp_path, enablement=full_options("telegram")),
            profiles_root=profiles_root,
            profile_list_output=profile_list,
        )

    assert "SYNTHETIC_ENABLEMENT_TOKEN_" not in str(caught.value)


class TransactionRunner:
    def __init__(self, *, restart: str = "passed") -> None:
        self.restart = restart
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def run(self, argv: tuple[str, ...], *, phase: str) -> CommandResult:
        self.calls.append((argv, phase))
        if self.restart == "unknown":
            raise CommandExecutionError("restart outcome unavailable")
        if self.restart == "failed" and len(self.calls) > 1:
            return CommandResult(
                argv=argv,
                exit_code=0,
                stdout="",
                stderr="",
            )
        return CommandResult(
            argv=argv,
            exit_code=0 if self.restart == "passed" else 1,
            stdout="",
            stderr="",
        )


def test_transaction_keeps_target_local_backups_until_commit(tmp_path: Path) -> None:
    module = enablement_module()
    profiles_root, profile_list = prepare_profiles(tmp_path)
    requested = request(tmp_path, enablement=full_options("telegram"))
    projection = module.build_enablement_projection(
        requested,
        profiles_root=profiles_root,
        profile_list_output=profile_list,
    )
    runner = TransactionRunner()
    transaction = module.HermesEnablementTransaction(
        projection=projection,
        plan_sha256="a" * 64,
        runner=runner,
    )

    result = transaction.apply()

    assert result.status == "applied"
    assert transaction.backup_root.parent.parent == profiles_root / "fixture-agent"
    assert transaction.backup_root.is_dir()
    assert projection.target_env_path.read_bytes() == projection.env_bytes
    assert projection.gateway_config_path.read_bytes() == projection.gateway_config_bytes
    transaction.commit()
    assert not transaction.backup_root.exists()


def test_known_restart_failure_rolls_back_only_enablement_files(tmp_path: Path) -> None:
    module = enablement_module()
    profiles_root, profile_list = prepare_profiles(tmp_path)
    requested = request(tmp_path, enablement=full_options("telegram"))
    projection = module.build_enablement_projection(
        requested,
        profiles_root=profiles_root,
        profile_list_output=profile_list,
    )
    original_source = projection.source_config_path.read_bytes()
    original_target = projection.target_config_path.read_bytes()
    runner = TransactionRunner(restart="failed")
    transaction = module.HermesEnablementTransaction(
        projection=projection,
        plan_sha256="b" * 64,
        runner=runner,
    )

    result = transaction.apply()

    assert result.status == "rolled_back"
    assert projection.source_config_path.read_bytes() == original_source
    assert projection.target_config_path.read_bytes() == original_target
    assert not projection.target_env_path.exists()


def test_unknown_restart_result_retains_evidence_without_replay(tmp_path: Path) -> None:
    module = enablement_module()
    profiles_root, profile_list = prepare_profiles(tmp_path)
    projection = module.build_enablement_projection(
        request(tmp_path, enablement=full_options("telegram")),
        profiles_root=profiles_root,
        profile_list_output=profile_list,
    )
    runner = TransactionRunner(restart="unknown")
    transaction = module.HermesEnablementTransaction(
        projection=projection,
        plan_sha256="c" * 64,
        runner=runner,
    )

    result = transaction.apply()

    assert result.status == "outcome_unknown"
    assert transaction.backup_root.is_dir()
    assert len(runner.calls) == 1
