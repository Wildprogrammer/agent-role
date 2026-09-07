---
spec_version: "1.0"
id: "mcp.cua-driver"
type: "mcp"
locked_version: "0.23.2"
version_requirement: ">=0.23.2"
recommended_version: "0.23.2"
official_source: "https://github.com/trycua/cua"
official_docs: "https://github.com/trycua/cua/blob/main/libs/cua-driver/README.md"
license: "MIT"
last_verified: "2026-09-07"
integrity:
  method: "sha256"
  value: "413e20eda0a224ae57d87ddf181520957e49ee9904f3deb9a92bc61b6367e01e"
  locked_version: "0.23.2"
  source: "https://github.com/trycua/cua/releases/download/cua-driver-rs-v0.23.2/checksums.txt"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented-upstream-not-workflow-v1"}
  arch: {x64: "documented", arm64: "documented"}
  runtimes: ["Windows 10/11 interactive desktop", "macOS 14+", "Cua Driver MCP over stdio"]
hosts:
  codex: "conditional"
  openclaw: "conditional"
  claude-code: "conditional"
  hermes: "conditional"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "cua-driver --version"
permissions: ["desktop accessibility inspection", "window-scoped screen capture", "mouse and keyboard input to an explicitly selected desktop target", "workflow-private action evidence"]
network: {required_for_install: true, required_for_core_use: false, telemetry_default_upstream: true, workflow_policy: "disable telemetry"}
data_access: ["titles and accessibility trees of selected desktop windows", "screenshots of selected desktop windows", "user-authorized text and input events"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "official-artifact", "manual"]
automation_status: "conditional"
---
# Cua Driver

## 能力用途和非目标

本能力让 Agent 通过 Cua Driver MCP 在 Windows 和 macOS 上检查并操作用户明确指定的桌面应用。它是动态桌面控制后端，不是视觉模型，不替代 Airtest 的模板匹配和确定性 `.air` 回放，也不授权控制未点名应用、身份验证界面或已有浏览器用户配置。

## 官方获取与文档

唯一官方来源是 `https://github.com/trycua/cua`，锁定稳定 SemVer 发布 `cua-driver-rs-v0.23.2`。GitHub 因 monorepo 发布策略显示 Pre-release，但该发布说明明确普通 SemVer 是稳定通道。`checksums.txt` 的 SHA-256 是 `413e20eda0a224ae57d87ddf181520957e49ee9904f3deb9a92bc61b6367e01e`。官方 Windows 安装器使用的完整 x86_64 资产 `cua-driver-rs-0.23.2-windows-x86_64.zip` 哈希为 `acb0e44ba75ccc2669186665182a01b9517a71a82e81625b4f9f555b455e7a05`；只含二进制的替代资产哈希为 `27a41831d5dda71082b58154ff87966a9ad8131ce66e8060da2d860558655c13`。必须按实际安装方法核对对应资产，不能混用，也不能使用 nightly。

## 系统、架构、运行时和硬件支持

Windows 需要 Windows 10/11 或 Windows Server 的交互式桌面会话，服务不能停留在 Session 0。macOS 需要 14 或更高版本，并使用签名 `CuaDriver.app` 保持 Accessibility 和 Screen Recording 的 TCC 身份。Linux 是上游已记录能力，但 `desktop-client-automation` 第一阶段不宣称 Linux 工作流支持。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code 和 Hermes 只有在当前任务实际列出 Cua MCP 工具时才是 conditional；OpenCode 保持 unverified。宿主存在、CLI 已安装或配置文件中出现名称都不能替代当前 MCP 工具发现。Agent 不自动写任何宿主 MCP 配置。

## 只读检测

先用绝对解析到的可执行文件运行 `--version`，要求精确为 0.23.2；再用 argv 运行 `call health_report --json {} --compact`。检测设置超时，不经过 shell，不截图、不列窗口、不发送输入。`overall=failed` 为不可用；`degraded` 必须结合任务所需的 UIA/AX 或截图检查判断，不能直接宣称完全就绪。

## 各系统安装

安装属于 user-managed system 级变更。Windows 官方发布入口是 `https://cua.ai/driver/install.ps1`，会写用户目录、用户 PATH 并尝试注册登录自启动任务。macOS 官方发布入口是 `https://cua.ai/driver/install.sh`，会安装签名应用并要求用户在系统设置授予两项 TCC 权限。执行前必须展示锁定版本、平台资产 SHA-256、路径、网络、服务或自启动影响、权限和卸载方式并单独确认；不得直接执行搜索结果中的管道安装命令。

## 调用示例和成功判据

Agent 通过 `cua-driver mcp` 调用 `list_windows`、`get_window_state` 和动作工具。成功要求精确锁定 `pid + window_id`，元素动作使用同一新鲜快照中的 `snapshot_id + element_token`，像素动作只取自该窗口同次截图，动作后重新读取状态。CLI 与 MCP 使用同一运行时契约，但 Agent 动作优先保留在 MCP 中，Hub 不复制动作协议。

## 权限、网络、数据和遥测

核心操作默认离线。安装后执行 `cua-driver telemetry disable`，再读回状态；不启用 `--dangerously-bypass-approvals`。窗口树、截图、标题和输入只限用户点名目标及工作流证据目录。现有浏览器配置附加、登录、验证码、凭据和系统权限界面不自动处理。

## 卸载或回滚

按同一锁定发布提供的 `uninstall.ps1` 或 `uninstall.sh` 卸载，再由用户移除宿主 MCP 映射和 macOS TCC 项。保留用户文件、Airtest bundle、工作流证据和目标应用数据。

## 已知限制

Windows 最小化窗口可能没有可捕获像素；部分 UIA 提供者会挂起或返回不完整树；后台投递并非对所有 Electron、Tauri 或 WebView 动作都有效。macOS 权限绑定应用身份，系统升级也可能改变后台输入行为。结构化拒绝后才允许针对当前动作尝试前台投递。

## 替代能力

Cua 不可用但当前 Codex 原生 Computer Use surface 已真实暴露时可使用兼容路径。已确认的 Windows 图片流程继续由 Airtest 回放；两者都不可用时返回人工步骤，不猜测坐标。
