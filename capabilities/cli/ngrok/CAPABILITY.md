---
spec_version: "1.0"
id: "cli.ngrok"
type: "cli"
locked_version: "3.39.9"
version_requirement: ">=3.39.9"
recommended_version: "3.39.9"
official_source: "https://ngrok.com/download"
official_docs: "https://ngrok.com/docs/agent/cli/"
license: "Proprietary"
last_verified: "2026-09-20"
integrity:
  method: "sha256"
  value: "28317f39d20029119e11562673b134ada4ef6feb508b4a22453731751a675362"
  locked_version: "3.39.9"
  source: "local-installed-binary"
systems:
  os: {windows: "verified-local", macos: "documented", linux: "documented"}
  arch: {x86_64: "verified-local", arm64: "documented"}
  runtimes: ["standalone ngrok agent executable", "an ngrok account and agent authtoken"]
  hardware: ["no special hardware"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "ngrok --version"
permissions: ["open an outbound ngrok connection", "publish a user-approved local HTTP port through an ngrok endpoint"]
network:
  required_for_install: true
  required_for_core_use: true
data_access: ["HTTP requests and responses crossing the explicitly approved tunnel", "ngrok account configuration managed outside the workflow"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "conditional"
---
# ngrok

## 能力用途和非目标

通过用户已安装、已认证的 ngrok Agent，把明确批准的本地 HTTP 端口临时发布为公网端点。工作流只运行 `ngrok http <port>` 并透传用户批准的非密钥附加参数；明确拒绝 argv 中的 `--authtoken`，不管理账户、域名、订阅、Traffic Policy 或后台系统服务。

## 官方获取与文档

安装入口为 `https://ngrok.com/download`，Agent CLI 文档为 `https://ngrok.com/docs/agent/cli/`。当前 Windows x64 实机验证版本为 `3.39.9-msix-stable`；其它平台按官方说明安装后仍需本机检测。

## 系统、架构、运行时和硬件支持

Windows x64 已通过本机版本检测；macOS、Linux 和 arm64 仅按上游文档登记。ngrok 是独立可执行文件，但核心使用需要 ngrok 账户、预先配置的 Agent authtoken 和可访问 ngrok 服务的网络。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes 和 OpenCode 均登记为 `unverified`。宿主必须能够维持一个可观察、可停止的前台进程；目录登记不代表公网链路已经实测。

## 只读检测

仅解析 `ngrok --version`，不读取或输出 authtoken，不启动端点，也不调用账户 API。认证状态由真正启动端点时的 ngrok 原生结果判定。

## 各系统安装

安装是 `user-managed`。工作流可提供官方安装指引，但不自动下载、安装、升级或执行 `ngrok config add-authtoken`。用户必须在工作流外自行完成账户登录和密钥配置，避免密钥经过对话或运行报告。

## 调用示例和成功判据

调用形态为 `ngrok http <LOCAL_PORT> <USER_APPROVED_ARGS...>`。成功要求 ngrok 原生输出一个活动公网端点，并从获准客户端验证该端点确实转发到目标本地服务。端点存活期以 ngrok 前台进程为准。

## 权限、网络、数据和遥测

启动会建立到 ngrok 的出站连接，并使公网请求经过 ngrok 基础设施到达本地服务。端点是否匿名、是否固定域名以及认证、OAuth、WAF 或 Traffic Policy 均由用户的 ngrok 参数和账户配置决定；不得默认把含敏感文件的服务匿名暴露到公网。

## 卸载或回滚

停止当前前台 ngrok 进程即可撤销临时端点。工作流不删除 ngrok、账户配置、authtoken、域名或 Traffic Policy；这些用户管理内容只能在独立明确请求下处理。

## 已知限制

公网 URL、配额、功能可用性和端点策略受 ngrok 账户及服务端状态影响。此能力不保证固定 URL，不解析或保存 authtoken，也不把 LAN 分享自动视为已获准公网发布。

## 替代能力

ngrok 缺失、未认证或服务不可达时，保留本地/LAN 分享，不自动改用其它隧道服务。
