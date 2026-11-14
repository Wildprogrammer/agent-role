# DeepSeek Harness Web 用户 Preset 适配范围

当前支持 DeepSeek Harness `0.2.1-rc.5` 与 `0.2.2-rc.1` Web Profile。发现阶段核对已安装包版本、Web bundles 和对应版本的 `dsh-web-app/presets/standard.patch.yml` 摘要。支持官方默认配置及显式设置 `patchReload: live` 的配置。`0.1.2-alpha.2` 和 `0.1.5-rc.2` 仍走兼容路径，并要求 `runtime_root` 和源码及构建证据。

0.2 使用 `@deepseek-ai/dsh-agent-preset`（单数）声明。Hub 在 `${DSH_HOME}/.agent-presets/<agent-id>/` 保存固定 Skill 快照、渲染后的 `preset.patch.yml` 和管理标记，并在 Web Profile 的 `cordis.patch.yml` 中维护对应的 `preset-<agent-id>`。混合版本或模板摘要漂移时返回 `unverified`。同名冲突或共享条目与受管副本不一致时拒绝规划。共享 patch 的前置摘要和目标摘要进入 plan SHA。apply 期间请勿在 DSH Web 编辑器中同步保存该 Profile。

`0.2.2-rc.1` 由 `@deepseek-ai/dsh-tool-subagent-control` 根入口注册 `list_agents`。如果旧 Preset 仍指向已移除的 `@deepseek-ai/dsh-tool-subagent-control/list-agents`，删除这一插件条目并保留根入口，随后重新加载 Preset。该版本的子 Agent 工具也移除了 `persona` 和 `toolFilter` 字段。需要固定委派目标时，先把 Persona 和工具配置放进目标 Preset，再用 `agentPreset` 选择。

`host_options.schedule: {"enabled": true}` 是可选能力开关。启用时 Preset 保留 `dsh-time-context` 与 `dsh-tool-schedule`，从而向该 Agent 暴露新版 `schedule_create/list/update/delete`；缺省或 `false` 时不挂载这两项。该开关只赋予模型管理提醒的能力，不替用户创建具体提醒，也不修改宿主级 `storages/schedule.json`。

```json
{"host_options":{"dsh_home":"<ABSOLUTE_DSH_HOME>","profile":"web","schedule":{"enabled":true}}}
```

旧版 Web 运行契约以 `${DSH_HOME}/profiles/node_modules/@deepseek-ai/` 中实际安装的核心包、Persona Schema 和 `dsh-agent-presets/presets/standard/` 模板为准。当前锁定支持 `0.1.2-alpha.2`（Persona 字段 `text`）与 `0.1.5-rc.2`（Persona 字段 `prefix`）；整个发现过程只读，不运行 npm、pnpm、npx 或构建脚本。

构建产物缺失但源码与实际 Profile 契约精确匹配时返回 `compatible_not_runnable`，提示用户按官方文档在工作流外准备现有 runtime。源码、Profile 包、Persona Schema、模板或 Web Profile 不匹配时不写用户 Preset。

旧版可运行时在 `${DSH_HOME}/.agent-presets/<agent-id>/` 创建或更新受管理的 `agent.cordis.yml`、`preset.yml`、`skills/` 和管理标记；0.2 使用前述 `preset.patch.yml` 与共享 Profile 中的受管单条注册。独立部署另在预览指定的新版本目录创建[独立运行副本](standalone-runtime.md)。不修改标准模板或全局默认 Preset。update 只接受匹配管理标记的既有目标。

Web 行为验证需要用户选择该 Preset 后执行固定无工具提示，并提供 session id、preset id、prompt SHA、response SHA 和解析结果。证据缺失时状态必须保持 `partially_verified`。

## Agent 间持续协作

旧版中，为一个测试 Agent 绑定固定业务解答 Agent 时，在目标 Preset 用 `@deepseek-ai/dsh-tool-subagent` 注册独立命名的 `ask_business_agent`，设置 `provider: spawn`、`backgroundMode: continuable`、固定业务 Persona、递归深度和工具过滤。业务 Persona 使用已部署业务 Agent 的 Skill 与运行副本。升级到 0.2.2 时，按上文迁移旧版委派配置。

DSH 的 `continuable` 是后台默认值，不是强制值。若模型传入 `run_in_background: false`，原生工具会执行前台一次性子 Agent，并在结果返回后销毁会话。部署因此必须同时完成两项约束：Persona 与目标 Skill 副本要求首次调用省略 `run_in_background` 或传 `true`；部署副本本地守卫在 `tools/pre-execute` 拒绝 `ask_business_agent` 的 `false` 调用并提示以后台模式重试。守卫只约束固定业务工具，不影响普通 `subagent`。

后台调用返回的持续子 Agent id 必须由父 Agent 保存。同一测试任务的后续业务问题、用户确认反馈和经验回流都通过 `send_message` 发给该 id；等待完成通知属于内部调度，不询问用户，也不增加确认门。验证须创建新父会话，先证明 `false` 被守卫拒绝，再证明后台调用在 `subagent/catalog` 中记录 `mode: continuable`，最后用 `send_message` 对同一 id 完成一次追问。只看答案内容或子 Agent 数量不能代替 mode 和 id 对账。

## MCP 接入

本机 DSH 已包含 `@deepseek-ai/dsh-mcp-client`，但不会默认启用服务器。部署请求的 `host_options.mcp_servers` 可显式绑定本次选中的工作流，例如：

```json
{
  "workflow": "jenkins-operations",
  "server_name": "jenkins",
  "command": "C:/runtimes/hub/Scripts/python.exe",
  "args": ["-m", "agent_workflow_hub.cli", "jenkins-mcp", "C:/configs/jenkins.ini"],
  "cwd": "C:/work/agent-role"
}
```

示例路径须替换成用户实际环境，命令及参数以所属工作流的入口说明为准。`workflow` 必须属于本次 Skill 组合，`server_name` 在当前 Agent 中唯一；输入 `command`、`cwd` 使用已存在的绝对路径。配置文件路径同时列入 `config_refs`，只引用文件，不把密码、token、header 或环境变量值放入参数、Persona、预览或日志。选择顶层 `runtime` 后，Hub 服务的实际映射改为新副本：默认由系统 Python 执行 `run-workflow-hub.py`，明确选 isolated 时才使用私有 Python；两者都先验证再写入 Preset。引用式旧请求仍要求服务已准备好。不会安装或升级 DSH，也不将其它 CLI 强制包装为 MCP。

preview 检查 MCP 插件构建产物、命令文件和工作目录，并将映射内容与写入摘要纳入原有 plan SHA。声明了 MCP 入口的已选工作流若遗漏映射，返回指导态，指出需要补充的工作流；这项就绪检查不增加用户确认节点，也不要求安装未选用的服务。

apply 使用 DSH 原生 stdio 桥接。0.2 将 `hub-mcp-<server_name>` 写入目标 Preset，旧版则写入 `agent.cordis.yml`。更新前查看现有映射，按最终请求提供本 Preset 要使用的全部映射。

verify 回读并比较预期映射，缺失或不一致为 failed；映射一致记录 `discovery.mcp.status=configured_not_probed`。它不冒充 DSH 已加载工具，也不探测真实业务，因此即使身份冒烟通过，存在 MCP 映射时整体仍为 `partially_verified`。随后按所属工作流分别完成 MCP 握手/工具发现和目标 Agent 的最小只读调用，在交付说明记录证据；不要把伪造的业务证据填进身份冒烟字段。已打开的会话不保证重载新 Preset；验证时在 Web 中重新选择该 Preset 创建新会话。
