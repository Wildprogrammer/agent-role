---
name: specialized-agent-deployment
description: Use when one existing primary workflow and explicitly selected supporting Skills must be assembled as a fixed-snapshot task-specific Agent for an existing Hermes or DeepSeek Harness host.
compatibility: Agent Workflow Hub spec 1.0; supports existing Hermes Profiles and DeepSeek Harness Web user Presets without installing or upgrading either host.
metadata:
  spec-version: "1.0"
  workflow-version: "0.5.0"
  display-name: "Specialized Agent Deployment"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "explicit"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '[]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"preview":"python <HUB_ROOT>/workflows/specialized-agent-deployment/scripts/specialized_agent_deployment.py preview --hub-root <HUB_ROOT> --request <ABSOLUTE_JSON>","apply":"python <HUB_ROOT>/workflows/specialized-agent-deployment/scripts/specialized_agent_deployment.py apply --hub-root <HUB_ROOT> --manifest <ABSOLUTE_JSON> --confirmed-plan-sha256 <SHA256>","verify":"python <HUB_ROOT>/workflows/specialized-agent-deployment/scripts/specialized_agent_deployment.py verify --hub-root <HUB_ROOT> --manifest <ABSOLUTE_JSON>"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# 专用 Agent 部署

## 用途与触发条件

把一个已有主工作流、主工作流正文明确需要且本次选中的关联工作流，以及用户点名的辅助 Skill，固定为一个特定事务 Agent。首版部署目标只有既有 Hermes Profile 和 DeepSeek Harness Web 用户 Preset。

本工作流只拥有组合选择、快照绑定、部署状态和一次部署确认。各工作流仍拥有自己的业务步骤；Hermes 与 DeepSeek Harness 适配器拥有各自发现、写入和验证协议。不得复制主流程步骤，不得把宿主协议改写到本 Skill 中。

## 非目标

不安装或修改宿主程序本身，不安装全局依赖，不改变全局默认 Agent/Preset，不提供 headless 部署路径。独立部署可在预览指定的新版本目录准备 Hub 源码与副本本地依赖；只有用户明确选择 `isolated` 时才创建私有 Python 环境。不因此扩大到任意应用或模型安装。Hermes 完整启用只允许在目标 Profile 本地迁移已确认的模型配置和完整 `.env`；凭据值不得进入 Hub 输出、manifest、日志、命令参数、Git 或模型上下文。缺少可运行前提时只输出指导态。

## 输入

使用符合 `references/deployment-request.schema.json` 的绝对 JSON 文件。请求明确给出 Agent 身份、用途、宿主、create/update 模式、主工作流、关联工作流、辅助 Skill、工作目录、外置配置引用和宿主选项。

DSH/Hermes 选择需要 MCP 的工作流时，将按所属 Skill 核实的启动映射放入 `host_options.mcp_servers`，字段及示例见各宿主适配说明。这是本次宿主接入参数，不是新的工作流依赖清单；缺少已选工作流声明的 MCP 映射时，preview 会明确列出缺失项，不再生成一个只有 Skill 文件的可执行计划。

新的完整部署须使用顶层 `runtime`，默认使用系统 Python 和 `system-source`，同时复制版本化 Hub 源码、全部所选资源，并把已准备 wheelhouse 中的依赖离线安装到副本 `packages/`；用户明确要求隔离时才选 `isolated` 并创建私有环境。两种模式都不依赖开发目录；系统模式不覆盖系统 Python 中已有包，也不写系统 site-packages。只在兼容旧请求或用户明确选用引用式部署时省略 runtime。配置、用户数据与大型模型迁移单独按本次范围处理，且不增加确认门。具体见 [独立运行副本](references/standalone-runtime.md)。

主 Agent 必须完整读取主工作流的 `SKILL.md`，从正文语义识别其明确声明需要的固定关联工作流，并与用户点名的辅助 Skill 一起显式传入请求。不得新增依赖 YAML；部署 manifest 不是工作流依赖权威，也不能替代主工作流正文。

只有用户未说明且会改变 Agent 能力边界的信息才需澄清。提问应简述原因；普通澄清不是确认门。

选择 Hermes 后，先询问用户“是否需要完整启用”。如需要，再询问“选择哪些消息平台路由给该专用 Agent”，并在意图不明显时说明：这会决定哪些平台的全部入站消息切换到目标 Profile。上述问题属于能力范围澄清，不是新增确认门；不需要完整启用时继续原有核心部署，不增加任何启用动作。

## 输出与命名规则

每个 `deployment_id` 在本工作流 `outputs/` 下只生成 `deployment-preview.md`、`deployment-manifest.json` 和验证后的 `verification.json`；staging 位于 Hub `workspace/workflows/specialized-agent-deployment/<deployment_id>/`。宿主目标由适配器发现并写入预览，不从输出目录名推断。

## 依赖和运行前检查

### 部署入口与依赖收集

部署入口 `scripts/specialized_agent_deployment.py` 调用 Hub 的 `agent_workflow_hub.specialized_agent_deployment` 包，需要项目支持的 Python、可导入的配套 Hub 包及其 `pyproject.toml` 声明的依赖。完整 Hub 中脚本会定位 `src`；单独的 Skill 快照不包含该实现。用实际解释器检查脚本 `--help` 和包位置，再按目标宿主适配文档检查已准备的 Hermes 或 DeepSeek Harness 运行时。preview 会产生预览文件，不把它当成无写入 doctor。

对显式选中的工作流，以及复合流程声明且本次实际使用的子流程，读取各自 Skill 的依赖章节和其引用的 capability/安装说明。不能只看 `required-capabilities`：空列表仍可能依赖 Hub 源码包、宿主原生工具或上游工作流。区分三类条件：基础必需、选用分支才需、已有替代路线；不安装未选用的模型、CLI 或服务。

在现有预览/交付说明中记录实际解释器与包来源、入口/资产路径、宿主工具接入、检查结果及未验证范围，不新增依赖 YAML 或额外确认门，也不记录凭据值。CLI/库验证实际命令或导入，MCP 验证完整握手、工具发现以及目标 Agent 内的只读调用，纯 Skill 验证宿主读取所需材料的能力；不能把三者混作同一种检查。能复用且环境未变的证据直接复用，安装与宿主写入仍按已有部署计划和授权执行。

要求 Hub 校验通过、所有显式选择的 Skill 可完整快照，并且目标 Hermes 或 DeepSeek Harness 已由用户准备。先只读发现版本、目标状态和所需功能；不满足前提时保持 guidance_only 或 compatible_not_runnable。

主 Agent 还须读取所选工作流自己的运行依赖、入口与配置要求，分别检查运行环境、服务可启动性和目标 Agent 的工具可见性。不得把 Skill 快照齐全视为依赖就绪，也不新增重复的依赖 YAML。选择 `jenkins-operations` 时，按该 Skill 引用的运行依赖与接入验证执行三层检查；Jenkins 协议仍由 Jenkins 工作流拥有，宿主映射由适配器拥有。

DSH/Hermes 适配器可在同一次已确认部署中生成显式请求的 stdio MCP 映射，并检查部署后的映射内容；独立运行副本负责 Hub 包和版本副本内的 Python 依赖，不修改其他宿主。主 Agent 必须在预览和交付说明中列出缺失项及已检查范围。静态映射检查仍不能替代服务完整握手和目标 Agent 实际调用；具体边界见 [宿主行为验证](references/host-behavior-verification.md)。

## 系统修改与权限影响

preview 只写 Hub 输出和 staging。用户确认后，apply 只执行预览绑定的宿主精确动作；宿主自身账号、文件权限和配置权限仍是授权边界。工作流不扩大权限。完整启用可修改目标 Profile 模型/平台配置和活动 Profile 的 Gateway 路由，但外部资源只读，不创建或修改 Jenkins、Git、数据库及其他业务资源。

## 执行步骤

1. 澄清 Agent 用途、已有宿主、create/update、主工作流和本次显式 Skill 组合。
2. Hermes 请求澄清是否需要完整启用；需要时选择消息平台。完整启用保留活动 Profile 的单一 Gateway，通过 `gateway.multiplex_profiles` 和 `gateway.profile_routes` 路由，不启动第二 Gateway。
3. 对每个 Skill 的完整目录做稳定快照，生成最小 Persona，并只读发现宿主事实。
4. 运行 `preview`，在 Hub 内生成部署预览、planned manifest 和 staging；此步骤不写宿主。
5. 展示精确计划和 plan SHA，完成唯一一次部署确认。
6. 将相同 plan SHA 传给 `apply`。apply 前重新快照、重新发现和重新规划；发生漂移时停止并重新预览，不写宿主。独立部署先准备并验证新运行副本，通过后才切换宿主引用；失败保留现有 Agent 与旧版本。
7. apply 后立即分层验证身份文件、Skill 快照、宿主发现和最小行为冒烟；完整启用只做 readiness-only 就绪检查，不启动主工作流，也不向真实消息平台发送测试消息。需要补充行为证据时运行 `verify`。

## 人工确认门

唯一确认节点是 `deployment_review`：用户确认预览中列出的精确宿主写入、一次最小行为验证、事务备份和事务内回滚。是否完整启用和平台选择只是能力范围澄清，不是新增确认门。plan SHA 只绑定这次已确认预览，不建立第二套确认存储，也不增加逐文件确认。

大影响、删除或批量操作不属于本工作流的自动执行范围；普通澄清不是确认门。

## 失败恢复

静态、发现和行为验证分别记录。Hermes 在 apply 后运行禁止工具调用的最小身份冒烟；DeepSeek Harness 缺少人工 Web 行为证据时必须是 `partially_verified`，不得伪造完整通过。

核心部署事务与 Hermes 完整启用事务状态独立。启用失败只回滚模型、`.env`、目标平台禁用和活动 Gateway 路由，不删除已经部署的 Profile、Skills 或 Persona；不追加确认。结果为 `outcome_unknown` 时只报告并回读对账，不自动重放、覆盖或删除。源、宿主事实或计划漂移会使旧确认失效。

## 重跑、幂等与覆盖策略

更新只接受带匹配管理标记的既有目标，不接管未知 Agent。不自动更新宿主、Skill 或已部署 Agent；需要变更时重新生成并确认新预览。

相同 planned manifest 只能在 planned 状态 apply。成功、回滚或未知结果均不自动重放；重新部署必须重新只读发现并生成新 plan SHA。create 不覆盖既有目标，update 不接管缺少匹配标记的目标。

## 清理方式

不自动删除、不卸载、不批量清理，也不创建 rule.md。输出和 staging 默认保留；用户需要清理时按 Hub 的精确清理规则另行处理。

## 验收标准

预览阶段宿主零写入；可执行计划只有一个确认节点；apply 必须绑定相同 plan SHA 且漂移发生在宿主写入前被拒绝；部署后得到诚实的 verified、partially_verified、rolled_back 或 outcome_unknown 结果。Hermes 完整启用使用单一 Gateway、外部资源只读和 readiness-only 验证。DSH 更新保留未选中修改的插件条目，所请求的 MCP 映射部署后须完整回读；仅静态映射和身份通过时保持 partially_verified，实际业务工具验证另行记录。主工作流与关联 Skill 仍由各自文件保持权威。
