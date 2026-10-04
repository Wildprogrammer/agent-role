---
spec_version: "1.0"
id: "cli.agent-device"
type: "cli"
locked_version: "0.21.19"
version_requirement: ">=0.21.19"
recommended_version: "0.21.19"
official_source: "https://github.com/callstack/agent-device"
official_docs: "https://oss.callstack.com/agent-device/docs/"
license: "MIT"
last_verified: "2026-10-03"
integrity:
  method: "npm-sha512"
  value: "sha512-XQhWY3znSRkVjVmijV1Ds0kJMQR0Iv7VdY3pzNqTv6zMdMqha4eDZEwsCOWRbT5Nz1TNFm3ObPju3TzlVMlP5Q=="
  locked_version: "0.21.19"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "documented", arm64: "documented"}
  runtimes: ["Node.js >=22.12", "Android platform tools for Android targets"]
hosts:
  codex: "conditional"
  openclaw: "conditional"
  claude-code: "conditional"
  hermes: "conditional"
  opencode: "conditional"
detect: {mode: "read-only", command: "<workspace-shared agent-device> --version"}
permissions: ["enumerate and control the explicitly selected mobile device", "start the local agent-device daemon and session", "write workflow-private screenshots, logs, scripts, and evidence"]
network: {required_for_install: true, required_for_core_use: false}
data_access: ["accessibility snapshots and screenshots from the selected device", "user-approved text and gestures", "device logs requested by the workflow"]
installation: {policy: "agent-managed", scope: "workspace-shared", methods: ["existing", "npm"]}
automation_status: "conditional"
---

# agent-device

## 能力用途和非目标

为 `mobile-device-automation` 提供 Android 真机和模拟器的设备枚举、无障碍树查询、语义操作、截图、录制及确定性 `.ad` 脚本回放。它是受控驱动器，不是视觉模型或自主 Agent；不替代 Airtest 的图片锚点回放，也不提供 STF 式设备农场管理。

## 官方获取与文档

只使用 Callstack 官方 npm 包 `agent-device@0.21.19`。源码和使用说明分别来自 frontmatter 中的官方仓库与文档站；安装包必须匹配 npm SHA-512 `sha512-XQhWY3znSRkVjVmijV1Ds0kJMQR0Iv7VdY3pzNqTv6zMdMqha4eDZEwsCOWRbT5Nz1TNFm3ObPju3TzlVMlP5Q==`。

## 系统、架构、运行时和硬件支持

Windows、macOS 和 Linux 均按上游文档登记，运行时要求 Node.js 22.12 或更高版本；Android 目标还要求可用的 Android platform tools、已授权的 USB 调试或无线 ADB 连接。该声明不等于每台主机已经实机验收；第一版仓库验收以 Windows 为基准。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes 和 OpenCode 均可通过工作流 CLI 条件式调用固定运行时。宿主必须能够执行 argv、访问工作流 staging，并让用户明确选择设备；不得把能力目录登记解释为 MCP 已自动注册或设备已授权。

## 只读检测

检测仅对 `<HUB_ROOT>/workspace/shared/runtimes/agent-device/0.21.19/` 中固定入口执行 `--version`，并核对包锁和版本。检测不安装依赖、不启动设备会话、不接受 ADB 授权，也不触碰设备数据。

## 各系统安装

安装目的地固定为 `<HUB_ROOT>/workspace/shared/runtimes/agent-device/0.21.19/`。确认 Node.js 版本、目标路径、网络和回滚范围后，以参数数组执行：

```text
npm install --prefix <DESTINATION> --omit=dev --save-exact agent-device@0.21.19
```

安装后必须验证 npm SHA-512、锁定版本和固定入口。禁止 `npx agent-device@latest`、全局 npm 安装、隐式安装 Node.js、Android SDK 或设备驱动、权限提升以及自动写入任何宿主 MCP 配置。

## 调用示例和成功判据

工作流始终通过参数数组调用固定入口，并显式传入设备 serial 或已绑定 session。成功判据是：唯一选择目标设备；命令退出码为零；返回结构可解析；录制或回放产物写入工作流私有目录；最终状态断言与证据包一致。不得用 shell 拼接未经验证的用户输入。

## 权限、网络、数据和遥测

核心设备控制不要求公网，但 ADB、设备厂商服务或应用自身可能使用网络。无障碍快照、截图、日志、输入文本和设备标识只在当前请求授权范围内读取或保存；敏感参数按调用传入并从日志和产物中脱敏。能力不会绕过 Android 安全界面、系统授权或应用权限。

## 卸载或回滚

回滚仅删除由本能力创建、路径和 `0.21.19` 身份均匹配的 `<HUB_ROOT>/workspace/shared/runtimes/agent-device/0.21.19/`。不得删除 Node.js、Android SDK、ADB 配置、用户脚本、设备数据、其他版本或其他工作流运行时。

## 已知限制

设备离线、多设备未唯一选择、无障碍树缺失、系统安全界面、应用自绘画布和动画状态都可能阻止语义定位。第一版不实现 iOS、设备农场调度或跨主机租约；macOS/Linux 只按上游文档支持，未在本仓库完成真实设备验收。

## 替代能力

语义定位不可用但目标可视觉确认时，可由同一工作流使用 `python.airtest` 对 Android 设备 framebuffer 做图片定位。图片失败不得静默回退保存坐标；若设备、运行时或权限条件不满足，应返回稳定的就绪错误并给出人工恢复步骤。
