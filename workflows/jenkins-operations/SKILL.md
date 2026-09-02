---
name: jenkins-operations
description: Use when an agent must inspect, create, or update explicitly scoped Jenkins folders, views, jobs, and runs through a policy-controlled typed MCP without installing Jenkins or bypassing Jenkins RBAC.
compatibility: Agent Workflow Hub spec 1.0; requires a user-managed Jenkins Controller and an optional locally configured Jenkins MCP policy.
metadata:
  spec-version: "1.0"
  workflow-version: "0.3.0"
  display-name: "Jenkins Operations"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '["app.jenkins"]'
  config-templates: '{"main":"references/jenkins.ini.example","policy":"references/jenkins-policy.yaml.example"}'
  config-requirements: '{"main":{"scope":"repository-external","required":true},"policy":{"scope":"repository-external","required":false}}'
  entrypoints: '{"mcp":"workflow-hub jenkins-mcp <ABSOLUTE_INI>"}'
  roles: '["roles/jenkins-operator.md"]'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---
# Jenkins 运维

## 用途与触发条件

用于已明确范围的 Jenkins Controller 操作：读取 Controller、节点摘要、Folder、View、Job、构建、队列、
日志和制品元数据；在策略允许的路径中创建 Folder、View、Freestyle、Pipeline 或 Multibranch
Job；以及修改受支持的描述、参数、触发器、启停状态和 Pipeline definition。Pipeline 的全部受支持
来源都可通过同一组固定模板创建或更新。
本工作流保持查、增、改的固定边界；`trigger_build` 和 `cancel_build` 不能被普通 CRU 预授权。

每次任务至少给出 Controller 标识、目标路径、操作类型、目标 Job 类型或模板、字段/参数，以及
是否为生产目标。Agent 在使用工作流角色前必须通过 `load_role_snapshot` 取得该角色的内容和 digest。

## 非目标

- 不自动下载 Jenkins、不自动安装 Jenkins，不升级、启动、停止或配置 Jenkins 服务。
- 不提供任意 REST 请求、任意 XML、Script Console 或任意 Jenkins CLI 命令；标准 Groovy 只能通过固定 `groovy-inline-v1` 模板提交，由 Jenkins sandbox/RBAC 约束。
- 不管理插件、凭据、节点、全局安全、JCasC reload、Controller 重启、删除或批量移动。
- 不以本地策略替代或绕过 Jenkins RBAC，也不读取、复制或输出 Token、密码、Cookie、crumb。
- 不自动注册或改写宿主 MCP 映射；宿主映射由用户在相应 Agent 中明确配置。

## 输入

用户或其受控配置必须提供一个现有的绝对 INI 路径。支持两种配置形态且互相兼容：旧形态
`[jenkins]` 保留 Controller 名称、URL、`environment`、可选的 `policy_file` 和必要的 TLS 选项；
共享形态使用 `[environment] + [target.jenkins]`：`[environment]` 提供共享环境名称，
`[target.jenkins]` 用 host 与 port 组成 URL（默认 http，HTTPS 可把 host 写成
`https://jenkins.example.test`），environment 缺省为 nonproduction，可显式写 production。
共享形态的 `allow_insecure_http` 默认 `true`（因为默认 URL 是 http）；显式
`false` 时 http URL 会被拒绝，必须显式允许不安全 HTTP。
其他 sections（如 `[target.redis]`）会被容忍。推荐从 `references/jenkins.ini.example` 复制；
同一 INI 也可作为环境验证工作流的输入。认证可匿名；填写 `username + token` 或
`username + password` 时使用该账号，不打印凭据；也可使用完整的 `username_env + token_env`。
三种方式不得混用，`token` 与 `password` 不得并存。密码认证的 Jenkins 若启用 CSRF 防护，可在
INI 显式设置 `require_crumb = true`；API Token 通常无需 crumb。

policy_file 是可选的：省略策略时，Jenkins 账号/RBAC 是默认授权边界，读写正常执行；
提供策略时，策略路径只能引用该 INI 声明的 Controller，并至少限定 Controller、环境级别、
Item 路径、`read`、`create_item`、`update_item`、`trigger_build`、`cancel_build` 动作、
模板、可修改字段、参数范围、并发上限与有效期。confirm_writes 默认 true（省略即启用）：写操作
默认先返回当前会话的挑战/重放确认；仅显式 `confirm_writes = false` 时，Jenkins 能力才在
Jenkins 账号/RBAC 允许下直接执行受控写。目标环境由 Controller 配置确定，调用方不得自行传入
或覆盖；首版不提供批量写操作。

创建 Job 必须提供受支持的类型化模板及其参数。更新必须提供目标路径、允许字段和预期当前摘要；
不能接受提交 `config.xml` 或任意 URL；标准 Groovy 与仓库 Jenkinsfile 只能通过固定的 pipeline 模板参数提交。
Pipeline definition 更新使用可选的结构化 `template_parameters`，逻辑字段固定为
`pipeline_definition`；策略必须同时允许该字段、目标模板和全部精确参数。

自动化测试生命周期可使用固定 `pipeline/pytest-inline-v1` 模板。它只接收非空 Git 仓库 URL（GitSCM 负责有效性）、
Jenkins 凭据 ID、受限 Agent 标签、完整小写 commit SHA、受控临时分支、仓库内
requirements/test 路径、`linux` 或 `windows` 和受限 JSON `pytest_args`。模板先按临时分支
检出，再 detach 到精确 commit，始终发布 `reports/junit.xml`；不接收 Jenkinsfile、Groovy、
XML、脚本片段或任意 pytest 命令。调用前需由顶层工作流把完整模板参数和 Job 配置摘要展示并绑定到 Gate 2。
对应策略规则也必须以 `parameters.<字段>.enum` 同时列出这九个字段的精确允许值；策略的参数键和值
必须与调用完全一致，不存在"缺字段时采用默认值"或按名称模糊匹配的路径。

## Pipeline 三种来源

Pipeline Job 支持三种来源，都通过 `jenkins_create_item` 或 `jenkins_update_item` 的
`item_type=pipeline` + `template` 创建或更新：

- 固定模板：`pipeline-v1`（占位脚本）与 `pytest-inline-v1`（受控 pytest 执行），只接收模板定义的参数。
- 内联标准 Groovy：`groovy-inline-v1`，接收非空标准 Groovy `script` 参数，XML 转义后写入 `CpsFlowDefinition` 且
  `sandbox=true`；Jenkins 自身 sandbox/RBAC 是执行边界，不做自定义 Groovy 语法分析或命令黑名单。
- 仓库 Jenkinsfile：`jenkinsfile-scm-v1`，接收 `repository_url`、`branch`/spec、可选 `script_path`
  （空则默认 `Jenkinsfile`）与可选 `credentials_id`（空则省略 `credentialsId` 元素），生成标准
  `CpsScmFlowDefinition` + `GitSCM` XML。

对应策略规则必须以 `templates` 列出所选模板，并以 `parameters.<字段>.enum` 列出该模板全部参数的精确允许值；
策略的参数键和值必须与调用完全一致。`trigger_build`、`read` 与 JUnit 读取对三种来源的 Job 使用同一组工具。

更新时 `template_parameters = null` 表示不修改 definition，显式空映射用于 `pipeline-v1`，非空映射按
对应模板校验。服务从模板结果中只替换现有 Job 唯一的顶层 `<definition>`；描述、properties、触发器、
启停状态、插件扩展和其他未修改配置保持不变。快照和预览不以空参数渲染参数化模板，写前绑定当前配置
和 payload 摘要，写后回读 definition 并验证未修改配置。

## 输出与命名规则

默认在当前对话或 MCP 结构化结果中输出脱敏摘要：目标、策略决定、变更 diff、执行状态、回读证据、
Jenkins 版本/插件兼容性和下一步。除非用户明确要求保存，不创建输出文件；若用户要求保存，只能写入
`workflows/jenkins-operations/outputs/<run-id>/`，且不含任何秘密或完整敏感日志。

## 依赖和运行前检查

先执行只读 preflight：验证 Capability、Controller URL、TLS、已配置认证方式的完整性（不读取或回显其值）、
`X-Jenkins` 版本头、身份最小 API 访问和已安装插件快照。Jenkins 缺失或 Controller 不可达时，
按照 `app.jenkins` 合约生成当前用户语言的安装/排障指导，而不是自行安装。

Jenkins MCP 使用项目锁定的 Python 依赖。安装该项目的普通 Python 依赖可遵循项目通用的受控
`pip`/`uv` 规则；这不授权安装 Jenkins 服务或改写宿主 MCP 映射。

## 系统修改与权限影响

读取不改变 Controller。创建或更新只有在 Jenkins RBAC（以及本地策略，若已配置）均允许时才执行；
更新前读取、比较并输出规范化 diff，更新后必须回读验证。`trigger_build` 和 `cancel_build` 是独立
动作，必须分别约束环境、Job、分支、参数和并发。五个固定写工具 `jenkins_create_item`、
`jenkins_update_item`、`jenkins_trigger_build`、`jenkins_cancel_build`、`jenkins_abandon_unknown`
在 `confirm_writes` 省略或为 true 时先返回 `needs_user_confirmation`，且首次调用不会创建客户端、
解析凭据或访问 Jenkins；仅当该 Controller 显式配置 `confirm_writes = false` 时，五个写工具才在
Jenkins 账号/RBAC 允许时直接执行。

Pipeline definition 更新复用同一个 `jenkins_update_item` 挑战与重放流程，不新增 Pipeline 专属确认。

挑战返回当前会话的短时、一次性 `confirmation_id`、精确请求指纹和完整脱敏摘要。`confirmation_id`
不是密码学凭据，也不是策略授权；调用方须在用户看过摘要后，以完全相同的参数加该 ID 重放同一工具。
服务会重新执行 HTTPS 硬门、策略复核与上下文复核，原子消费挑战，再签发只供内部 POST 使用的一次性 permit。
任何参数、Controller 安全配置或已加载策略漂移，或 ID 过期、重用，都会关闭式失败。生产 Controller
必须使用 HTTPS；显式 `allow_insecure_http` 仅适用于非生产本地/测试 Controller，确认不能覆盖该硬门。
共享形态默认 `allow_insecure_http = true`，显式 `false` 会拒绝 http URL。
构建驱动须将本地并发租约保持到 Jenkins 的远端结果明确；`outcome_unknown` 不自动释放或重触发。

## 执行步骤

1. 校验用户范围、配置路径和目标环境；加载工作流角色 snapshot。
2. 执行只读 preflight，获得 Controller、认证和插件兼容性证据。
3. 对查操作执行类型化读取并脱敏返回。
4. 对增操作校验路径、模板、参数与策略；若 `confirm_writes = true`，展示挑战；用户确认后以相同参数和
   `confirmation_id` 重放，创建后回读目标。
5. 对改操作读取当前摘要、生成 diff、检测冲突；若 `confirm_writes = true`，展示挑战并完成精确重放后写入和回读验证。
6. 对构建、队列与日志操作使用类型化驱动；只有 `trigger_build` 或 `cancel_build` 被分别允许时
   才执行。提交构建后发生网络异常时返回 `outcome_unknown`，不自动再次触发。

## 人工确认门

- Gate A：`confirm_writes` 省略即启用（默认 true）；写操作默认需当前会话确认，首次调用只展示
  脱敏摘要和精确请求 SHA-256。仅显式 `confirm_writes = false` 时在 Jenkins 账号/RBAC
  允许下直接执行。
- Gate P：策略只决定允许或拒绝的范围与风险，不能签发或替代用户确认。策略外、过期、目标不明或
  参数越界时在挑战或消费前停止；若已经提供 ID，该 ID 同时失效。
- Gate C：任何将来的删除、移动、插件、凭据、节点、全局安全、JCasC reload、重启或脚本请求，
  都不属于本工作流首版，必须转为独立高风险设计。

## 失败恢复

配置、TLS、网络、认证、RBAC 或策略不通过时，不尝试替代账号、降级 TLS 或扩大请求范围。输出失败
类别、脱敏证据和用户可采取的下一步。未知插件或版本不匹配时标为 `unsupported`；可查询官方文档后
通过新增专用驱动和测试支持，不能临时猜测接口。

写入请求超时或连接中断时，先根据请求标识、队列与构建原因查找结果；无法确认时标为
`outcome_unknown` 并保留并发租约，不自动重试。`jenkins_abandon_unknown` 也先挑战；确认后只释放
本地跟踪，不再次发送远端写入。任何替代操作都应在完成只读对账后作为新的写挑战开始。

## 重跑、幂等与覆盖策略

查操作可重跑。创建操作先检查路径与名称冲突；更新操作将预期当前摘要绑定到本次修改，若目标已变更
则返回冲突。构建触发没有通用幂等保证，网络异常后不可盲重试。不会覆盖用户已有配置、输出或策略文件。

## 验收标准

Hub 合约验证通过；Controller preflight 能在没有秘密泄露的情况下确认版本与访问条件；允许范围内的
Folder/View/Job 可创建且回读；允许范围内的 Job 更新产生 diff 并回读一致；策略外与未知插件操作被
明确拒绝；异常构建请求返回 `outcome_unknown` 而非重复触发。

## 清理方式

默认不写入文件。仅可清理用户明确指定且已确认的可重建输出目录；不得删除 Jenkins 服务、Jenkins
Home、Controller 配置、Job、制品、构建记录、外部策略或凭据文件。

## MCP 接入与策略边界

使用用户指定的仓库外 INI 启动受限 MCP 服务：

```powershell
workflow-hub jenkins-mcp <绝对 INI 路径>
```

该命令只启动 stdio 服务，绝不改写 Codex、Hermes、OpenClaw、Claude Code 或 OpenCode 的 MCP 配置。
`src/agent_workflow_hub/jenkins_mcp/` 是本 Jenkins 工作流的专属自建 MCP 实现；其中多个 Python 模块
共同组成一个服务，不是项目公共 MCP 池。策略模板位于 `references/jenkins-policy.yaml.example`。
由用户在目标宿主中创建映射，并只暴露需要的固定类型工具。服务没有原始 HTTP、任意 URL、XML、
Script Console 或通用 CLI 工具；Groovy 与 Jenkinsfile 只能通过固定的 pipeline 模板参数提交。

所有读取同样受 `read` 策略限制。普通 Job/日志/插件读取使用默认 `item` 作用域；Controller 状态、根目录
枚举和节点摘要需要显式的独立作用域，避免把全局读取误当成某个同名 Job 的读取授权：

```yaml
- name: controller-health
  action: read
  controllers: [staging]
  environments: [nonproduction]
  path_prefixes: [scope.controller]
  read_scopes: [controller]

- name: root-list
  action: read
  controllers: [staging]
  environments: [nonproduction]
  path_prefixes: [scope.root]
  read_scopes: [root_list]

- name: node-summary
  action: read
  controllers: [staging]
  environments: [nonproduction]
  path_prefixes: [scope.nodes]
  read_scopes: [nodes]
```

没有 `read_scopes` 的既有读取规则只适用于默认 `item` 作用域。`scope.controller` 与 `scope.root`
以及 `scope.nodes` 只是在策略匹配时使用的保留占位路径；只有同时匹配相应 `read_scopes` 才能授予
全局读取，不能授权同名真实 Job。节点工具只返回名称、在线/临时离线状态、执行器数量和空闲状态；
不读取节点配置、文件路径、监控明细、环境变量或凭据。

标准 stdio MCP 可以直接完成当前会话的挑战/重放流程。五个写工具仅新增可选 `confirmation_id`；读取、
快照、预览和观察工具不接受该字段，固定工具名称集合不变。`confirm_writes` 省略即启用（默认 true），
首次写调用返回 `needs_user_confirmation`，其中摘要明确展示 target、environment、action、object、
关键参数脱敏、risk、rollback/reconcile 和精确请求指纹；仅显式 `confirm_writes = false` 时独立能力
直接执行受控写。确认后只把服务返回的 ID 加到原调用；不得改参数、缓存 ID 到其他会话或把它当作密码学凭据。
