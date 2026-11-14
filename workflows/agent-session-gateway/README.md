# Agent Session Gateway

Agent Session Gateway connects bounded external channels such as Lingxi/Xiaozhi
and XiaoAi to an explicitly selected canonical Agent session. The Gateway owns
binding, confirmation, deduplication, attachments, and optional device-control
projection. It does not copy chat history or merge history between Agent Hosts.

## System boundary

```text
Lingxi/Xiaozhi ─┐
XiaoAi MCP      ├─ Agent Session Gateway ─ Hermes session
Android 手机    ┘                         └ DeepSeek Harness session
```

This repository provides the Gateway and its pinned adapter bundle. A complete
physical voice system additionally needs:

- the [Lingxi ESP32 firmware](https://github.com/Wildprogrammer/lingxi-esp32);
- `xiaozhi-esp32-server` v0.9.7 at commit
  `87c6df77b0220ddbd8fc804f984f83f69f3225e5`;
- at least one supported Agent Host: Hermes Agent 0.21+ or DeepSeek Harness Web
  0.2.0-rc.2, 0.2.1-rc.5, or 0.2.2-rc.1;
- the server-side ASR, VAD, and TTS dependencies selected by the operator;
- supported ESP32-S3 hardware and audio modules.

The Gateway is not a Xiaozhi server, an Agent Host, a model distributor, or a
public tunnel manager.

## Runtime matrix

| Layer | Runtime |
| --- | --- |
| Agent Workflow Hub and Gateway | Python 3.11+ |
| Pinned Xiaozhi server | Python 3.10 in an isolated environment |
| Managed DeepSeek Harness | Node.js plus DSH 0.2.0-rc.2, 0.2.1-rc.5, or 0.2.2-rc.1 |
| Lingxi firmware build | ESP-IDF release/v6.1; see the firmware repository |

Do not install the Hub package into the Python 3.10 Xiaozhi environment. The
managed server adapter imports the reviewed Gateway source through the bounded
startup contract, while host communication runs through private loopback or
stdio interfaces.

## Start here

1. Read `SKILL.md` completely and validate this repository with an absolute
   path.
2. Copy `references/bridge-config.json.example` outside the repository and
   replace every synthetic value with your own allowlisted Agent configuration.
3. Run the `doctor` entrypoint and review the reported SHA-256.
4. Clone the pinned Xiaozhi server commit and run the adapter installer in
   dry-run mode before applying it.
5. Pass every executable and configuration path explicitly to
   `scripts/start_xiaozhi_server.ps1`.
6. Complete the software WebSocket simulation before binding physical hardware.
7. Install a matching signed firmware release from the Lingxi repository and
   bind the new device to an existing Agent session.

Configuration, bindings, credentials, models, logs, attachments, and runtime
backups stay outside both source repositories. Never commit a real API key,
Bearer token, launch token, MAC address, device UUID, LAN address, session ID,
or Agent inventory.

## Android 手机独立终端

手机可直接提供文字、麦克风、相机、扬声器和字幕，无需 ESP32 在线。
它通过专用 HTTPS / WebSocket 入口绑定已有 Hermes 或 DeepSeek Harness 会话。
支持仅文字模式、照片压缩与上传进度、取消上传，以及拍照返回后的有限自动重连；
恢复连接不自动重发消息。电脑端仍使用同一 canonical 会话。

参见 [手机端部署说明](references/mobile-client.md) 和
[手机配置模板](references/mobile-config.json.example)。局域网使用专用 CA，公网使用受信 HTTPS
反向代理；证书、私钥、密码和真实入口地址均由部署者保存在仓库外。
慢公网链路仍可能断开，压缩图可降低传输量，不能保证大原图稳定上传。

Hermes 0.22+ 使用原生 gateway 与一次性连接 ticket，并显式查询 stored 会话；
旧版 legacy serve 路径继续保留，不把两种凭据机制混用。

For DSH upgrades and supported session operations, see
[DeepSeek Harness Web compatibility](references/deepseek-harness-compatibility.md).

## Compatibility names

The public workflow ID is `agent-session-gateway`. Some internal Python modules,
environment variables, binding filenames, and deployment adapters retain the
historical `xiaozhi_hermes` / `XIAOZHI_HERMES_*` spelling so existing private
deployments can upgrade without losing bindings. Those names are compatibility
identifiers, not the product boundary.

## Release matching

Gateway and firmware versions are independent. A release compatibility record
must identify the exact Gateway commit, adapter bundle hashes, pinned Xiaozhi
server commit, matching `lingxi-esp32` release and source commit, board ID, and
manifest digest. Mutable branch names are not compatibility evidence.

## License and upstream attribution

Agent Workflow Hub and this Gateway are released under the repository MIT
License. The adapter bundle integrates with the MIT-licensed
`xinnan-tech/xiaozhi-esp32-server`; see the repository-level
`THIRD_PARTY_NOTICES.md`. Firmware attribution and binary notices are owned by
the separate Lingxi firmware repository.
