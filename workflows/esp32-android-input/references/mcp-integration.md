# ESP32 Android Agent 的 MCP 接入

上游仓库：`https://github.com/shenlan-ai/esp32-android-agent.git`，锁定提交 `529c05b901fe6eb8c9bf0312e9d521f210c0e16c`，上游包版本 `0.1.0`。官方 [`docs/MCP.md`](https://github.com/shenlan-ai/esp32-android-agent/blob/529c05b901fe6eb8c9bf0312e9d521f210c0e16c/docs/MCP.md) 和 README 说明：工作台在本机运行，ADB 用于截图；输入可为经典 ESP32 BLE HID、S3 USB HID 或显式 ADB 控制；外部 Agent 通过上游 stdio MCP 使用工作台已选设备。MCP extra 锁定 `mcp==1.30.0`，不是另写一个 Hub MCP server。

上游文档要求 Python 3.10+ 与 Android Platform Tools。工作台以 `python -m android_agent --no-browser` 启动，由操作者选择设备和路由。Codex 等宿主用同一虚拟环境的绝对 Python 路径执行本工作流的 `scripts/esp32_android_mcp.py`，仍注册为 `android_agent`。固定上游 checkout 位于 `workspace/shared/mcp/esp32-android-agent`，`./server[mcp]` 安装在其 `.venv`。

Codex 可使用以下 stdio 注册示例。将占位路径换成实际的工作台虚拟环境 Python 和 Hub 脚本路径，并使用工作台实际的回环地址。在新会话先用 `android_status` 验证连接和路由。

```powershell
codex mcp add android_agent -- "C:/path/to/esp32-agent/.venv/Scripts/python.exe" "C:/hub/workflows/esp32-android-input/scripts/esp32_android_mcp.py" --url http://127.0.0.1:8890 --mode hid
```

DeepSeek Harness 的 `host_options.mcp_servers` 映射须经专用 Agent 预览；示例字段如下，不能直接粘贴占位路径：

```json
{
  "workflow": "esp32-android-input",
  "server_name": "android_agent",
  "command": "C:/path/to/esp32-agent/.venv/Scripts/python.exe",
  "args": ["C:/hub/workflows/esp32-android-input/scripts/esp32_android_mcp.py", "--url", "http://127.0.0.1:8890", "--mode", "hid"],
  "cwd": "C:/path/to/esp32-agent"
}
```

若工作台启用了访问令牌，bridge 从 DSH 后端环境继承 `ANDROID_AGENT_ACCESS_TOKEN`；不把令牌放入 URL、args 或 Hub 部署请求。若目标宿主无法安全继承该变量，保持未配置状态。

`--mode hid` 接受 BLE 和 USB HID，拦截 ADB 动作。已知接线时用 `--mode ble` 或 `--mode usb` 锁定一种；无硬件诊断需要实际 ADB 输入时用独立的 `--mode adb` 会话。上游仍按观测 ID 检查设备、控制器和画面是否变化。

与 `mobile-device-automation` 配合时，手机工作流加载这个独立工作流，通过宿主调用 MCP；Hub 的 `.ad`/Airtest replay bundle 使用原有执行器。推荐的验收顺序：

1. 授权 ADB，先查 `android_status`。工作台可接管时再截图确认选中的手机。
2. 先在 ADB 控制模式做一次用户明确授权的无害动作，验证工作台画面与目标手机一致。
3. 按准确板型选择输入路由并配对。经典 ESP32 BLE HID 有上游实机证据；S3 USB HID 需要目标手机的 OTG/USB HID 实测，不能由“能编译”推导。
4. 选择 HID 并完成需要的校准，通过本工作流入口完成握手、工具发现和截图。S3 USB HID 使用手机 OTG 与无线 ADB，按上游说明配置板子的 Wi-Fi 控制通道。
5. 专用 Agent 部署时将映射归到 `esp32-android-input`。需要业务侧编排时同时选入 `mobile-device-automation`。

四个工具为 `android_status`、`android_screenshot`、`android_action` 与 `android_stop`。任务活动中只读状态，暂停后再接管。截图失败可能停止工作台任务并释放输入。每次动作前检查 `selected`、`connected`、`mode`、`hid_ready` 和工具公布的能力。重选手机后重新验证，禁止使用旧 `observation_id`。动作必须串行执行并用新截图验证；超时结果不确定时不得重放。

## 真机验收与恢复

- **连接恢复。** USB 监听显示板子在线，但 MCP 的 `hid_ready` 仍为 `false` 时，检查 MCP 的 `--url` 是否指向负责这块板子的工作台实例。确认已选手机、原配对板和任务状态后，在工作台重新选择 USB HID，将控制器绑定到新连接。恢复后重新执行状态检查与截图。
- **指针定位。** 无线 ADB 负责截图，工作台经 Wi‑Fi 控制 ESP32，ESP32 通过 USB HID 向手机发送鼠标与键盘事件。HID 将完整画面的 `0..1000` 坐标线性换算为鼠标相对移动量。校准状态只表示校准可用，实际落点仍可能偏移。首次验证坐标、小目标或误点后，可用工作台的无点击鼠标探测（`POST /api/control/locate`，使用工作台会话）移动指针并在截图上核对落点。探测参数是 HID 移动计数，MCP `tap` 仍使用 `0..1000`。确认落点后用新 `observation_id` 点击，再按实际页面变化验收。
- **文字输入。** `type` 只支持可打印 ASCII。先确认输入框已聚焦，由操作者切到英文输入法，输入后核对框内的实际文本，再用已公布的 `enter` 键或页面搜索按钮提交。页面动态变化造成 `stale_observation` 时，按新画面重新决策。

2026-11-15 在 Windows 的 Codex 中，用 ESP32-S3 N16R8（测试固件 `0.1.2`）和 Android 15 验证了 USB HID 动态操作。测试从系统桌面打开一个购物应用，定位搜索框，经 USB HID 输入 ASCII 关键词并按回车，随后用无线 ADB 截图验证结果页。操作者手动切换了英文输入法，连接恢复和鼠标探测使用本地工作台。这份记录只覆盖上述硬件、固件和宿主组合。

该测试使用了基于锁定版本的工作台，带有原生 USB CDC 配网的 DTR 修补。如果相同板型已枚举原生 USB 串口，但在锁定版本的配网界面提交后无响应，可先核对原生 USB 口与 USB 转串口的区别。已验证的工作台修补是在上游 `server/android_agent/provision.py` 的 `provision_board` 中，在打开原生 CDC 端口前设定 `device.dtr = True` 和 `device.rts = False`。该修补应用于工作台源码，适配脚本的上游版本锁定不变。
