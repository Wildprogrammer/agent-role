---
name: esp32-android-input
description: Use when an Agent needs the upstream ESP32 Android stdio MCP for Android screenshots and selected HID input, with explicit device, transport, firmware, and action-verification safeguards.
compatibility: Agent Workflow Hub spec 1.0; adapts the user-managed esp32-android-agent upstream without changing Android, firmware, or host configuration automatically.
metadata:
  spec-version: "1.0"
  workflow-version: "0.2.0"
  display-name: "ESP32 Android 输入适配"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '["mcp.esp32-android-agent"]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"mcp":"<ESP32_VENV_PYTHON> <HUB_ROOT>/workflows/esp32-android-input/scripts/esp32_android_mcp.py --url http://127.0.0.1:8890 --mode hid"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# ESP32 Android 输入适配

## 用途与触发条件

本独立工作流拥有 ESP32 Android stdio MCP 入口与输入策略，可单独使用，也可由 [手机设备自动化](../mobile-device-automation/SKILL.md) 调用。入口复用 `shenlan-ai/esp32-android-agent` 的四个工具、工作台与输入协议，并在调用前核对设备、任务和控制模式。手机工作流负责业务步骤和 agent-device/Airtest 固化回放。上游固定提交与接入示例见 [MCP 接入](references/mcp-integration.md)。

该工作流可为既有手机自动化流程提供一个独立的输入执行器：截图仍经授权 ADB，动作经 BLE HID、S3 USB HID 或用户显式选择的 ADB 控制；每一步都必须观察、执行一次、再验证。

## 非目标

- 不声称 HID 输入不可检测，也不提供反检测、设备指纹规避、验证码绕过或风控对抗。
- 不自动解锁手机、开启开发者选项、授权 ADB、配对蓝牙/USB、刷写固件或修改手机安全设置。
- 不替代 `mobile-device-automation` 的 selector、回放、证据和高影响动作确认契约。
- 不读取 Android 私密内容、短信、账户、通知或剪贴板；不把截图传给未获授权的云模型。

## 输入

必须给出目标手机、允许的操作范围、首选输入路由、连接拓扑、板型/芯片/Flash 证据、是否允许 ADB 截图、MCP 客户端和停止条件。多个设备时必须显式选择一个设备；不得按列表第一个猜测。

ESP32-S3 需要额外给出 USB OTG/供电/数据线拓扑和手机端实际 HID 枚举证据。S3 N16R8 与 S3 N8 是不同板型，按实际 Flash、PSRAM 和固件配置验收。

## 输出与命名规则

输出仅写入 `workflows/esp32-android-input/outputs/<run-id>/`：`readiness.md`、`device-profile.json`、`mcp-binding.json`、`action-ledger.jsonl` 和可选 `acceptance.md`。不保存序列号、MAC、无线地址、账号、截图正文、凭据或私有绝对路径。

`mcp-binding.json` 只记录已准备服务的启动程序、参数、工作目录和工具白名单；密钥只以环境变量名出现。任何 DSH `host_options.mcp_servers` 映射必须在专用 Agent 部署预览中显示，不能直接写入 DSH 配置。

## 依赖和运行前检查

完整阅读 [MCP 接入](references/mcp-integration.md) 和 `capabilities/mcp/esp32-android-agent/CAPABILITY.md`。先只读检查 Python、Android Platform Tools、授权 ADB、上游固定提交、MCP extra、工作台服务版本、设备连接、截图路径、输入路由和紧急停止是否可用。工具存在不等于手机已授权或 HID 已被系统接受。

上游 `esp32-android-agent`、手机调试授权、蓝牙/USB 配对和固件均为用户管理能力。缺少其中任一项时只生成指导态报告，不自动烧录、重启工作台、配对设备或修改宿主 MCP 注册。获用户授权准备工作区依赖时，只能在 `workspace/shared/mcp/esp32-android-agent` 按 capability 固定提交创建独立 checkout/venv，不改上游源码、系统 Python 或 DSH 配置。

## 系统修改与权限影响

诊断时先使用只读的 `android_status`。工作台在黑屏、受保护画面或截图连接失败时可能释放输入并取消自己的任务，因此工作台有活动任务时，先由操作者暂停再截图。鼠标/键盘动作、应用启动、ADB 控制、保存上游配置和固件写入都必须在用户任务范围内执行。

任何把截图发送给视觉模型的自动任务，都要说明模型服务、发送范围与保留策略。不得因物理 HID 路径而跳过平台规则、用户确认或业务影响评估。

## 执行步骤

1. 核对目标手机、板型、输入路由和无害验收范围；运行只读诊断。
2. 先以 ADB 控制路径验证截图、设备选择和单次无害点击；再选择 HID 路径，不能在 HID 故障时静默降级。
3. 按上游协议连接 ESP32，验证键盘/鼠标在 Android 端实际出现，并测试停止/释放输入。
4. 启动已准备的工作台，通过本工作流入口完成 MCP 握手与工具发现。先读 `android_status`，确认选中的手机在线、工作台可接管，再截图。
5. 每次动作前核对用户选择的精确 `mode`（`ble` 或 `usb`）、`hid_ready: true` 和工具返回的动作能力。用新截图的 `observation_id` 执行一次输入并重新观测。首次验证坐标、目标较小或发生过误点时，先用工作台移动指针而不按键，确认实际落点再提交点击。使用 `type` 前确认输入框已聚焦，由操作者切换至英文输入法并确认预期文本为可打印 ASCII。
6. 只有运行证据齐全时，才将 MCP 映射带入另一个 Agent 的部署预览；映射写入仍须通过其唯一部署确认。

## 人工确认门

下载、克隆、依赖安装、ESP32 烧录、Android 调试授权、蓝牙/USB 配对、服务配置保存、MCP 宿主注册和任何手机业务动作都需要本次任务对应的明确授权。实际应用内提交、支付、发送消息、发帖、删除或账号变更仍由上游手机自动化工作流的更严格确认规则管理。

## 失败恢复

ADB 未授权、设备脱离、HID 未枚举、输入未验证、MCP 握手失败或屏幕状态不确定时，立即停止输入，保留脱敏证据并报告精确阶段。HID 失败不得自动改用 ADB；用户必须明确选择切换输入路由。不得重放可能已经生效的动作。

重选手机可能将控制模式重置为 ADB。USB 板重新连上监听服务后，工作台控制器也可能仍绑定旧连接。核对已选手机和原配对板，工作台可接管时重新选择用户原定的 USB HID 模式，确认 `hid_ready: true` 后重新截图。定位、重连和已验证范围见 [MCP 接入](references/mcp-integration.md#真机验收与恢复)。

## 重跑、幂等与覆盖策略

每次验收使用新 run ID 和新账本。相同手机再次连接也必须重新确认设备身份、截图和 HID 接受状态。已有 MCP 映射只有在拥有者工作流的预览显示完全一致时才可复用；不覆盖未知配置。

## 验收标准

完成验收必须有：目标设备的显式选择、ADB 截图可用、所选输入路由实际成功、紧急停止有效、MCP 握手与工具发现成功、至少一次无害动作的前后证据，以及明确记录的未验证范围。没有 Android 端 HID 接受证据时，S3 USB HID 只能标记为 `unverified`。

## 清理方式

默认保留最小脱敏账本与验收报告。临时截图、日志和运行缓存只能在用户给出 run ID、准确路径清单并二次确认后清理；不得删除上游配置、固件、配对信息或 Android 数据。
