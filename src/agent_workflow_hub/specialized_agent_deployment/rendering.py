"""Deterministic Chinese persona and deployment preview rendering."""

from __future__ import annotations

import json
from pathlib import Path

from .contracts import DeploymentPlan, DeploymentRequest, SkillSnapshot
from .runtime_bundle import runtime_python


class DeploymentRenderingError(ValueError):
    """Raised when rendering inputs are inconsistent."""


def _expected_skill_names(request: DeploymentRequest) -> tuple[str, ...]:
    return (
        request.primary_workflow,
        *(item.name for item in request.related_workflows),
        *(item.name for item in request.auxiliary_skills),
    )


def _validate_snapshot_composition(
    request: DeploymentRequest,
    snapshots: tuple[SkillSnapshot, ...],
) -> None:
    if type(request) is not DeploymentRequest:
        raise DeploymentRenderingError("request must be a DeploymentRequest")
    if not all(type(item) is SkillSnapshot for item in snapshots):
        raise DeploymentRenderingError("snapshots must be typed Skill snapshots")
    actual = tuple(item.selection.name for item in snapshots)
    if actual != _expected_skill_names(request):
        raise DeploymentRenderingError(
            "snapshot composition does not match the deployment request"
        )


def render_persona(
    request: DeploymentRequest,
    snapshots: tuple[SkillSnapshot, ...],
) -> str:
    """Render identity and routing only; workflow logic remains in Skills."""

    snapshots = tuple(snapshots)
    _validate_snapshot_composition(request, snapshots)
    skill_names = "、".join(item.selection.name for item in snapshots)
    config_lines = (
        "\n".join(f"- `{path}`" for path in request.config_refs)
        if request.config_refs
        else "- 无"
    )
    runtime_text = ""
    if request.runtime is not None:
        python_label = "系统 Python" if request.runtime["mode"] == "system-source" else "私有 Python"
        python_path = (
            request.runtime["python"]
            if request.runtime["mode"] == "system-source"
            else runtime_python(request.runtime)
        )
        runtime_text = (
            "\n独立运行副本（执行工作流时使用，不回退到开发仓库）：\n"
            f"- HUB_ROOT：`{Path(request.runtime['destination']) / 'hub'}`\n"
            f"- {python_label}：`{python_path}`\n"
            "- Skill 中的 `<HUB_ROOT>` 和 Python 命令使用上述路径；脚本、模板、角色资源从此副本读取。\n"
        )
    return (
        f"# {request.display_name}\n\n"
        f"你是专用于“{request.purpose}”的 Agent。\n\n"
        f"默认主工作流：`{request.primary_workflow}`。\n\n"
        "收到任务后，先完整加载默认主工作流的 `SKILL.md`，再严格按主 Skill "
        "明确声明调用相关 Skill；不得自行复制、改写、跳过或重排工作流规范。\n\n"
        f"本次固定 Skill 集：{skill_names}。\n\n"
        f"工作目录：`{request.workdir}`。\n\n"
        "外置配置引用（只按路径使用，不在此复制配置正文）：\n"
        f"{config_lines}\n"
        f"{runtime_text}"
    )


def render_deployment_preview(plan: DeploymentPlan) -> str:
    """Render all facts covered by the single deployment review."""

    if type(plan) is not DeploymentPlan:
        raise DeploymentRenderingError("plan must be a DeploymentPlan")
    request = plan.request
    skill_lines = "\n".join(
        f"- `{item.selection.name}`：来源 `{item.selection.source}`；"
        f"tree SHA `{item.tree_sha256}`；{len(item.files)} 个文件"
        for item in plan.snapshots
    )
    write_lines = "\n".join(
        f"- `{item.action}` `{item.target}`；内容 SHA `{item.content_sha256}`；"
        f"{item.size} bytes；{item.description}；参数 `"
        f"{json.dumps(item.to_mapping()['parameters'], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)}`"
        for item in plan.writes
    ) or "- 无宿主写入；当前结果仅提供用户准备指导。"
    config_lines = (
        "\n".join(f"- `{path}`" for path in request.config_refs)
        if request.config_refs
        else "- 无"
    )
    host_facts = json.dumps(
        plan.host_facts.to_mapping()["facts"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    review = (
        "## deployment_review\n\n"
        "确认本预览意味着授权其中列出的宿主写入、验证会话、备份与事务内回滚；"
        "若列有 runtime，也包含新版本目录内的运行依赖准备、资源复制和 MCP 握手/工具发现；"
        "不授权安装或升级宿主、写入凭据、覆盖未知目标、卸载或执行业务任务。\n"
        if plan.writes
        else "## 指导态\n\n宿主尚未满足部署前提；准备完成后重新生成预览。\n"
    )
    return (
        "# 专用 Agent 部署预览\n\n"
        "## Agent 身份\n\n"
        f"- deployment_id：`{request.deployment_id}`\n"
        f"- agent_id：`{request.agent_id}`\n"
        f"- 显示名称：{request.display_name}\n"
        f"- 用途：{request.purpose}\n"
        f"- 主工作流：`{request.primary_workflow}`\n"
        f"- 模式：`{request.mode}`\n\n"
        "## 固定 Skill 快照\n\n"
        f"{skill_lines}\n\n"
        "## 工作目录与配置引用\n\n"
        f"- 工作目录：`{request.workdir}`\n"
        f"{config_lines}\n\n"
        "## 宿主事实\n\n"
        f"- 宿主：`{plan.host_facts.host}`\n"
        f"- 兼容状态：`{plan.host_facts.compatibility}`\n"
        f"- 版本：`{plan.host_facts.version or 'unknown'}`\n"
        f"- 目标根：`{plan.host_facts.target_root or 'unavailable'}`\n"
        f"- 只读事实：`{host_facts}`\n\n"
        "## 精确动作和写入\n\n"
        f"{write_lines}\n\n"
        "## 验证、记录与恢复范围\n\n"
        "- 行为冒烟会产生一次模型调用，并留下宿主会话记录；不执行主工作流的真实业务任务。\n"
        "- 更新时只备份清单列明的受管理文件；新建时只跟踪当前事务创建的精确文件。\n"
        "- 失败时只在同一事务内回滚受管理文件；结果未知时先回读对账，不盲目重放或删除。\n\n"
        + ("- 独立运行副本先准备和验证，成功才切换宿主；安装失败不切换，失败新目录及旧版本保留，不自动删除。\n\n" if request.runtime else "")
        +
        "## 计划绑定\n\n"
        f"- plan_sha256：`{plan.plan_sha256}`\n"
        f"- Persona SHA：`{plan.persona_sha256}`\n\n"
        f"{review}"
    )


__all__ = [
    "DeploymentRenderingError",
    "render_deployment_preview",
    "render_persona",
]
