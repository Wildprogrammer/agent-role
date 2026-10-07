from __future__ import annotations

import hashlib
from pathlib import Path

from agent_workflow_hub.specialized_agent_deployment.collaboration import (
    _section,
    augment_peer_file,
    deployed_skill_payload,
    specialize_skill_snapshots,
)
from agent_workflow_hub.specialized_agent_deployment.contracts import (
    Collaborator,
    DeploymentRequest,
    SkillSelection,
)
from agent_workflow_hub.specialized_agent_deployment.sources import snapshot_composition


def request(hub_root: Path) -> DeploymentRequest:
    return DeploymentRequest(
        schema_version="1.0",
        deployment_id="business-support",
        agent_id="business-agent",
        display_name="Business Agent",
        purpose="answer business questions",
        host="hermes",
        mode="create",
        primary_workflow="knowledge-support-agent",
        related_workflows=(),
        auxiliary_skills=(),
        workdir=str(hub_root.resolve()),
        config_refs=(),
        host_options={},
        collaborators=(
            Collaborator(
                id="test-requirements",
                agent_id="test-lifecycle-bot",
                transport="hermes-bot-mode",
                capability="business-requirement-support",
                local_role="provider",
                peer_workflow="automated-test-lifecycle",
            ),
        ),
    )


def make_hub(tmp_path: Path) -> Path:
    root = tmp_path / "hub"
    skill = root / "workflows" / "knowledge-support-agent"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: knowledge-support-agent\ndescription: fixture\n---\n\n# Workflow\n",
        encoding="utf-8",
    )
    return root.resolve()


def test_specialization_changes_only_deployed_primary_skill_copy(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    requested = request(hub)
    source_path = hub / "workflows" / "knowledge-support-agent" / "SKILL.md"
    before = source_path.read_bytes()
    source = snapshot_composition(hub, requested)

    deployed = specialize_skill_snapshots(hub, requested, source)
    payload = deployed_skill_payload(
        hub,
        requested,
        source[0],
        deployed[0],
        "SKILL.md",
    )

    assert source_path.read_bytes() == before
    assert deployed[0].tree_sha256 != source[0].tree_sha256
    assert hashlib.sha256(payload).hexdigest() == deployed[0].files[0].sha256
    text = payload.decode("utf-8")
    assert text.count("agent-workflow-hub:collaboration:test-requirements:start") == 1
    assert "message_agent" in text
    assert "ANSWERED" in text
    assert "NEED_USER" in text
    assert "CONFLICT" in text


def test_peer_augmentation_is_idempotent_and_reverses_role() -> None:
    requested = request(Path("C:/fixture"))
    original = b"# Existing persona\r\n\r\nKeep this.\r\n"

    once = augment_peer_file(original, requested, requested.collaborators[0])
    twice = augment_peer_file(once, requested, requested.collaborators[0])

    assert twice == once
    assert once.startswith(original)
    text = once.decode("utf-8")
    assert text.count("agent-workflow-hub:collaboration:test-requirements:start") == 1
    assert "business-agent" in text
    assert "业务规则、用户行为或预期结果" in text
    assert "技术实现、工具或环境问题" in text
    assert "不得用 `terminal`" in text
    assert "嵌套启动 Hermes" in text


def _api_server_request(hub_root: Path) -> DeploymentRequest:
    return DeploymentRequest(
        schema_version="1.0",
        deployment_id="lifecycle",
        agent_id="test-lifecycle-bot",
        display_name="Lifecycle Bot",
        purpose="run test lifecycle",
        host="hermes",
        mode="create",
        primary_workflow="automated-test-lifecycle",
        related_workflows=(),
        auxiliary_skills=(),
        workdir=str(hub_root.resolve()),
        config_refs=(),
        host_options={},
        collaborators=(
            Collaborator(
                id="test-requirements",
                agent_id="autotestplatform-business-support",
                transport="hermes-api-server",
                capability="business-requirement-support",
                local_role="consumer",
                peer_workflow="knowledge-support-agent",
                api_server_base_url="http://127.0.0.1:8642/p/autotestplatform-business-support",
                api_server_key_env="BUSINESS_SUPPORT_API_KEY",
            ),
        ),
    )


def test_api_server_contract_validation() -> None:
    import pytest

    from agent_workflow_hub.specialized_agent_deployment.contracts import (
        DeploymentContractError,
    )

    # api-server 仅限 consumer
    with pytest.raises(DeploymentContractError):
        Collaborator(
            id="x",
            agent_id="peer",
            transport="hermes-api-server",
            capability="business-requirement-support",
            local_role="provider",
            peer_workflow="wf",
            api_server_base_url="http://127.0.0.1:8642/p/peer",
            api_server_key_env="PEER_KEY",
        )
    # api-server 必须提供端点与密钥变量
    with pytest.raises(DeploymentContractError):
        Collaborator(
            id="x",
            agent_id="peer",
            transport="hermes-api-server",
            capability="business-requirement-support",
            local_role="consumer",
            peer_workflow="wf",
        )
    # bot-mode 不允许带 api_server 字段
    with pytest.raises(DeploymentContractError):
        Collaborator(
            id="x",
            agent_id="peer",
            transport="hermes-bot-mode",
            capability="business-requirement-support",
            local_role="consumer",
            peer_workflow="wf",
            api_server_base_url="http://127.0.0.1:8642/p/peer",
        )
    for invalid_url in (
        "https://127.0.0.1:8642/p/peer",
        "http://attacker.example/p/peer",
        "http://127.0.0.1:8642/p/another-peer",
        "http://127.0.0.1:8642/p/peer?token=x",
        "http://localhost:/p/peer",
        "http://user@127.0.0.1:8642/p/peer",
    ):
        with pytest.raises(DeploymentContractError):
            Collaborator(
                id="x",
                agent_id="peer",
                transport="hermes-api-server",
                capability="business-requirement-support",
                local_role="consumer",
                peer_workflow="wf",
                api_server_base_url=invalid_url,
                api_server_key_env="PEER_KEY",
            )
    with pytest.raises(DeploymentContractError):
        Collaborator(
            id="x",
            agent_id="peer",
            transport="hermes-api-server",
            capability="business-requirement-support",
            local_role="consumer",
            peer_workflow="wf",
            api_server_base_url=123,  # type: ignore[arg-type]
            api_server_key_env="PEER_KEY",
        )
    with pytest.raises(DeploymentContractError):
        Collaborator(
            id="x",
            agent_id="peer",
            transport="hermes-api-server",
            capability="business-requirement-support",
            local_role="consumer",
            peer_workflow="wf",
            api_server_base_url="http://127.0.0.1:8642/p/peer",
            api_server_key_env="NOT-A-VARIABLE",
        )


def test_api_server_transport_roundtrip_and_section() -> None:
    requested = _api_server_request(Path("C:/fixture"))
    collab = requested.collaborators[0]

    mapping = collab.to_mapping()
    assert mapping["transport"] == "hermes-api-server"
    assert mapping["api_server_base_url"].endswith("/autotestplatform-business-support")
    assert mapping["api_server_key_env"] == "BUSINESS_SUPPORT_API_KEY"
    # 从 mapping 重建应等价
    assert Collaborator.from_mapping(mapping) == collab

    original = b"# Persona\r\n\r\nBody\r\n"
    once = augment_peer_file(original, requested, collab)
    twice = augment_peer_file(once, requested, collab)
    assert twice == once  # 幂等
    # peer（provider）侧文件：只含应答协议，不含 HTTP 调用模板
    peer_text = once.decode("utf-8")
    assert peer_text.count("agent-workflow-hub:collaboration:test-requirements:start") == 1
    assert "/v1/runs" not in peer_text
    assert "ANSWERED" in peer_text

    # consumer 自己部署技能里的 HTTP 通道文案
    consumer_text = _section(
        collaboration_id="test-requirements",
        local_agent="test-lifecycle-bot",
        peer_agent="autotestplatform-business-support",
        role="consumer",
        newline="\n",
        transport=collab.transport,
        api_server_base_url=collab.api_server_base_url,
        api_server_key_env=collab.api_server_key_env,
    )
    text = consumer_text
    assert text.count("agent-workflow-hub:collaboration:test-requirements:start") == 1
    # HTTP 通道四要素
    assert "http://127.0.0.1:8642/p/autotestplatform-business-support/v1/runs" in text
    assert "Authorization: Bearer ${BUSINESS_SUPPORT_API_KEY}" in text
    assert "/v1/runs/$RID" in text
    assert '"session_id"' in text
    assert '"conversation"' not in text
    for terminal_status in (
        "completed",
        "failed",
        "cancelled",
        "interrupted",
        "waiting_for_approval",
    ):
        assert terminal_status in text
    assert "NEED_USER" in text and "CONFLICT" in text
    # 明确禁止越界行为
    assert "run-workflow-script.py" in text
    assert "hermes ... chat" in text

    # provider 侧文案不含任何传输调用细节（只声明应答协议）
    provider_text = _section(
        collaboration_id="test-requirements",
        local_agent="autotestplatform-business-support",
        peer_agent="test-lifecycle-bot",
        role="provider",
        newline="\n",
        transport="hermes-api-server",
        api_server_base_url="http://127.0.0.1:8642/p/autotestplatform-business-support",
        api_server_key_env="BUSINESS_SUPPORT_API_KEY",
    )
    assert "/v1/runs" not in provider_text
    assert "ANSWERED" in provider_text
