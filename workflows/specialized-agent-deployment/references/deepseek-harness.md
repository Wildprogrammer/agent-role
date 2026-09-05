# DeepSeek Harness Web 用户 Preset 适配范围

当前唯一基线为官方 DeepSeek Harness `0.1.2-alpha.2`、提交 `0a53fb55bea101816fa226bb964ae2bed71c343b` 和锁定模板摘要。标准模板位于 `packages/preset/agent-presets/presets/standard/`；旧版 `apps/cli/config/agent-presets/standard/` 不再受支持。只读发现核对 origin、HEAD、工作树、模板、Web Profile 与构建产物；不会运行 npm、pnpm、npx 或构建脚本。

构建产物缺失但版本和模板精确匹配时返回 `compatible_not_runnable`，提示用户按官方文档在工作流外准备现有 runtime。版本、提交、模板或 Web Profile 不匹配时不写用户 Preset。

可运行时在 `${DSH_HOME}/.agent-presets/<agent-id>/` 创建或更新受管理的 `agent.cordis.yml`、`preset.yml`、`skills/` 和管理标记；独立部署另在预览指定的新版本目录创建[独立运行副本](standalone-runtime.md)。不修改标准模板、Web Profile 或全局默认 Preset。update 只接受匹配管理标记的既有目标。

Web 行为验证需要用户选择该 Preset 后执行固定无工具提示，并提供 session id、preset id、prompt SHA、response SHA 和解析结果。证据缺失时状态必须保持 `partially_verified`。

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

apply 在目标 `agent.cordis.yml` 增加或更新 `hub-mcp-<server_name>` 条目，使用 DSH 原生 stdio 桥接；用户其它插件、注释及 `!!js` 表达式保留。已有同名 namespace 属于另一条目时明确报冲突，不覆盖它。主 Agent 应先查明既有映射并复用或协商调整命名，不重复接入。更新不自动删除本次未指定的旧映射。

verify 回读并比较预期映射，缺失或不一致为 failed；映射一致记录 `discovery.mcp.status=configured_not_probed`。它不冒充 DSH 已加载工具，也不探测真实业务，因此即使身份冒烟通过，存在 MCP 映射时整体仍为 `partially_verified`。随后按所属工作流分别完成 MCP 握手/工具发现和目标 Agent 的最小只读调用，在交付说明记录证据；不要把伪造的业务证据填进身份冒烟字段。已打开的会话不保证重载新 Preset；验证时在 Web 中重新选择该 Preset 创建新会话。
