from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from agent_workflow_hub.specialized_agent_deployment.contracts import (
    Collaborator,
    DeploymentRequest,
    SkillFile,
    SkillSelection,
    SkillSnapshot,
    canonical_sha256,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.base import (
    ApplyContext,
    VerifyContext,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.hermes import (
    BEHAVIOR_PROMPT_TEMPLATE,
    HERMES_RUNS_API_MIN_VERSION,
    VERIFIED_HERMES_VERSION,
    HermesAdapter,
    HermesAdapterError,
    _bot_mode_profile_bytes,
    _parse_behavior_output,
)
from agent_workflow_hub.specialized_agent_deployment.hosts.hermes_enablement import (
    build_enablement_projection,
)
from agent_workflow_hub.specialized_agent_deployment.planning import (
    build_deployment_plan,
    planned_manifest,
)
from agent_workflow_hub.specialized_agent_deployment.rendering import (
    render_deployment_preview,
    render_persona,
)
from agent_workflow_hub.specialized_agent_deployment.runner import (
    CommandExecutionError,
    CommandResult,
)


class FakeRunner:
    def __init__(self, responses=None, handler=None) -> None:
        self.responses = dict(responses or {})
        self.handler = handler
        self.calls: list[tuple[tuple[str, ...], str]] = []

    def run(self, argv: tuple[str, ...], *, phase: str) -> CommandResult:
        self.calls.append((argv, phase))
        if self.handler is not None:
            handled = self.handler(argv, phase)
            if handled is not None:
                return handled
        value = self.responses.get(argv)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return result(argv, 0, "")
        return value


def result(
    argv: tuple[str, ...], exit_code: int = 0, stdout: str = ""
) -> CommandResult:
    return CommandResult(
        argv=argv,
        exit_code=exit_code,
        stdout=stdout,
        stderr="",
    )


def request(
    tmp_path: Path,
    *,
    mode: str = "create",
    host_options: dict[str, object] | None = None,
    config_refs: tuple[str, ...] | None = None,
    collaborators: tuple[Collaborator, ...] = (),
) -> DeploymentRequest:
    related = SkillSelection(
        name="requirements-analysis",
        source_kind="hub-workflow",
        source="workflows/requirements-analysis",
        reason="declared",
    )
    return DeploymentRequest(
        schema_version="1.0",
        deployment_id="hermes-fixture",
        agent_id="fixture-agent",
        display_name="Fixture Agent",
        purpose="test deployment",
        host="hermes",
        mode=mode,
        primary_workflow="primary-flow",
        related_workflows=(related,),
        auxiliary_skills=(),
        workdir=str((tmp_path / "work").resolve()),
        config_refs=(
            (str((tmp_path / "private.ini").resolve()),)
            if config_refs is None
            else config_refs
        ),
        host_options=host_options or {},
        collaborators=collaborators,
    )


def test_behavior_parser_accepts_json_or_safe_python_dict_literal() -> None:
    expected = {
        "identity": "fixture-agent",
        "primary_workflow": "primary-flow",
        "first_action": "完整加载主工作流",
    }
    assert _parse_behavior_output(json.dumps(expected, ensure_ascii=False)) == expected
    assert _parse_behavior_output(repr(expected)) == expected
    assert _parse_behavior_output("not a mapping") == {}


def enablement_options(*platforms: str) -> dict[str, object]:
    return {
        "enablement": {
            "mode": "full",
            "source_profile": "active",
            "model_strategy": "managed-fields",
            "env_strategy": "full",
            "platforms": list(platforms),
            "gateway_strategy": "multiplex-routes",
            "external_resources": "check_only",
            "behavior_check": "readiness_only",
        }
    }


def prepare_enablement_source(tmp_path: Path) -> Path:
    profiles_root = (tmp_path / "profiles").resolve()
    source = profiles_root / "source-profile"
    source.mkdir(parents=True)
    (source / "config.yaml").write_text(
        "model:\n"
        "  default: fixture-model\n"
        "  provider: fixture-provider\n"
        "  api_key: SYNTHETIC_ENABLEMENT_TOKEN_MODEL\n",
        encoding="utf-8",
    )
    (source / ".env").write_text(
        "WEIXIN_TOKEN=SYNTHETIC_ENABLEMENT_TOKEN_WEIXIN\n",
        encoding="utf-8",
    )
    return profiles_root


def stage_core_files(
    staging: Path,
    requested: DeploymentRequest,
    intents,
) -> None:
    persona = render_persona(requested, snapshots()).encode("utf-8")
    marker = json.dumps(
        {
            "schema_version": requested.schema_version,
            "deployment_id": requested.deployment_id,
            "agent_id": requested.agent_id,
            "host": "hermes",
            "persona_sha256": hashlib.sha256(persona).hexdigest(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    snapshot_payloads = {
        record.sha256: (
            "---\n"
            f"name: {item.selection.name}\n"
            f"description: {item.selection.name} fixture\n"
            "---\n"
            "# Instructions\n\nUse this fixture.\n"
        ).encode()
        for item in snapshots()
        for record in item.files
    }
    for intent in intents:
        if intent.parameters.get("kind") != "file":
            continue
        if intent.parameters.get("payload_kind") in {
            "peer-collaboration",
            "hermes-bot-mode-profile",
        }:
            continue
        relative = str(intent.parameters["staging_relative"])
        target = staging.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative.endswith("SOUL.md"):
            payload = persona
        elif relative.endswith(".agent-workflow-hub-deployment.json"):
            payload = marker
        else:
            payload = snapshot_payloads[intent.content_sha256]
        target.write_bytes(payload)


def snapshot(name: str, content: bytes) -> SkillSnapshot:
    selection = SkillSelection(
        name=name,
        source_kind="hub-workflow",
        source=f"workflows/{name}",
        reason="fixture",
    )
    file = SkillFile(
        relative_path="SKILL.md",
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    return SkillSnapshot(
        selection=selection,
        files=(file,),
        tree_sha256=canonical_sha256([file.to_mapping()]),
    )


def snapshots() -> tuple[SkillSnapshot, ...]:
    def content(name: str) -> bytes:
        return (
            "---\n"
            f"name: {name}\n"
            f"description: {name} fixture\n"
            "---\n"
            "# Instructions\n\nUse this fixture.\n"
        ).encode()

    return (
        snapshot("primary-flow", content("primary-flow")),
        snapshot("requirements-analysis", content("requirements-analysis")),
    )


def discovery_responses(
    *, version: str = VERIFIED_HERMES_VERSION, profiles: str = ""
) -> dict[tuple[str, ...], CommandResult]:
    version_argv = ("hermes", "--version")
    list_argv = ("hermes", "profile", "list")
    return {
        version_argv: result(version_argv, stdout=f"Hermes Agent v{version}"),
        list_argv: result(list_argv, stdout=profiles),
    }


def business_collaborator() -> Collaborator:
    return Collaborator(
        id="test-requirements",
        agent_id="test-lifecycle-bot",
        transport="hermes-bot-mode",
        capability="business-requirement-support",
        local_role="provider",
        peer_workflow="automated-test-lifecycle",
    )


def api_server_collaborator() -> Collaborator:
    return Collaborator(
        id="business-requirements",
        agent_id="business-agent",
        transport="hermes-api-server",
        capability="business-requirement-support",
        local_role="consumer",
        peer_workflow="knowledge-support-agent",
        api_server_base_url="http://127.0.0.1:8642/p/business-agent",
        api_server_key_env="BUSINESS_AGENT_API_KEY",
    )


def prepare_api_collaboration_profiles(
    tmp_path: Path,
    requested: DeploymentRequest,
    *,
    provider_enabled: bool = True,
    provider_key: bool = True,
) -> tuple[Path, Path]:
    profiles_root = (tmp_path / "profiles").resolve()
    target = profiles_root / requested.agent_id
    provider = profiles_root / "business-agent"
    (provider / "skills" / "knowledge-support-agent").mkdir(parents=True)
    target.mkdir(parents=True)
    (target / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    (target / ".env").write_text(
        "BUSINESS_AGENT_API_KEY=SYNTHETIC_CONSUMER_KEY\n",
        encoding="utf-8",
    )
    (target / ".agent-workflow-hub-deployment.json").write_text(
        json.dumps(
            {
                "schema_version": requested.schema_version,
                "deployment_id": requested.deployment_id,
                "agent_id": requested.agent_id,
                "host": "hermes",
            }
        ),
        encoding="utf-8",
    )
    (provider / "config.yaml").write_text(
        "platforms:\n  api_server:\n"
        f"    enabled: {'true' if provider_enabled else 'false'}\n",
        encoding="utf-8",
    )
    (provider / ".env").write_text(
        "API_SERVER_KEY=SYNTHETIC_PROVIDER_KEY\n" if provider_key else "",
        encoding="utf-8",
    )
    (provider / "AGENTS.md").write_text("# Business Agent\n", encoding="utf-8")
    (provider / "skills" / "knowledge-support-agent" / "SKILL.md").write_text(
        "# Knowledge support\n",
        encoding="utf-8",
    )
    return profiles_root, target


def api_discovery_responses(
    requested: DeploymentRequest,
    target: Path,
    *,
    version: str = HERMES_RUNS_API_MIN_VERSION,
) -> dict[tuple[str, ...], CommandResult]:
    responses = discovery_responses(
        version=version,
        profiles=" fixture-agent fixture-model stopped — —\n"
        " business-agent fixture-model running — —\n",
    )
    path_argv = (
        "hermes",
        "--profile",
        requested.agent_id,
        "config",
        "path",
    )
    responses[path_argv] = result(path_argv, stdout=str(target / "config.yaml"))
    return responses


def test_private_runtime_mcp_uses_native_config_and_keeps_rollback_values(tmp_path):
    from dataclasses import replace
    import sys
    from agent_workflow_hub.specialized_agent_deployment.runtime_bundle import runtime_python
    requested = replace(request(tmp_path), runtime={"python": sys._base_executable,
        "wheelhouse": str(tmp_path / "wheels"), "destination": str(tmp_path / "release")},
        host_options={"mcp_servers": [{"workflow": "primary-flow", "server_name": "fixture",
            "command": sys.executable, "args": ["-m", "agent_workflow_hub.cli", "jenkins-mcp", str(tmp_path / "private.ini")],
            "cwd": str(tmp_path)}]})
    runner = FakeRunner(discovery_responses())
    adapter = HermesAdapter(runner, profiles_root=tmp_path / "profiles", staging_root=tmp_path / "staging")
    facts = adapter.discover(requested)
    facts = replace(facts, facts={**facts.facts, "mcp_previous": {
        "mcp_servers.fixture.command": "old-python", "mcp_servers.fixture.args": ["old-module"],
        "mcp_servers.fixture.cwd": "old-dir"}})
    writes = adapter.plan_writes(requested, snapshots(), render_persona(requested, snapshots()), facts)
    configs = {item.parameters["config_key"]: item for item in writes if item.parameters.get("kind") == "config-set"}
    assert configs["mcp_servers.fixture.command"].parameters["value"] == runtime_python(requested.runtime)
    assert json.loads(configs["mcp_servers.fixture.args"].parameters["value"])[0] == "-I"
    assert configs["mcp_servers.fixture.command"].parameters["previous_value"] == "old-python"
    assert json.loads(configs["mcp_servers.fixture.args"].parameters["previous_value"]) == ["old-module"]
    adapter._rollback_config(requested, [configs["mcp_servers.fixture.command"], configs["mcp_servers.fixture.args"]])
    assert runner.calls[-1][0][-1] == "old-python"
    assert json.loads(runner.calls[-2][0][-1]) == ["old-module"]
    assert not (tmp_path / "profiles").exists()


def test_failed_config_recovery_is_reported_as_unknown(tmp_path):
    from agent_workflow_hub.specialized_agent_deployment.filesystem import TransactionOutcomeUnknown
    from agent_workflow_hub.specialized_agent_deployment.hosts.hermes import _command_intent
    runner = FakeRunner(handler=lambda argv, phase: result(argv, 1))
    adapter = HermesAdapter(runner, profiles_root=tmp_path / "profiles", staging_root=tmp_path / "staging")
    intent = _command_intent(target=tmp_path / "config.yaml", argv=("hermes", "config", "set", "fixture", "new"),
        description="fixture", kind="config-set", extra={"config_key": "mcp_servers.fixture.command", "previous_value": "old"})
    with pytest.raises(TransactionOutcomeUnknown, match="rollback failed"):
        adapter._rollback_config(request(tmp_path), [intent])


def test_discover_uses_only_read_commands_and_marks_0206_verified(
    tmp_path: Path,
) -> None:
    runner = FakeRunner(discovery_responses())
    adapter = HermesAdapter(
        runner,
        profiles_root=(tmp_path / "profiles").resolve(),
        staging_root=(tmp_path / "staging").resolve(),
    )
    facts = adapter.discover(request(tmp_path))
    assert facts.version == VERIFIED_HERMES_VERSION
    assert facts.compatibility == "verified"
    assert facts.facts["profile_exists"] is False
    assert all(phase == "preview" for _, phase in runner.calls)
    assert not any("set" in argv for argv, _ in runner.calls)
    assert all(
        argv[-1] == "--help"
        for argv, _ in runner.calls
        if "create" in argv or "delete" in argv
    )


def test_discover_unknown_version_is_conditional_after_feature_probe(
    tmp_path: Path,
) -> None:
    adapter = HermesAdapter(
        FakeRunner(discovery_responses(version="0.20.0")),
        profiles_root=(tmp_path / "profiles").resolve(),
        staging_root=(tmp_path / "staging").resolve(),
    )
    assert adapter.discover(request(tmp_path)).compatibility == "conditional"


def test_discover_missing_host_returns_guidance(tmp_path: Path) -> None:
    runner = FakeRunner(
        {("hermes", "--version"): CommandExecutionError("missing")}
    )
    adapter = HermesAdapter(
        runner,
        profiles_root=(tmp_path / "profiles").resolve(),
        staging_root=(tmp_path / "staging").resolve(),
    )
    facts = adapter.discover(request(tmp_path))
    assert facts.compatibility == "missing"
    assert "guidance" in facts.facts


def test_plan_lists_exact_profile_files_config_and_behavior_commands(
    tmp_path: Path,
) -> None:
    private_config = tmp_path / "private.ini"
    private_config.write_text("secret=do-not-read", encoding="utf-8")
    runner = FakeRunner(discovery_responses())
    staging = (tmp_path / "staging").resolve()
    adapter = HermesAdapter(
        runner,
        profiles_root=(tmp_path / "profiles").resolve(),
        staging_root=staging,
    )
    requested = request(tmp_path)
    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )
    argvs = [tuple(item.parameters["argv"]) for item in intents if "argv" in item.parameters]
    assert (
        "hermes",
        "profile",
        "create",
        requested.agent_id,
        "--no-alias",
        "--no-skills",
    ) in argvs
    assert not any("agent.system_prompt_file" in argv for argv in argvs)
    assert (
        "hermes",
        "--profile",
        requested.agent_id,
        "config",
        "set",
        "terminal.cwd",
        requested.workdir,
    ) in argvs
    behavior = next(argv for argv in argvs if "--usage-file" in argv)
    assert behavior[-1] == str(staging / "hermes-usage.json")
    assert BEHAVIOR_PROMPT_TEMPLATE.format(
        agent_id=requested.agent_id,
        primary_workflow=requested.primary_workflow,
    ) in behavior
    targets = {Path(item.target).name for item in intents if item.action == "create"}
    assert {"SOUL.md", "SKILL.md", ".agent-workflow-hub-deployment.json"} <= targets
    assert private_config.read_text(encoding="utf-8") == "secret=do-not-read"
    plan = build_deployment_plan(requested, snapshots(), facts, intents)
    preview = render_deployment_preview(plan)
    assert '"profile","create","fixture-agent","--no-alias","--no-skills"' in preview
    assert '"config","set","terminal.cwd"' in preview


def test_full_enablement_discovery_and_plan_are_redacted(tmp_path: Path) -> None:
    profiles_root = prepare_enablement_source(tmp_path)
    requested = request(
        tmp_path,
        host_options=enablement_options("weixin"),
    )
    runner = FakeRunner(
        discovery_responses(profiles=" ◆source-profile fixture-model running alias —\n")
    )
    adapter = HermesAdapter(
        runner,
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )

    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )

    assert facts.facts["enablement"]["source_profile"] == "source-profile"
    kinds = [item.parameters.get("kind") for item in intents]
    assert kinds[-4:] == [
        "enablement-target-config",
        "enablement-env-copy",
        "enablement-gateway-config",
        "enablement-gateway-restart",
    ]
    serialized = json.dumps(
        [item.to_mapping() for item in intents],
        ensure_ascii=False,
    )
    assert "SYNTHETIC_ENABLEMENT_TOKEN_" not in serialized
    preview = render_deployment_preview(
        build_deployment_plan(requested, snapshots(), facts, intents)
    )
    assert preview.count("deployment_review") == 1
    assert "source-profile" in preview
    assert "weixin" in preview
    assert "SYNTHETIC_ENABLEMENT_TOKEN_" not in preview

    bot_mode_profile = next(
        item
        for item in intents
        if item.parameters.get("payload_kind") == "hermes-bot-mode-profile"
    )
    assert Path(bot_mode_profile.target) == profiles_root / requested.agent_id / "profile.yaml"
    staged_profile = yaml.safe_load(
        (tmp_path / "staging" / "host-files" / "profile.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert staged_profile["description"] == requested.purpose
    assert staged_profile["ui_meta"]["hermes-bots"] == {
        "title": requested.display_name,
        "description": requested.purpose,
    }


def test_full_enablement_newer_version_proceeds_after_read_only_probes(
    tmp_path: Path,
) -> None:
    profiles_root = prepare_enablement_source(tmp_path)
    requested = request(
        tmp_path,
        host_options=enablement_options(),
    )
    adapter = HermesAdapter(
        FakeRunner(
            discovery_responses(
                version="0.21.5",
                profiles=" ◆source-profile fixture-model running alias —\n",
            )
        ),
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )

    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )

    assert facts.compatibility == "conditional"
    assert facts.facts["enablement"]["source_profile"] == "source-profile"
    assert any(
        item.parameters.get("kind") == "enablement-gateway-restart"
        for item in intents
    )


def test_collaboration_updates_only_two_exact_unmanaged_peer_files(
    tmp_path: Path,
) -> None:
    profiles_root = (tmp_path / "profiles").resolve()
    peer = profiles_root / "test-lifecycle-bot"
    peer_skill = peer / "skills" / "automated-test-lifecycle"
    peer_skill.mkdir(parents=True)
    agents_path = peer / "AGENTS.md"
    skill_path = peer_skill / "SKILL.md"
    config_path = peer / "config.yaml"
    agents_path.write_text("# Existing test Agent\n", encoding="utf-8")
    skill_path.write_text("# Existing lifecycle copy\n", encoding="utf-8")
    config_path.write_text("model: fixture\n", encoding="utf-8")
    config_before = config_path.read_bytes()
    requested = request(tmp_path, collaborators=(business_collaborator(),))
    staging = (tmp_path / "staging").resolve()
    adapter = HermesAdapter(
        FakeRunner(discovery_responses()),
        profiles_root=profiles_root,
        staging_root=staging,
    )
    facts = adapter.discover(requested)

    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )

    peer_intents = [
        item for item in intents if item.parameters.get("payload_kind") == "peer-collaboration"
    ]
    assert {Path(item.target) for item in peer_intents} == {agents_path, skill_path}
    assert all(item.expected_before_sha256 for item in peer_intents)
    assert not any(Path(item.target).name == ".agent-workflow-hub-deployment.json" for item in peer_intents)
    stage_core_files(staging, requested, intents)
    plan = build_deployment_plan(requested, snapshots(), facts, intents)

    def handler(argv: tuple[str, ...], phase: str):
        if argv == ("hermes", "profile", "list"):
            return result(argv, stdout=" fixture-agent fixture-model stopped — —\n")
        return result(argv)

    outcome = HermesAdapter(
        FakeRunner(handler=handler),
        profiles_root=profiles_root,
        staging_root=staging,
    ).apply(
        ApplyContext(
            plan=plan,
            manifest=planned_manifest(plan),
            staging_root=staging,
            backup_root=(tmp_path / "backup").resolve(),
        )
    )

    assert outcome.status == "applied"
    assert "message_agent" in agents_path.read_text(encoding="utf-8")
    assert "message_agent" in skill_path.read_text(encoding="utf-8")
    assert config_path.read_bytes() == config_before
    assert not (peer / ".agent-workflow-hub-deployment.json").exists()


def test_api_server_collaboration_create_returns_two_stage_guidance(
    tmp_path: Path,
) -> None:
    requested = request(
        tmp_path,
        collaborators=(api_server_collaborator(),),
    )
    adapter = HermesAdapter(
        FakeRunner(discovery_responses(version=HERMES_RUNS_API_MIN_VERSION)),
        profiles_root=(tmp_path / "profiles").resolve(),
        staging_root=(tmp_path / "staging").resolve(),
    )

    facts = adapter.discover(requested)

    assert facts.compatibility == "unverified"
    assert any("两阶段" in item for item in facts.facts["guidance"])
    assert adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    ) == ()


def test_api_server_collaboration_update_requires_021_and_ready_profiles(
    tmp_path: Path,
) -> None:
    requested = request(
        tmp_path,
        mode="update",
        collaborators=(api_server_collaborator(),),
    )
    profiles_root, target = prepare_api_collaboration_profiles(tmp_path, requested)
    adapter = HermesAdapter(
        FakeRunner(api_discovery_responses(requested, target)),
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )

    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )

    assert facts.compatibility == "conditional"
    api_facts = facts.facts["api_server_collaboration"]
    assert api_facts["version_ready"] is True
    assert all(api_facts["bindings"][0][key] is True for key in (
        "consumer_key_configured",
        "provider_registered",
        "provider_api_server_enabled",
        "provider_key_configured",
    ))
    assert intents
    assert any(
        item.parameters.get("payload_kind") == "peer-collaboration"
        for item in intents
    )
    serialized = json.dumps(facts.to_mapping(), ensure_ascii=False)
    assert "SYNTHETIC_CONSUMER_KEY" not in serialized
    assert "SYNTHETIC_PROVIDER_KEY" not in serialized


def test_api_server_collaboration_missing_provider_readiness_is_guidance_only(
    tmp_path: Path,
) -> None:
    requested = request(
        tmp_path,
        mode="update",
        collaborators=(api_server_collaborator(),),
    )
    profiles_root, target = prepare_api_collaboration_profiles(
        tmp_path,
        requested,
        provider_enabled=False,
        provider_key=False,
    )
    adapter = HermesAdapter(
        FakeRunner(api_discovery_responses(requested, target)),
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )

    facts = adapter.discover(requested)

    assert facts.compatibility == "unverified"
    assert any("api_server.enabled" in item for item in facts.facts["guidance"])
    assert any("API_SERVER_KEY" in item for item in facts.facts["guidance"])
    assert adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    ) == ()


def test_api_server_collaboration_rejects_pre_021_host(tmp_path: Path) -> None:
    requested = request(
        tmp_path,
        mode="update",
        collaborators=(api_server_collaborator(),),
    )
    profiles_root, target = prepare_api_collaboration_profiles(tmp_path, requested)
    adapter = HermesAdapter(
        FakeRunner(
            api_discovery_responses(
                requested,
                target,
                version=VERIFIED_HERMES_VERSION,
            )
        ),
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )

    facts = adapter.discover(requested)

    assert facts.compatibility == "unverified"
    assert any(
        HERMES_RUNS_API_MIN_VERSION in item
        for item in facts.facts["guidance"]
    )


def test_full_enablement_on_019_returns_guidance_without_writes(tmp_path: Path) -> None:
    profiles_root = prepare_enablement_source(tmp_path)
    requested = request(
        tmp_path,
        host_options=enablement_options("weixin"),
    )
    adapter = HermesAdapter(
        FakeRunner(
            discovery_responses(
                version="0.19.0",
                profiles=" ◆source-profile fixture-model running alias —\n",
            )
        ),
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )

    facts = adapter.discover(requested)

    assert facts.compatibility == "compatible_not_runnable"
    assert adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    ) == ()


def test_enablement_failure_does_not_undo_core_update(tmp_path: Path) -> None:
    profiles_root = prepare_enablement_source(tmp_path)
    target = profiles_root / "fixture-agent"
    target.mkdir()
    (target / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    requested = request(
        tmp_path,
        mode="update",
        host_options=enablement_options("weixin"),
        config_refs=(),
    )
    (target / ".agent-workflow-hub-deployment.json").write_text(
        json.dumps(
            {
                "schema_version": requested.schema_version,
                "deployment_id": requested.deployment_id,
                "agent_id": requested.agent_id,
                "host": "hermes",
            }
        ),
        encoding="utf-8",
    )
    staging = (tmp_path / "staging").resolve()

    def handler(argv: tuple[str, ...], phase: str):
        if argv == ("hermes", "--version"):
            return result(argv, stdout=f"Hermes Agent v{VERIFIED_HERMES_VERSION}")
        if argv == ("hermes", "profile", "list"):
            return result(
                argv,
                stdout=" ◆source-profile fixture-model running alias —\n"
                " fixture-agent fixture-model stopped — —\n",
            )
        if argv[-2:] == ("config", "path"):
            return result(argv, stdout=str(target / "config.yaml"))
        if argv[-3:] == ("config", "get", "agent.system_prompt_file"):
            return result(argv, stdout="AGENTS.md")
        if argv[-3:] == ("config", "get", "terminal.cwd"):
            return result(argv, stdout=requested.workdir)
        if argv[-2:] == ("gateway", "restart"):
            calls = [call for call, _ in runner.calls if call[-2:] == ("gateway", "restart")]
            return result(argv, exit_code=1 if len(calls) == 1 else 0)
        return result(argv)

    runner = FakeRunner(handler=handler)
    adapter = HermesAdapter(
        runner,
        profiles_root=profiles_root,
        staging_root=staging,
    )
    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )
    stage_core_files(staging, requested, intents)
    plan = build_deployment_plan(requested, snapshots(), facts, intents)

    outcome = adapter.apply(
        ApplyContext(
            plan=plan,
            manifest=planned_manifest(plan),
            staging_root=staging,
            backup_root=(tmp_path / "core-backup").resolve(),
        )
    )

    assert outcome.status == "applied"
    assert outcome.details["enablement_status"] == "rolled_back"
    assert (target / "SOUL.md").is_file()
    assert (target / "skills" / "primary-flow" / "SKILL.md").is_file()


def test_create_failure_after_confirmed_profile_deletes_only_that_profile(
    tmp_path: Path,
) -> None:
    requested = request(tmp_path)
    profiles_root = (tmp_path / "profiles").resolve()
    target = profiles_root / requested.agent_id
    staging = (tmp_path / "staging").resolve()
    runner = FakeRunner(discovery_responses())
    adapter = HermesAdapter(
        runner,
        profiles_root=profiles_root,
        staging_root=staging,
    )
    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )
    plan = build_deployment_plan(requested, snapshots(), facts, intents)
    manifest = planned_manifest(plan)

    config_sets = 0

    def handler(argv: tuple[str, ...], phase: str):
        nonlocal config_sets
        if argv[:3] == ("hermes", "profile", "create"):
            target.mkdir(parents=True)
            return result(argv)
        if argv == ("hermes", "profile", "list"):
            return result(argv, stdout=f" {requested.agent_id}  model stopped")
        if "config" in argv and "set" in argv:
            config_sets += 1
            return result(argv, exit_code=1 if config_sets == 2 else 0)
        if argv[:3] == ("hermes", "profile", "delete"):
            return result(argv)
        return result(argv)

    apply_runner = FakeRunner(handler=handler)
    applying = HermesAdapter(
        apply_runner,
        profiles_root=profiles_root,
        staging_root=staging,
    )
    with pytest.raises(HermesAdapterError):
        applying.apply(
            ApplyContext(
                plan=plan,
                manifest=manifest,
                staging_root=staging,
                backup_root=(tmp_path / "backup").resolve(),
            )
        )
    assert (
        "hermes",
        "profile",
        "delete",
        "-y",
        requested.agent_id,
    ) in [argv for argv, _ in apply_runner.calls]


def test_create_replaces_profile_seeded_soul_with_planned_persona(
    tmp_path: Path,
) -> None:
    requested = request(tmp_path)
    profiles_root = (tmp_path / "profiles").resolve()
    target = profiles_root / requested.agent_id
    staging = (tmp_path / "staging").resolve()
    discovery = HermesAdapter(
        FakeRunner(discovery_responses()),
        profiles_root=profiles_root,
        staging_root=staging,
    )
    facts = discovery.discover(requested)
    persona = render_persona(requested, snapshots())
    intents = discovery.plan_writes(requested, snapshots(), persona, facts)
    stage_core_files(staging, requested, intents)
    plan = build_deployment_plan(requested, snapshots(), facts, intents)

    def handler(argv: tuple[str, ...], phase: str):
        if argv[:3] == ("hermes", "profile", "create"):
            target.mkdir(parents=True)
            (target / "SOUL.md").write_bytes(b"default Hermes persona\n")
            return result(argv)
        if argv == ("hermes", "profile", "list"):
            return result(argv, stdout=f" {requested.agent_id} fixture stopped\n")
        return result(argv)

    outcome = HermesAdapter(
        FakeRunner(handler=handler),
        profiles_root=profiles_root,
        staging_root=staging,
    ).apply(
        ApplyContext(
            plan=plan,
            manifest=planned_manifest(plan),
            staging_root=staging,
            backup_root=(tmp_path / "backup").resolve(),
        )
    )

    assert outcome.status == "applied"
    assert (target / "SOUL.md").read_bytes() == persona.encode("utf-8")


def test_create_unknown_result_does_not_delete_profile(tmp_path: Path) -> None:
    requested = request(tmp_path)
    profiles_root = (tmp_path / "profiles").resolve()
    staging = (tmp_path / "staging").resolve()
    discovery = HermesAdapter(
        FakeRunner(discovery_responses()),
        profiles_root=profiles_root,
        staging_root=staging,
    )
    facts = discovery.discover(requested)
    intents = discovery.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )
    plan = build_deployment_plan(requested, snapshots(), facts, intents)

    def handler(argv: tuple[str, ...], phase: str):
        if argv[:3] == ("hermes", "profile", "create"):
            return result(argv)
        if argv == ("hermes", "profile", "list"):
            return result(argv, stdout="default")
        return result(argv)

    runner = FakeRunner(handler=handler)
    applied = HermesAdapter(
        runner,
        profiles_root=profiles_root,
        staging_root=staging,
    ).apply(
        ApplyContext(
            plan=plan,
            manifest=planned_manifest(plan),
            staging_root=staging,
            backup_root=(tmp_path / "backup").resolve(),
        )
    )
    assert applied.status == "outcome_unknown"
    assert not any(argv[:3] == ("hermes", "profile", "delete") for argv, _ in runner.calls)


def test_verify_reads_config_skills_and_behavior(tmp_path: Path) -> None:
    requested = request(tmp_path)
    profiles_root = (tmp_path / "profiles").resolve()
    target = profiles_root / requested.agent_id
    staging = (tmp_path / "staging").resolve()
    adapter = HermesAdapter(
        FakeRunner(discovery_responses()),
        profiles_root=profiles_root,
        staging_root=staging,
    )
    facts = adapter.discover(requested)
    intents = adapter.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )
    plan = build_deployment_plan(requested, snapshots(), facts, intents)
    manifest = planned_manifest(plan)
    target.mkdir(parents=True)
    persona = render_persona(requested, snapshots()).encode("utf-8")
    (target / "SOUL.md").write_bytes(persona)
    (target / ".agent-workflow-hub-deployment.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "deployment_id": requested.deployment_id,
                "agent_id": requested.agent_id,
                "host": "hermes",
                "persona_sha256": hashlib.sha256(persona).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    for item in snapshots():
        root = target / "skills" / item.selection.name
        root.mkdir(parents=True)
        content = (
            "---\n"
            f"name: {item.selection.name}\n"
            f"description: {item.selection.name} fixture\n"
            "---\n"
            "# Instructions\n\nUse this fixture.\n"
        ).encode()
        (root / "SKILL.md").write_bytes(content)

    def handler(argv: tuple[str, ...], phase: str):
        if argv[-3:] == ("config", "get", "terminal.cwd"):
            return result(argv, stdout=requested.workdir)
        if "skills" in argv and "list" in argv:
            return result(argv, stdout="primary-flow\nrequirements-analysis")
        if "-z" in argv:
            return result(
                argv,
                stdout=json.dumps(
                    {
                        "identity": requested.agent_id,
                        "primary_workflow": requested.primary_workflow,
                        "first_action": "完整加载主工作流",
                    },
                    ensure_ascii=False,
                ),
            )
        return result(argv)

    verify_runner = FakeRunner(handler=handler)
    verifying = HermesAdapter(
        verify_runner,
        profiles_root=profiles_root,
        staging_root=staging,
    )
    verification = verifying.verify(
        VerifyContext(
            manifest=manifest,
            staging_root=staging,
            behavior_evidence_path=None,
        )
    )
    assert verification.status == "verified", verification.to_mapping()
    called = [argv for argv, _ in verify_runner.calls]
    assert not any("agent.system_prompt_file" in argv for argv in called)
    assert (
        "hermes",
        "--profile",
        requested.agent_id,
        "config",
        "get",
        "terminal.cwd",
    ) in called


def test_verify_reports_full_enablement_readiness_without_business_run(
    tmp_path: Path,
) -> None:
    profiles_root = prepare_enablement_source(tmp_path)
    target = profiles_root / "fixture-agent"
    target.mkdir()
    (target / "config.yaml").write_text("model: {}\n", encoding="utf-8")
    requested = request(
        tmp_path,
        mode="update",
        host_options=enablement_options("weixin"),
        config_refs=(),
    )
    Path(requested.workdir).mkdir(parents=True)
    marker = {
        "schema_version": requested.schema_version,
        "deployment_id": requested.deployment_id,
        "agent_id": requested.agent_id,
        "host": "hermes",
        "persona_sha256": hashlib.sha256(
            render_persona(requested, snapshots()).encode("utf-8")
        ).hexdigest(),
    }
    (target / "SOUL.md").write_bytes(
        render_persona(requested, snapshots()).encode("utf-8")
    )
    (target / ".agent-workflow-hub-deployment.json").write_text(
        json.dumps(marker), encoding="utf-8"
    )
    for item in snapshots():
        root = target / "skills" / item.selection.name
        root.mkdir(parents=True)
        content = (
            "---\n"
            f"name: {item.selection.name}\n"
            f"description: {item.selection.name} fixture\n"
            "---\n"
            "# Instructions\n\nUse this fixture.\n"
        ).encode("utf-8")
        (root / "SKILL.md").write_bytes(content)
    profile_list = (
        " ◆source-profile fixture-model running alias —\n"
        " fixture-agent fixture-model stopped — —\n"
    )
    projection = build_enablement_projection(
        requested,
        profiles_root=profiles_root,
        profile_list_output=profile_list,
    )
    projection.target_config_path.write_bytes(projection.target_config_bytes)
    projection.target_env_path.write_bytes(projection.env_bytes)
    projection.gateway_config_path.write_bytes(projection.gateway_config_bytes)
    (target / "profile.yaml").write_bytes(
        _bot_mode_profile_bytes(target / "profile.yaml", requested)
    )

    discovery = HermesAdapter(
        FakeRunner(
            discovery_responses(profiles=profile_list)
            | {
                (
                    "hermes",
                    "--profile",
                    requested.agent_id,
                    "config",
                    "path",
                ): result(
                    (
                        "hermes",
                        "--profile",
                        requested.agent_id,
                        "config",
                        "path",
                    ),
                    stdout=str(target / "config.yaml"),
                )
            }
        ),
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )
    facts = discovery.discover(requested)
    intents = discovery.plan_writes(
        requested,
        snapshots(),
        render_persona(requested, snapshots()),
        facts,
    )
    manifest = planned_manifest(
        build_deployment_plan(requested, snapshots(), facts, intents)
    )

    def handler(argv: tuple[str, ...], phase: str):
        if argv == ("hermes", "profile", "list"):
            return result(argv, stdout=profile_list)
        if argv[-3:] == ("config", "get", "terminal.cwd"):
            return result(argv, stdout=requested.workdir)
        if "skills" in argv and "list" in argv:
            return result(argv, stdout="primary-flow\nrequirements-analysis")
        if argv[-2:] == ("gateway", "status"):
            return result(
                argv,
                stdout="running; credentials configured; served profile fixture-agent",
            )
        if "-z" in argv:
            return result(
                argv,
                stdout=json.dumps(
                    {
                        "identity": requested.agent_id,
                        "primary_workflow": requested.primary_workflow,
                        "first_action": "完整加载主工作流",
                    },
                    ensure_ascii=False,
                ),
            )
        return result(argv)

    verifying = HermesAdapter(
        FakeRunner(handler=handler),
        profiles_root=profiles_root,
        staging_root=(tmp_path / "staging").resolve(),
    )
    verification = verifying.verify(
        VerifyContext(
            manifest=manifest,
            staging_root=(tmp_path / "staging").resolve(),
            behavior_evidence_path=None,
        )
    )

    assert verification.status == "verified", verification.to_mapping()
    assert verification.enablement.status == "verified"
    assert {item.name: item.status for item in verification.enablement.checks} == {
        "environment": "passed",
        "external_resources": "passed",
        "gateway": "passed",
        "model_and_adapters": "passed",
        "workdir": "passed",
    }
