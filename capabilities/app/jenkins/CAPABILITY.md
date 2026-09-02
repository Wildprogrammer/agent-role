---
spec_version: "1.0"
id: "app.jenkins"
type: "app"
locked_version: "jenkins-2.568.1"
version_requirement: ">=2.479.0"
recommended_version: "2.568.1"
official_source: "https://get.jenkins.io/war-stable/2.568.1/jenkins.war"
official_docs: "https://www.jenkins.io/doc/book/installing/"
license: "MIT"
last_verified: "2026-07-23"
integrity:
  method: "sha256"
  value: "58f24f3965fbef7708629fbe158d51bf138ffd577cadbc86b46367e8ad0beb83"
  locked_version: "jenkins-2.568.1"
  source: "https://www.jenkins.io/download/"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "documented", arm64: "documented"}
  runtimes: ["Java >=21"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "curl -sS -D - <configured-controller>/api/json"
permissions:
  - "read configured Controller metadata"
  - "create or update policy-approved Jenkins items through the Jenkins MCP"
network:
  required_for_install: true
  required_for_core_use: true
data_access:
  - "configured Controller metadata and item configuration"
  - "build, queue, log and artifact metadata allowed by Jenkins RBAC"
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "conditional"
---
# Jenkins

## Purpose

Provide a Jenkins Controller that the Jenkins Operations workflow can inspect and,
when the Controller's own RBAC and the local operation policy both allow it, use for
typed create/read/update operations and minimal read-only node summaries.

## Install

Jenkins is a user-managed system service. The Agent first performs read-only
detection. It never downloads, installs, upgrades, starts, stops, reconfigures, or
registers Jenkins. If Jenkins is unavailable, it writes guidance in the current user
language that references the locked official artifact, Java requirement, checksum,
disk/network effects, permissions, rollback, and read-only verification.

## Security

Use Controller-specific least-privilege Jenkins credentials stored in a user-owned
INI outside this repository. The INI may use either direct credentials or environment
variable references, but never both for the same Controller. Raw usernames, tokens,
passwords, crumbs, cookies, and sensitive build parameters must never be written to
the repository, output, logs, or audit results. Local policy never grants permissions
that Jenkins RBAC does not grant.

The typed MCP server runs over user-configured stdio. It can perform only explicit
policy-approved reads and nonproduction writes. Production writes are conditional:
they require a valid current-session confirmation. The server presents the redacted
request summary and issues a short-lived, single-use `confirmation_id` challenge
held in the process-local `SessionConfirmationStore`; the user replays the identical
request with that `confirmation_id` and the store consumes it exactly once. Without
a valid, unconsumed current-session confirmation the write is not executed. When
the lifecycle merges a confirmation set, each member keeps its own single-use
confirmation and is released individually. This mechanism is fully in-process and
does not depend on the MCP host. A generic MCP `accept` response, Boolean tool
parameter, token, or external helper script is not a confirmation mechanism.

## Success

The configured Controller returns a Jenkins version header and a read-only API
response. A host becomes executable only after the MCP host can discover the typed
Jenkins tools and a permitted smoke operation succeeds.

## Known limitations

The Remote Access API and plugin endpoints vary by Controller and plugin version.
Unknown plugins remain unsupported until an explicit driver and fixture are added.
This capability does not authorize arbitrary REST calls, arbitrary XML, Script
Console, plugin management, credentials management, node management, or restart.
Generic stdio alone does not provide a trustworthy human-confirmation channel for
production writes; without a valid current-session confirmation those writes
deliberately remain non-executable.

## Alternatives

Use the Jenkins web UI or a user-operated Jenkins CLI command when the target action
is outside the typed MCP policy. Do not turn an unsupported action into a raw API or
Groovy request.

## Rollback

Disable or remove the user-owned Jenkins service through the user's normal operating
procedure. Agent only provides guidance and must not remove MCP host mappings or
external policy files; preserve Jenkins home data, job configurations, credentials,
and build history.

## 能力用途和非目标

用途是连接已存在的 Jenkins Controller，执行受 Jenkins RBAC 与本地策略共同约束的查、增、改。
非目标是安装 Jenkins、绕过 RBAC、自动修改系统服务、任意脚本执行或高风险全局管理。

## 官方获取与文档

官方来源、文档、固定版本和 SHA-256 在 frontmatter 中声明。用户自行选择 Windows 安装包、WAR、
Docker 或受支持的软件源；Agent 不替用户执行下载、安装或服务注册。

## 系统、架构、运行时和硬件支持

Windows、macOS 和 Linux 的支持状态以 Jenkins 官方安装文档和用户本地检测为准。推荐版本要求
Java 21 或更高版本；具体硬件、端口、数据目录和服务帐户由用户的 Jenkins 部署决定。

## 五种宿主兼容矩阵

五种宿主初始均为 `unverified`。每种宿主都必须完成本地 MCP 发现和针对已配置测试 Controller
的最小 smoke，才能提升为已验证。

## 只读检测

按本能力 frontmatter 的检测命令或等价的受控 HTTPS 请求读取版本头和 `/api/json`。不发送写请求，
不导出 Cookie、Token 或完整配置。

## 各系统安装

这是 `user-managed` 的系统能力。缺失时仅输出当前用户语言的官方安装指导，包括 Java 前置、
锁定版本、校验、端口、网络、服务权限、数据目录、回退与只读检查。

## 调用示例和成功判据

成功判据是配置的 Controller 可达、身份通过 Jenkins 自身认证、能读取 `X-Jenkins` 版本头并返回
最小 API 数据。创建或更新操作还必须通过本地策略、回读验证与目标 Jenkins 权限检查。

## 权限、网络、数据和遥测

核心使用需要到配置 Controller 的网络访问。仅使用用户明确提供的地址和凭据配置；不发送遥测，
不上传 Jenkins 配置、构建日志、制品或凭据到第三方服务。

## 卸载或回滚

卸载与回滚由用户管理 Jenkins 服务和数据。Agent 仅提供指导，不移除宿主 MCP 映射或外部策略文件，
也绝不删除 Jenkins Home、Job、制品、凭据或历史构建。

## 已知限制

不支持未知插件的动态接口，不支持高风险删除或全局管理，也不因为账户拥有管理员权限而自动扩大能力。

## 替代能力

当标准 REST 驱动不能覆盖需求时，使用 Jenkins UI 或由用户明确执行的固定 CLI 操作；后续可将经过
官方文档核实和测试的稳定接口沉淀为专用 MCP 驱动。
