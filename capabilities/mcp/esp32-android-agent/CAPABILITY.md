---
spec_version: "1.0"
id: "mcp.esp32-android-agent"
type: "mcp"
locked_version: "git:529c05b901fe6eb8c9bf0312e9d521f210c0e16c"
recommended_version: "0.1.0"
official_source: "https://github.com/shenlan-ai/esp32-android-agent"
official_docs: "https://github.com/shenlan-ai/esp32-android-agent/blob/529c05b901fe6eb8c9bf0312e9d521f210c0e16c/docs/MCP.md"
license: "MIT application; optional firmware tooling and esptool are GPL-2.0-or-later"
last_verified: "2026-11-13"
integrity: {method: "git-commit", value: "529c05b901fe6eb8c9bf0312e9d521f210c0e16c"}
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "documented", arm64: "unverified"}
  runtimes: ["Python >= 3.10", "Android Platform Tools", "local ESP32 Android Agent workbench", "MCP stdio"]
  hardware: ["Android device with authorized ADB", "optional classic ESP32 BLE HID or ESP32-S3 native USB HID"]
hosts:
  codex: "conditional"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "android-agent-mcp --help"}
permissions: ["read the selected Android screen through authorized ADB", "send one declared Android action through the selected controller", "cancel shared input and release the controller"]
network: {required_for_install: true, required_for_core_use: false, default_policy: "loopback-only MCP bridge"}
data_access: ["selected phone screenshot returned as MCP image content to the selected Agent", "current workbench device, input-mode and calibration state"]
installation:
  policy: "user-managed"
  scope: "workspace-shared"
  methods: ["existing", "git"]
workspace_source: "workspace/shared/mcp/esp32-android-agent"
automation_status: "conditional"
---

# ESP32 Android Agent MCP

## 能力用途和非目标

复用上游本地工作台和 stdio MCP，把已选 Android 的 ADB 画面与控制器提供给 Agent。使用 `esp32-android-input` 的 MCP 入口检查任务状态和输入路由；四个工具与输入协议由上游实现。

上游 MCP 工具为 `android_status`、`android_screenshot`、`android_action` 与 `android_stop`。服务端必须与工作台运行在可访问同一 loopback 服务的主机上；它复用工作台里已选设备和校准，不创建第二条串口连接。MCP 客户端只能使用当前 status 实际报告的动作与按键。

## 官方获取与文档

唯一来源为 `https://github.com/shenlan-ai/esp32-android-agent`，锁定 `529c05b901fe6eb8c9bf0312e9d521f210c0e16c`。上游 package 是 `0.1.0`，Python >= 3.10，MCP extra 固定 `mcp==1.30.0`。官方 [MCP guide](https://github.com/shenlan-ai/esp32-android-agent/blob/529c05b901fe6eb8c9bf0312e9d521f210c0e16c/docs/MCP.md) 定义 stdio 启动、工具契约、回环限制和输入确认行为；[README](https://github.com/shenlan-ai/esp32-android-agent/blob/529c05b901fe6eb8c9bf0312e9d521f210c0e16c/README.md) 定义工作台与硬件准备。

## 系统、架构、运行时和硬件支持

Python >= 3.10、Android Platform Tools、运行中的本地工作台和 `server[mcp]` 虚拟环境。MCP 使用 stdio 与 `http://127.0.0.1:8890`（默认工作台地址）通信；本能力核心操作不需外网。

上游当前公开的真实设备证据覆盖经典 ESP32 BLE HID 与 Android 15 指定设备。S3 DevKitC-1 N8、XIAO ESP32S3 和 ESP32-S3-Zero 有软件和协议证据。锁定上游版本的公开手机 HID 验收范围见上游文档。N16R8 不等同于 N8 profile；需验证确切板卡、flash/PSRAM 参数、USB OTG 拓扑及 Android HID 枚举后才可标记 ready。编译通过不代表手机接受 HID。Hub 的 [N16R8 本地真机验收](../../../workflows/esp32-android-input/references/mcp-integration.md#真机验收与恢复) 记录了测试组合和工作台修补条件。

## 五种宿主兼容矩阵

Codex: conditional；OpenClaw: unverified；Claude Code: unverified；Hermes: unverified；OpenCode: unverified。DeepSeek Harness 通过本地部署适配器提供 stdio MCP 映射；DSH 映射必须带入选中的 `esp32-android-input` Skill 部署预览，并以新会话核对工具发现，不从配置文件存在推断为已加载。

## 只读检测

用绝对路径读取虚拟环境中的 package version，并运行 MCP 入口的 `--help`。使用 MCP `list_tools` 与 `android_status` 作只读检查。工作台未运行时，stdio 仍可握手和枚举工具，状态调用会失败。

## 各系统安装

工作台和 MCP bridge 使用用户管理的独立 checkout 与 `.venv`。固定来源 checkout 位于 `workspace/shared/mcp/esp32-android-agent`，MCP extra 由该目录的 Python 执行 `pip install -e "./server[mcp]"` 安装。安装只修改该工作区，不改系统 Python、Hub requirements、固件或 DSH profile。新安装、依赖安装与宿主注册需符合当前任务授权；不以 `npx`、全局 pip 或系统级安装替代。

## 调用示例和成功判据

在 DSH 专用 Agent 部署请求中选择 `esp32-android-input` 后，映射使用真实已存在的绝对 Python 路径和上游目录：

```json
{
  "workflow": "esp32-android-input",
  "server_name": "android_agent",
  "command": "C:/path/to/esp32-agent/.venv/Scripts/python.exe",
  "args": ["C:/hub/workflows/esp32-android-input/scripts/esp32_android_mcp.py", "--url", "http://127.0.0.1:8890", "--mode", "hid"],
  "cwd": "C:/path/to/esp32-agent"
}
```

该映射只能在 MCP extra 已安装、工作台已运行、服务版本包含 MCP 路由、设备已选中且 `android_status` 可读后写入。若工作台配置访问令牌，`ANDROID_AGENT_ACCESS_TOKEN` 必须由 DSH 后端安全继承；不能写入 args、URL、部署请求、Skill、Persona 或日志。DSH 映射 schema 不含环境变量字段，因此未配置安全继承时保持指导态，不把认证降级为明文参数。

工具验收顺序为 `android_status` → `android_screenshot` → 一次 `android_action` → 新截图验证。诊断工作台活动任务时仅调用 `android_status`；截图失败可能取消任务并释放输入。HID 动作要求 `mode` 与选定的 BLE 或 USB 一致，且 `hid_ready: true`。坐标是相对完整原图的 0..1000 整数，必须绑定新鲜且未使用的 `observation_id`。截图过期、模式改变、画面不明或动作结果未知时停止输入。中止自己接管的操作时调用 `android_stop` 并确认释放。

## 权限、网络、数据和遥测

`android_action` 是真实手机输入。动作范围服从当前用户任务和上游宿主审批。HID 失败不得静默切换 ADB；切换必须由用户明确选择。安卓截图会以图像内容进入所选 Agent 的模型上下文，执行前核对屏幕内容和模型服务范围。桥接仅接受 HTTP loopback，不走代理、不跟随重定向、不提供远程 MCP/OAuth。该能力不承诺输入不可检测，也不提供规避审核、验证码或风控的功能。

## 卸载或回滚

关闭由用户启动的工作台和 MCP bridge；宿主 MCP 映射由所属宿主部署适配器按事务恢复。移除 workspace checkout 及 `.venv` 需要用户给出精确路径并明确授权；不卸载 ESP32 固件、不取消 Android ADB 授权、不删除配对信息或手机数据。

## 已知限制

工作台未运行、MCP HTTP 路由版本过旧、token 未配置或设备未连接时不可用。屏幕和控制器由工作台当前选择共享；MCP 没有选择设备、修改校准、刷写固件或切换控制模式的工具。截图过期、已使用、画面变化或并发输入都会拒绝动作。无障碍 selector 不通过此 MCP 提供。

## 替代能力

Android 界面稳定且宿主提供 agent-device MCP 时，手机工作流仍优先使用新鲜可访问性快照与 selector。没有 MCP 时可使用已声明的 agent-device/Airtest CLI 路径。ESP32 HID 失败后不得自动回退到 ADB；用户必须显式选择新的控制路由。
