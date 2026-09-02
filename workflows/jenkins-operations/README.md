# Jenkins Operations MCP

`jenkins-operations` 是 Agent Workflow Hub 中专用于 Jenkins 的受控 MCP。
它通过固定类型的工具读取、创建和更新 Jenkins 对象；不暴露任意 HTTP、任意
`config.xml`、Script Console 或 Jenkins CLI；标准 Groovy 与仓库 Jenkinsfile
只能通过固定的 pipeline 模板参数提交。

此 README 面向配置和维护该 MCP 的使用者；Agent 的执行约束与决策流程见
[SKILL.md](SKILL.md)。

## 能力范围

| 类别 | MCP 工具 |
| --- | --- |
| Controller 与对象读取 | `jenkins_controller_info`、`jenkins_list_nodes`、`jenkins_list_items`、`jenkins_get_item` |
| 创建受支持对象 | `jenkins_create_item`：Folder、Freestyle、Pipeline、View、Multibranch；只能使用内置版本化模板；Pipeline 支持固定模板、内联标准 Groovy 与仓库 Jenkinsfile 三种来源 |
| 安全配置变更 | `jenkins_config_snapshot`、`jenkins_config_preview`、`jenkins_update_item` |
| 构建控制 | `jenkins_trigger_build`、`jenkins_observe_build`、`jenkins_cancel_build`、`jenkins_observe_cancellation`、`jenkins_abandon_unknown` |
| 运行与插件摘要 | `jenkins_get_progressive_log`、`jenkins_pipeline_runs`、`jenkins_junit_summary`、`jenkins_multibranch_children` |

配置更新必须走“快照 → 预览 diff → 携带配置和 payload 摘要写入 → 回读”的链路。
当前受支持的受控字段为描述、启停状态、定时 cron、字符串参数与 Pipeline 的
`pipeline_definition`，具体仍由策略的 `allowed_fields` 收窄。创建或更新 Pipeline 都使用固定模板，模板定义见
[`src/agent_workflow_hub/jenkins_mcp/templates.py`](../../src/agent_workflow_hub/jenkins_mcp/templates.py)。

其中 `pipeline/pytest-inline-v1` 是为自动化测试生命周期提供的封闭模板：它接收类型化的
非空 Git 仓库 URL、凭据 ID、Agent 标签、完整 commit SHA、受控临时分支、仓库内 requirements/test 路径、
运行系统和受限 JSON pytest 选项。模板会检出分支后 detach 到精确 commit，并始终发布
`reports/junit.xml`；模板内不接受 Jenkinsfile、任意 Groovy、XML、Shell 或 pytest 参数。使用该模板的
策略规则必须显式列出 `pytest-inline-v1`，顶层在 Gate 2 中绑定完整参数和 Job 配置摘要。
策略还必须用 `parameters.<字段>.enum` 列出全部九个字段的准确允许值；调用参数的键和值必须与该
映射完全相等，缺失、额外或不同值均会被拒绝。可直接按
[jenkins-policy.yaml.example](references/jenkins-policy.yaml.example) 中的注释示例替换枚举值。

Pipeline 共有三种来源：固定模板（`pipeline-v1`、`pytest-inline-v1`）、内联标准 Groovy
（`groovy-inline-v1`，参数 `script`，XML 转义后写入 `CpsFlowDefinition` 且 `sandbox=true`，
不做语法分析或命令黑名单）以及仓库 Jenkinsfile（`jenkinsfile-scm-v1`，参数 `repository_url`、
`branch`/spec、`script_path` 默认 `Jenkinsfile`、`credentials_id` 可选，生成标准
`CpsScmFlowDefinition` + `GitSCM` XML）。三种来源都通过 `jenkins_create_item` 创建，或通过
`jenkins_update_item` 的结构化 `template_parameters` 更新。更新只替换现有 Job 唯一的顶层
definition，未修改配置保持不变；
运行/读取使用同一组 `jenkins_trigger_build`、`jenkins_observe_build`、日志与 JUnit 工具。

Pipeline 更新沿用一次 `jenkins_update_item` 确认流程，不新增 Pipeline 专属确认。

## 明确不支持的操作

- 任意 URL、REST 请求、XML、Script Console、Jenkins CLI 或绕过模板的任意 Groovy/Jenkinsfile 提交。
- Jenkins 安装、升级、重启、JCasC reload、全局安全设置、凭据、插件与节点配置。
- 删除或批量移动 Job、构件或构建记录。
- 绕过 Jenkins RBAC、读取或输出密码、Token、Cookie、CSRF crumb。

本地策略是可选的额外最小权限边界，不能替代 Jenkins 自身的 RBAC。

## 安装与启动

项目要求 Python 3.11+。在仓库根目录安装项目后，CLI 会提供 `workflow-hub`：

```powershell
uv pip install -e .
# 或：python -m pip install -e .

workflow-hub jenkins-mcp C:\path\to\jenkins.ini
```

该命令运行标准输入/输出（stdio）MCP 服务，适合作为 Codex、Hermes、OpenClaw、
Claude Code 或 OpenCode 的 MCP 子进程。它不会自动修改任何宿主的 MCP 配置；由
使用者在宿主中配置上述命令和 INI 的绝对路径。

## 配置 Jenkins Controller

从 [references/jenkins.ini.example](references/jenkins.ini.example) 复制一份仅自己可读的
INI，并按需编辑：

```ini
[jenkins]
name = staging
url = https://jenkins.example.internal
environment = nonproduction
policy_file = jenkins-policy.yaml

# HTTP 仅允许非生产 Controller；HTTPS 时不要设为 true。
allow_insecure_http = false

# 使用账号密码且 Jenkins 启用 CSRF 防护时设为 true；API Token 通常不需要。
require_crumb = true

# 推荐使用环境变量。不要把此 INI 提交到版本库。
username_env = JENKINS_USERNAME
token_env = JENKINS_API_TOKEN

# 内部 CA 可使用相对 INI 的证书路径。
# ca_bundle = certificates\jenkins-ca.pem
```

也支持在 INI 中直接提供 `username` 加 `token` 或 `password`，但不能与环境变量方式
混用，且 `token` 与 `password` 不能同时存在。凭据、Cookie 和 crumb 永不出现在 MCP
工具结果或工作流输出中。

也可以使用共享形态 `[environment] + [target.jenkins]`：host 与 port 组成 URL（默认 http，
HTTPS 可把 host 写成 `https://jenkins.example.internal`），environment 缺省为
nonproduction，可显式写 production；其他 sections 会被容忍：

```ini
[environment]
name = dev

[target.jenkins]
host = jenkins.example.internal
port = 8443
environment = nonproduction
; policy_file = jenkins-policy.yaml
; confirm_writes = true
; username = operator
; token = replace-with-jenkins-api-token
```

`[target.jenkins]` 可匿名（不填凭据字段）。policy_file 是可选的：省略时，Jenkins
账号/RBAC 是默认授权边界，读写正常执行。confirm_writes 默认 true（省略即启用）：写操作
默认先返回当前会话的一次性挑战，首次调用不解析凭据、不创建 Jenkins 客户端，也不发起网络读写；
仅显式 `confirm_writes = false` 时，Jenkins 能力才在账号/RBAC 允许下直接执行受控写。
`environment` 只能是 `nonproduction` 或 `production`。生产 Controller 必须使用 HTTPS；确认 ID
不能覆盖该硬门。

共享形态下 `allow_insecure_http` 默认 `true`（host/port 默认组成 http URL）；显式
`false` 时 http URL 会被拒绝，必须显式允许不安全 HTTP。HTTPS 主机不要显式设置
`allow_insecure_http = true`（该选项只适用于 http URL）。

## 策略文件

INI 中的 `policy_file` 是可选的；省略时，Jenkins 账号/RBAC 是默认授权边界。`confirm_writes`
省略即启用（默认 true），写操作先挑战；仅显式 `confirm_writes = false` 时独立能力直接执行受控写。
提供策略时，`policy_file` 指向 YAML 策略。可从
[references/jenkins-policy.yaml.example](references/jenkins-policy.yaml.example) 开始。
策略需要分别限定：

- Controller 名称和环境；
- 路径前缀；
- 动作，例如 `read`、`create_item`、`update_item`、`trigger_build`、`cancel_build`；
- 创建时允许的对象类型和模板；
- 更新时允许的字段，以及 Pipeline 更新模板的精确参数；
- 构建参数枚举、并发上限和可选的到期时间。

Controller、根目录任务列表和节点摘要是三个独立的全局读取作用域，分别需要策略中的
`read_scopes: [controller]`、`[root_list]`、`[nodes]`。普通 Job 读取不应借用这些作用域。

下例仅允许在非生产环境创建一个短期的 Freestyle Job，并只允许修改其 cron：

```yaml
version: 1
rules:
  - name: create-auto-run
    action: create_item
    controllers: [staging]
    environments: [nonproduction]
    path_prefixes: [auto_run]
    item_types: [freestyle]
    templates: [freestyle-v1]
    expires_at: "2026-12-31T00:00:00+00:00"

  - name: schedule-auto-run
    action: update_item
    controllers: [staging]
    environments: [nonproduction]
    path_prefixes: [auto_run]
    item_types: [freestyle]
    templates: [freestyle-v1]
    allowed_fields: [cron]
    expires_at: "2026-12-31T00:00:00+00:00"
```

策略到期后会拒绝新的写操作，但不会撤销已经写入 Jenkins 的 Job 或定时器。

## 使用方式

1. 先调用 Controller/对象读取工具，确认 Controller 可达、目标存在状态、插件兼容性和策略范围。
2. 创建对象时，选择固定的对象类型与模板。`confirm_writes` 省略即启用；首次调用返回
   `needs_user_confirmation`、`confirmation_id`、精确请求指纹和脱敏摘要；向用户展示后，以完全相同的
   参数加该 ID 重放。服务只执行一次并回读类型；仅显式 `confirm_writes = false` 时写操作直接执行。
3. 更新对象时，先取得 `jenkins_config_snapshot` 的 `digest`，再调用
   `jenkins_config_preview` 查看仅限授权字段的变更；Pipeline definition 更新同时传递固定模板的
   `template_parameters`，并由策略的 `pipeline_definition` 字段和参数 enum 精确约束。
4. 用预览返回的 `payload_digest` 和快照的 `digest` 调用 `jenkins_update_item`；`confirm_writes`
   省略即启用，展示挑战，再以相同参数和 `confirmation_id` 重放。请求、Controller
   安全配置或已加载策略变化都会让旧 ID 永久失效；回读无法验证时服务停止，不会盲目重试。
5. 配置策略时，构建触发与取消必须有独立规则，不会被普通创建/更新权限隐式授权。网络中断后的
   构建会返回 `outcome_unknown` 并保留并发租约，不自动重试。`confirm_writes` 省略即启用时
   `jenkins_abandon_unknown` 自身也先挑战；确认后只改变本地跟踪并释放租约，不重试远端写入。

`confirmation_id` 只是服务在当前进程会话内关联一次挑战的 opaque ID，不是签名或可转移凭据。
策略会在挑战前、确认消费后以及内部 POST permit 消费时复核；过期、漂移和重用都关闭式失败。

## 代码结构

| 文件 | 责任 |
| --- | --- |
| [`server.py`](../../src/agent_workflow_hub/jenkins_mcp/server.py) | MCP `@server.tool()` 声明、运行时和 stdio 服务入口 |
| [`client.py`](../../src/agent_workflow_hub/jenkins_mcp/client.py) | Jenkins HTTP、认证、TLS、CSRF crumb 与受控请求 |
| [`items.py`](../../src/agent_workflow_hub/jenkins_mcp/items.py) | Folder/View/Job 创建和回读验证 |
| [`changes.py`](../../src/agent_workflow_hub/jenkins_mcp/changes.py) | 快照、预览、摘要绑定更新与配置回读 |
| [`runs.py`](../../src/agent_workflow_hub/jenkins_mcp/runs.py) | 构建触发、观察、取消和未知结果处理 |
| [`plugins.py`](../../src/agent_workflow_hub/jenkins_mcp/plugins.py) | Pipeline、JUnit、多分支等插件能力适配 |
| [`config.py`](../../src/agent_workflow_hub/jenkins_mcp/config.py) | INI 校验与凭据解析 |
| [`policy.py`](../../src/agent_workflow_hub/jenkins_mcp/policy.py) | YAML 最小权限策略解析与匹配 |

对 Agent 暴露的 MCP 接口只在 `server.py` 的 `create_jenkins_mcp_server()` 中注册；其余
模块是该 Jenkins 工作流专属的内部实现，不是项目公共 MCP 池。

## 验证与排障

```powershell
# 检查仓库工作流与文档契约
workflow-hub validate .

# 检查指定 Controller 的基础可达性（不会写入 Jenkins）
workflow-hub doctor --host <host-name> .
```

遇到认证、TLS、网络、RBAC、策略或插件兼容性失败时，MCP 会返回脱敏后的失败状态。
应修复相应配置或权限，而不是改用更高权限账户、降低 TLS 要求或绕过策略。
