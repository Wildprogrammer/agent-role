# Hermes Profile 适配范围

核心部署的当前已验证 Hermes Agent 基线为 `0.20.6`；`0.19.0` 继续由既有核心部署回归覆盖，但不提供完整启用。其他版本只有在只读 feature probe 证明所需命令和 multiplex 路由能力完整时才能继续，否则输出指导态。

## 核心部署

create 使用隔离 Profile，不创建 alias，不复制默认 Skills。Persona 写入目标 Profile 的 `AGENTS.md`，固定 Skill 快照位于 Profile 的 `skills/`，工作目录通过 Hermes 原生 config 命令设置。update 只接受带匹配 Agent Workflow Hub 管理标记的 Profile。

预览只运行版本、Profile 列表、必要帮助、配置读取和 Skill 列表等只读命令。apply 才执行预览列出的 create/config/file 动作。行为验证使用一次 `-z` 无工具身份冒烟并将 usage 记录写到 Hub staging。

## 可选完整启用

核心部署也支持顶层 `runtime` 的[独立运行副本](standalone-runtime.md)，不必启用模型或 Gateway 迁移。默认由系统 Python 运行源码副本，用户明确要求隔离时才新建私有环境；资源副本先验证，随后才设置 Profile 的 Persona 和 MCP 引用。

`host_options.mcp_servers` 使用与 DSH 相同的显式字段（`workflow`、`server_name`、`command`、`args`、`cwd`），适配器通过 Hermes 原生 `config set mcp_servers.<name>.<key>` 设置 command/args/cwd，并保留旧值用于事务恢复；不整文件替换用户配置。Hub MCP 默认改用系统 Python加版本副本启动器，isolated 模式才改用 runtime 私有 Python；第三方 MCP 不改写。适配器回读映射，不把无工具身份冒烟作为真实工具调用证据；配置了 MCP 时保留 `partially_verified`。既有 Hermes 不会因准备 DSH 部署而被修改。

`host_options.enablement` 缺失或使用 `mode: none` 时，行为与核心部署完全一致。`mode: full` 首版固定使用：

- `source_profile: active`
- `model_strategy: managed-fields`
- `env_strategy: full`
- `gateway_strategy: multiplex-routes`
- `external_resources: check_only`
- `behavior_check: readiness_only`

preview 将活动 Profile 解析为精确名称。活动源 Profile 与目标 Profile 相同时只返回指导，不生成迁移写计划。

模型只迁移 `model.default`、`provider`、`base_url`、`api_key` 和 `api_mode` 等受管理字段；目标未知字段保持原值。完整 `.env` 由宿主本地按摘要复制。秘密值不得进入 WriteIntent、预览、manifest、日志、命令参数、Git 或 Hub staging。

完整启用保留活动 Profile 的单一 Gateway，并设置 `gateway.multiplex_profiles: true`。每个用户选中平台增加确定性的 `gateway.profile_routes` 平台级路由，目标为专用 Agent Profile；不执行 `hermes profile use`，也不启动第二 Gateway。

目标 Profile 只提供 Agent 运行时。对从源配置、源 `.env` 和用户选择中发现的所有平台，目标配置必须写成：

```yaml
platforms:
  telegram:
    enabled: false
  weixin:
    enabled: false
```

平台连接始终由活动 Gateway 拥有。未知同平台路由、指向其他 Profile 的具体路由、未知路由字段或受管理名称冲突都会阻止 preview；不得静默覆盖或删除。

## 事务与验证

核心部署先完成，完整启用随后执行独立事务。秘密备份只保存在目标 Profile 的 `.agent-workflow-hub-transaction/<plan-sha256>/`，验证通过后清理；启用失败只恢复目标模型/平台配置、目标 `.env` 和活动 Gateway 路由，不删除核心 Profile、Skills 或 Persona。

Gateway 或文件写入结果未知时保留证据并进入 `outcome_unknown`，先回读对账，不盲目重放。就绪验证检查模型摘要、`.env` 摘要、目标平台禁用、路由、Gateway 状态、工作目录和无工具身份；不执行真实业务流程、不发送真实消息。外部资源只调用其所属工作流的只读检查入口，没有入口时记录 `not_checked`。
