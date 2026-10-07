"""Deterministic deployment-copy specialization for Agent collaboration."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from .contracts import (
    Collaborator,
    DeploymentRequest,
    SkillFile,
    SkillSnapshot,
    canonical_sha256,
)
from .sources import resolve_skill_source


class CollaborationError(ValueError):
    """Raised when a collaboration copy cannot be rendered safely."""


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _section(
    *,
    collaboration_id: str,
    local_agent: str,
    peer_agent: str,
    role: str,
    newline: str,
    transport: str = "hermes-bot-mode",
    api_server_base_url: str | None = None,
    api_server_key_env: str | None = None,
) -> str:
    start = f"<!-- agent-workflow-hub:collaboration:{collaboration_id}:start -->"
    end = f"<!-- agent-workflow-hub:collaboration:{collaboration_id}:end -->"
    if role == "provider":
        channel_text = (
            "Hermes Desktop Bot Mode 中，"
            f"Agent `{peer_agent}` 可通过 `message_agent` 咨询"
            if transport == "hermes-bot-mode"
            else "请求可从 Desktop `message_agent` 或已配置的本机 api_server 到达；"
            f"Agent `{peer_agent}` 可咨询"
        )
        body = (
            f"## 部署协作扩展：{collaboration_id}{newline}{newline}"
            f"当前 Agent `{local_agent}` 是业务需求解答方。{channel_text}业务规则、用户行为或预期结果。"
            f"{newline}{newline}"
            "回答必须只采用以下一种状态，并提供可核查来源位置："
            f"`ANSWERED`（答案与来源）、`NEED_USER`（已检查范围、证据不足点和应询问用户的问题）、"
            f"`CONFLICT`（冲突证据与待选择项）。{newline}"
            "收到用户确认后的反馈时，按当前知识工作流的反馈重入规则处理，不虚构已入库结果。"
        )
    elif role == "consumer" and transport == "hermes-bot-mode":
        body = (
            f"## 部署协作扩展：{collaboration_id}{newline}{newline}"
            f"当前 Agent `{local_agent}` 是业务需求咨询方。仅当业务规则、用户行为或预期结果不明确时，"
            f"才在 Hermes Desktop Bot Mode 中通过 `message_agent` 咨询 Agent `{peer_agent}`；"
            f"技术实现、工具或环境问题由当前 Agent 自行处理。{newline}{newline}"
            "`message_agent` 是 canonical `Bot Chat` 的当前会话内建工具；不得用 `terminal`、Shell、"
            f"嵌套启动 Hermes CLI 或创建中间文件来代发或探测。工具不可用时直接报告通信不可用。{newline}{newline}"
            "收到 `ANSWERED` 时引用其来源并继续；收到 `NEED_USER` 或 `CONFLICT` 时暂停不确定分支、"
            f"向用户提问。用户确认后，将确认答案通过 `message_agent` 反馈给业务 Agent，再继续原流程。"
        )
    elif role == "consumer" and transport == "hermes-api-server":
        if not api_server_base_url or not api_server_key_env:
            raise CollaborationError(
                "hermes-api-server section requires api_server_base_url and api_server_key_env"
            )
        base_url = api_server_base_url.rstrip("/")
        body = (
            f"## 部署协作扩展：{collaboration_id}{newline}{newline}"
            f"当前 Agent `{local_agent}` 是业务需求咨询方。仅当业务规则、用户行为、平台功能事实或预期结果"
            f"不明确时，才咨询 Agent `{peer_agent}`；与本次任务直接相关的技术操作（CI、Git、脚本、环境）"
            f"由当前 Agent 自行处理。判断不清时默认咨询，不得自行读代码代答。{newline}{newline}"
            "传输方式按会话类型二选一："
            f"{newline}{newline}"
            "1. canonical Bot Chat 会话（Desktop）：使用内建 `message_agent` 工具。"
            f"{newline}"
            "2. 网关/微信等非 Bot Chat 会话（无 message_agent）：只能通过本机 api_server 调用，"
            f"固定端点 `{base_url}/v1/runs`（{peer_agent} profile 前缀）。必须严格按以下四步执行："
            f"{newline}"
            "   a. 用 write_file 把请求体写到本 Agent 自己工作目录的临时文件："
            f'{{"input":"<业务问题全文>","session_id":"<本工作流会话的稳定唯一标识>"}}{newline}'
            "后续追问必须复用同一个 `session_id`，不得把每次请求改成新会话。"
            f"{newline}"
            "   b. 提交（curl，git-bash）："
            f'{newline}   RID=$(curl --fail-with-body --silent --show-error --max-time 30 -X POST "{base_url}/v1/runs" \\'
            f'{newline}     -H "Authorization: Bearer ${{{api_server_key_env}}}" \\'
            f"""{newline}     -H "Content-Type: application/json" --data-binary @"<请求文件绝对路径>" \\"""
            f"{newline}     | python -c \"import json,sys;print(json.load(sys.stdin)['run_id'])\"){newline}"
            f"   c. 每 10 秒轮询 `GET {base_url}/v1/runs/$RID`（同 Authorization 头），"
            "设置有限总等待时间和重试次数；status 为 `completed`、`failed`、`cancelled`、"
            "`interrupted` 或 `waiting_for_approval` 时立即停止轮询。`waiting_for_approval` 表示需要提供方人工介入；"
            "其他非 completed 终态按通信未完成报告。禁止无限等待，禁止改用其他端点或端口。"
            f"{newline}"
            "   d. 从返回 JSON 的 output 字段取答案；答案必须是 ANSWERED/NEED_USER/CONFLICT 三态之一。"
            f"{newline}{newline}"
            f"绝对禁止（任何会话类型、任何理由）：读取或执行对端 profile 目录下的文件与脚本（如 "
            f"run-workflow-script.py、knowledge_support_agent.py）；向对端工作目录写文件；"
            "用 Shell 嵌套启动 `hermes ... chat` 或创建中间会话文件；"
            f"输出、记录 {api_server_key_env} 明文。业务证据只能来自 api_server 返回的 output；"
            "两种通道都不可用时直接报告通信不可用。"
            f"{newline}{newline}"
            "收到 `ANSWERED` 时引用其来源并继续；收到 `NEED_USER` 或 `CONFLICT` 时暂停不确定分支、"
            "向用户提问（网关会话中通过原平台提问）。用户确认后，将确认答案经原通道反馈给业务 Agent，"
            "再继续原流程。"
        )
    else:
        raise CollaborationError(f"unsupported collaboration role/transport: {role}/{transport}")
    return newline.join((start, body, end))


def _replace_or_append(original: bytes, section: str, collaboration_id: str) -> bytes:
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise CollaborationError("collaboration target must be UTF-8") from None
    newline = _newline(text)
    start = f"<!-- agent-workflow-hub:collaboration:{collaboration_id}:start -->"
    end = f"<!-- agent-workflow-hub:collaboration:{collaboration_id}:end -->"
    start_index = text.find(start)
    end_index = text.find(end)
    if (start_index < 0) != (end_index < 0):
        raise CollaborationError("incomplete managed collaboration section")
    if start_index >= 0:
        end_index += len(end)
        if text.find(start, start_index + len(start)) >= 0 or text.find(end, end_index) >= 0:
            raise CollaborationError("duplicate managed collaboration section")
        rendered = text[:start_index] + section + text[end_index:]
    else:
        separator = "" if not text else newline * (1 if text.endswith(("\n", "\r")) else 2)
        rendered = text + separator + section + newline
    return rendered.encode("utf-8")


def _render_local(original: bytes, request: DeploymentRequest) -> bytes:
    payload = original
    for collaborator in request.collaborators:
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise CollaborationError("collaboration target must be UTF-8") from None
        payload = _replace_or_append(
            payload,
            _section(
                collaboration_id=collaborator.id,
                local_agent=request.agent_id,
                peer_agent=collaborator.agent_id,
                role=collaborator.local_role,
                newline=_newline(text),
                transport=collaborator.transport,
                api_server_base_url=collaborator.api_server_base_url,
                api_server_key_env=collaborator.api_server_key_env,
            ),
            collaborator.id,
        )
    return payload


def augment_peer_file(
    original: bytes,
    request: DeploymentRequest,
    collaborator: Collaborator,
) -> bytes:
    """Return one peer file with the opposite, idempotent collaboration role."""

    role = "consumer" if collaborator.local_role == "provider" else "provider"
    try:
        text = original.decode("utf-8")
    except UnicodeDecodeError:
        raise CollaborationError("collaboration target must be UTF-8") from None
    return _replace_or_append(
        original,
        _section(
            collaboration_id=collaborator.id,
            local_agent=collaborator.agent_id,
            peer_agent=request.agent_id,
            role=role,
            newline=_newline(text),
            transport=collaborator.transport,
            api_server_base_url=collaborator.api_server_base_url,
            api_server_key_env=collaborator.api_server_key_env,
        ),
        collaborator.id,
    )


def _source_payload(
    hub_root: Path,
    snapshot: SkillSnapshot,
    relative_path: str,
) -> bytes:
    record = next(
        (item for item in snapshot.files if item.relative_path == relative_path),
        None,
    )
    if record is None:
        raise CollaborationError(f"unplanned Skill file: {relative_path}")
    source_root = resolve_skill_source(hub_root, snapshot.selection)
    source = source_root.joinpath(*PurePosixPath(relative_path).parts)
    try:
        payload = source.read_bytes()
    except OSError as exc:
        raise CollaborationError(f"cannot read Skill source: {source}: {exc}") from None
    if len(payload) != record.size or hashlib.sha256(payload).hexdigest() != record.sha256:
        raise CollaborationError(f"Skill source drifted: {source}")
    return payload


def specialize_skill_snapshots(
    hub_root: Path,
    request: DeploymentRequest,
    source_snapshots: tuple[SkillSnapshot, ...],
) -> tuple[SkillSnapshot, ...]:
    """Hash the exact Skill bytes destined for the host without changing sources."""

    if not request.collaborators:
        return source_snapshots
    deployed: list[SkillSnapshot] = []
    for snapshot in source_snapshots:
        if snapshot.selection.name != request.primary_workflow:
            deployed.append(snapshot)
            continue
        files: list[SkillFile] = []
        for record in snapshot.files:
            payload = _source_payload(hub_root, snapshot, record.relative_path)
            if record.relative_path == "SKILL.md":
                payload = _render_local(payload, request)
            files.append(
                SkillFile(
                    relative_path=record.relative_path,
                    size=len(payload),
                    sha256=hashlib.sha256(payload).hexdigest(),
                )
            )
        file_tuple = tuple(files)
        deployed.append(
            SkillSnapshot(
                selection=snapshot.selection,
                files=file_tuple,
                tree_sha256=canonical_sha256([item.to_mapping() for item in file_tuple]),
            )
        )
    return tuple(deployed)


def deployed_skill_payload(
    hub_root: Path,
    request: DeploymentRequest,
    source_snapshot: SkillSnapshot,
    deployed_snapshot: SkillSnapshot,
    relative_path: str,
) -> bytes:
    """Rebuild and validate one plan-bound host Skill payload."""

    payload = _source_payload(hub_root, source_snapshot, relative_path)
    if request.collaborators and source_snapshot.selection.name == request.primary_workflow and relative_path == "SKILL.md":
        payload = _render_local(payload, request)
    record = next(
        (item for item in deployed_snapshot.files if item.relative_path == relative_path),
        None,
    )
    if record is None or len(payload) != record.size or hashlib.sha256(payload).hexdigest() != record.sha256:
        raise CollaborationError("deployed Skill payload does not match its plan")
    return payload


__all__ = [
    "CollaborationError",
    "augment_peer_file",
    "deployed_skill_payload",
    "specialize_skill_snapshots",
]
