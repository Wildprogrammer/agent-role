---
name: agent-session-gateway
description: Use when an external channel such as Lingxi/Xiaozhi or XiaoAi needs to bind to and continue an existing canonical conversation on a supported Agent host, while preserving host isolation, confirmation, authentication, deduplication, attachments, and optional device capabilities.
compatibility: Agent Workflow Hub spec 1.0; pinned xiaozhi-esp32-server v0.9.7 on Windows with Python 3.10; Python 3.11+, Hermes Agent 0.21 legacy serve or 0.22+ native gateway, and DeepSeek Harness Web 0.2.0-rc.2, 0.2.1-rc.5 or 0.2.2-rc.1; hardware is optional for software validation.
metadata:
  spec-version: "1.0"
  workflow-version: "0.9.3"
  display-name: "Agent Session Gateway"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '[]'
  capability-slots: '{}'
  config-templates: '{"bridge":"references/bridge-config.json.example"}'
  config-requirements: '{"bridge":{"scope":"repository-external","required":true}}'
  entrypoints: '{"doctor":"python <HUB_ROOT>/workflows/agent-session-gateway/scripts/xiaozhi_hermes_bridge.py doctor --config <ABSOLUTE_CONFIG_JSON>","simulate":"python <HUB_ROOT>/workflows/agent-session-gateway/scripts/xiaozhi_hermes_bridge.py simulate --config <ABSOLUTE_CONFIG_JSON> --request <ABSOLUTE_REQUEST_JSON>","serve-stdio":"python <HUB_ROOT>/workflows/agent-session-gateway/scripts/xiaozhi_hermes_bridge.py serve-stdio --config <ABSOLUTE_CONFIG_JSON> --confirmed-config-sha256 <SHA256>","serve-xiaoai-mcp":"python <HUB_ROOT>/workflows/agent-session-gateway/scripts/serve_xiaoai_mcp.py","install-server-adapter":"python <HUB_ROOT>/workflows/agent-session-gateway/scripts/install_xiaozhi_server_adapter.py --server-root <ABSOLUTE_XIAOZHI_SERVER_ROOT> --apply","start-server":"powershell -File <HUB_ROOT>/workflows/agent-session-gateway/scripts/start_xiaozhi_server.ps1 -ConfirmedBridgeConfigSha256 <SHA256> -ServerRoot <ABSOLUTE_XIAOZHI_SERVER_ROOT> -ServerPython <ABSOLUTE_SERVER_PYTHON> -BridgePython <ABSOLUTE_GATEWAY_PYTHON> -BridgeConfig <ABSOLUTE_CONFIG_JSON>","verify-server-plugin":"python <HUB_ROOT>/workflows/agent-session-gateway/scripts/verify_xiaozhi_server_plugin.py","simulate-websocket":"python <HUB_ROOT>/workflows/agent-session-gateway/scripts/simulate_xiaozhi_text.py <TEXT_TURNS...>"}'
  desktop-entrypoints: '{"doctor":"desktop-doctor --config <CONFIG>","agents":"desktop-list-agents --config <CONFIG>","list":"desktop-list-sessions --config <CONFIG> --host <HOST> --profile <AGENT>","bind":"desktop-bind --config <CONFIG> --device-id <DEVICE> --host <HOST> --profile <AGENT> --stored-session-id <SESSION>","show":"desktop-show-binding --config <CONFIG> --device-id <DEVICE>","unbind":"desktop-unbind --config <CONFIG> --device-id <DEVICE>","send":"desktop-send --config <CONFIG> --device-id <DEVICE> --text <TEXT> --confirmed"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# Agent Session Gateway

## 产品定位与兼容标识

本工作流的正式产品定位是“多通道 Agent 会话网关”：把灵溪/Xiaozhi、小爱等外部通道显式绑定到 Hermes、DeepSeek Harness 等受支持 Agent Host 的既有 canonical session，使多个入口在绑定同一宿主会话时共享连续历史。网关不复制宿主历史，不把不同宿主的历史合并，也不在宿主故障时自动改路由。

公开工作流 ID 和目录名为 `agent-session-gateway`。现有私有部署仍可继续使用历史 Python 包、CLI、`XIAOZHI_HERMES_*` 环境变量和仓库外绑定文件；公开版不要求迁移或删除这些兼容标识。产品名称使用 `Agent Session Gateway`，避免把 Xiaozhi 或 Hermes 误当成不可替换的核心层。

职责按以下边界组织：

- **网关核心**拥有外部身份到 `{host, agent, session}` 的绑定、确认、去重、附件、安全策略和会话连续性规则。
- **通道适配器**负责 Lingxi/Xiaozhi WebSocket 与设备 MCP、小爱 Streamable HTTP MCP，以及未来其他通道的认证、连接和消息格式；通道不拥有独立 canonical history。
- **宿主适配器**负责 Hermes Desktop、DeepSeek Harness Web 及未来 Agent Host 的原生会话协议；宿主之间不广播、不复制历史、不自动回退。
- **设备扩展**负责灵溪的音量、停止播报、OLED、音频诊断和连接状态；这些能力不得改变网关的会话绑定与历史语义。

可选 **Android 手机独立终端** 由手机自身提供麦克风、相机、扬声器和表情/字幕，不依赖物理 ESP32 上线。接入方式、TLS配置、运行边界和验收见 `references/mobile-client.md`；启动器以 `-LingxiMobileConfig <仓库外绝对JSON路径>` 显式启用。同一 Xiaozhi 进程复用已加载语音识别、EdgeTTS配置和语音侧 adapter；手机拥有独立 `mobile-<随机ID>` 绑定，不接管原设备。原 admin/companion 两个回环权限面保持不变，手机新增专用权限面。

固定上游 `xiaozhi-esp32-server` 是外部通道运行时，负责设备 WebSocket、ASR/VAD/TTS、音频传输和 Xiaozhi 协议。工作流只拥有安装到该运行时的适配器源文件和安装契约；上游 checkout 中的已安装文件只是部署副本，不是权威源码。

ESP32 固件源码、板型定义和生成的 `xiaozhi.bin`、`generated_assets.bin`、`merged-binary.bin` 属于独立的灵溪固件发布域，不是工作流资源，也不得提交到本工作流 Git 目录。工作流只保存构建/发布契约、制品元数据、大小、摘要、兼容性和外部位置；`merged-binary.bin` 仅用于物理工厂烧录或救援，不能作为远程 OTA 制品。

## 用途与触发条件

为外部交互通道提供一个最小、可审计的 PC 侧 Agent 会话控制边界。一台设备或逻辑入口只显式绑定一个 `{host, agent, session}`：`hermes` 复用 Hermes Desktop 原生会话，`deepseek-harness` 复用同一 DSH Web 进程的原生会话。灵溪、小爱和电脑端只有在绑定同一宿主 session 时才共享该宿主的 canonical history；两个宿主的历史不合并、不广播、不互相回退。可选“灵溪控制台”和电脑伴随屏必须嵌入同一 Xiaozhi 服务进程，通过唯一 `DeviceControlCoordinator` 复用语音侧适配器、待确认状态和实时设备连接，不能启动第二个控制台 sidecar。可选“小爱 MCP”使用服务端固定的逻辑设备 ID，经独立 Bearer 认证从回环 Streamable HTTP `/mcp` 暴露六个收敛工具；它仍写入相同 canonical 会话，不创建历史副本。第六个工具只签发短时、一次性附件上传页，避免把文件或 Base64 塞进 MCP JSON。旧的逐行 JSON + Hermes Runs API 调度保留为兼容路径。

## 非目标

- 不实现 Xiaozhi 官方云、MQTT、通用 MCP 客户端、域名或公网隧道管理；手机入口可使用用户提供的TLS证书，并提供专用LAN测试证书生成脚本（不安装系统信任）；小爱 MCP 只提供回环服务，公网 HTTPS 由用户已有的独立隧道工具承载，现有 Xiaozhi/控制端口不得随之暴露。
- 不安装、启动、停止或修改 Hermes，不创建 Profile，不启用 `api_server`，不生成 API 密钥。
- 不执行 shell、键鼠或 USB HID 操作，不自动批准 Hermes 的工具调用。
- 不替代 ESP32 固件、音频驱动、唤醒词、AEC、功放、扬声器或会议软件适配。
- 不把 ESP32 固件源码或 `.bin` 制品作为工作流资源纳入 Git；固件构建、工厂烧录和发布制品由独立固件域负责。
- 不自动跟随电脑前台窗口，不静默新建或改绑会话；目标失效时明确失败。
- 不把 Hermes 历史复制到 DeepSeek Harness，反之亦然；不在宿主故障时自动改路由。
- 不使用 DSH headless/stdio 进程代替 Web 会话控制器，因为独立进程不能保证与浏览器共享同一 canonical session。
- 不把电脑伴随屏当成外接触摸屏固件；当前只实现 PC 浏览器状态表情、停止播报、绝对音量和既有待确认动作。ESP32 外接屏、触控和表情素材烧录继续后置。
- 小爱 MCP 首版不提供创建、压缩、删除、重置、分叉、归档、历史导出、任意 shell/文件/进程/网络调用、服务启停或配置修改；这些能力不能通过普通 `send_message` 伪装成管理工具。
- 小爱 MCP 不声称小爱客户端会把相册图片或文档字节原生注入 MCP 参数；附件入口是明确返回给用户打开的短时上传页。真实手机不能展示或打开该 URL 时，文字工具仍可用，但附件能力不得宣称验收通过。

## 输入

桥接配置必须符合 `references/bridge-config.schema.json`。每个允许的 Profile 都显式声明名称、精确的数值回环 HTTP `/p/<profile>` 地址，以及只保存变量名的 `api_key_env`；密钥值只能由桥接进程环境提供。`trusted_caller_id` 标识唯一受信的 stdio 父进程。`dispatch_enabled` 默认应保持 `false`，直到本机 Hermes Profile 权限和实时提交范围已审查。`require_confirmation` 只控制旧的非绑定 Runs API `dispatch`。独立的 `bound_voice_dispatch_mode` 默认 `confirm`（关闭直写），只有用户确认精确配置摘要后才可设为 `direct`；生命周期操作始终要求确认。

Hermes Profile 白名单完全由仓库外桥接配置的 `profiles` 数组提供。公开示例只使用 `default`、`specialist` 等合成名称，不附带任何维护者的真实 Profile；修改白名单后必须重新计算配置摘要并确认，插件在启动时从同一已确认配置生成工具枚举。

小爱 MCP 只在 `XIAOAI_MCP_TOKEN`、`XIAOAI_MCP_DEVICE_ID` 和现有私有桥接环境完整时启动。监听地址固定 `127.0.0.1`，默认端口 `8770`；token 必须是 32–512 个无空白字符，只用于公网通道认证，HTTP 只接受唯一的 `Authorization: Bearer <token>`，拒绝裸 token 和重复认证头，且 token 不能充当或覆盖逻辑设备 ID。`XIAOAI_MCP_DEVICE_ID` 由服务端配置且不出现在工具参数中；一个 token 对应一个逻辑入口。可选 `XIAOAI_MCP_MAX_BODY_BYTES`、`XIAOAI_MCP_REQUESTS_PER_MINUTE`、`XIAOAI_MCP_MAX_CONCURRENT_REQUESTS`、`XIAOAI_MCP_BODY_READ_TIMEOUT_SECONDS`、`XIAOAI_MCP_MAX_INPUT_CHARS`、`XIAOAI_MCP_MAX_OUTPUT_CHARS` 和 `XIAOAI_MCP_SEND_CACHE_SECONDS` 均有闭合范围。附件入口还要求不含路径的 HTTPS origin `XIAOAI_MCP_PUBLIC_BASE_URL` 和仓库外绝对目录 `XIAOAI_MCP_ATTACHMENT_ROOT`；`XIAOAI_MCP_MAX_ATTACHMENT_BYTES` 固定为 `52428800`，上传授权默认 600 秒、已上传附件默认 1800 秒，可分别用 `XIAOAI_MCP_UPLOAD_TTL_SECONDS` 和 `XIAOAI_MCP_ATTACHMENT_TTL_SECONDS` 在闭合范围内调整。网关使用无服务端会话累积的 stateless Streamable HTTP；请求正文断连、超时、类型异常、超限或并发槽耗尽时在进入工具前失败。公网反向隧道必须把外部 HTTPS Host 重写为回环 Host，并且只转发该端口的 `/mcp`、`/upload` 与 `/attachments`；不得转发 `8000/8003/8765/8766/3080`。

小爱 MCP 工具固定为 `list_agents`、`list_sessions`、`show_binding`、`bind_session`、`prepare_attachment_upload`、`send_message`。会话列表只返回 ID、标题和最小时间/计数/运行状态，不返回 preview、cwd 或历史正文。首次绑定必须指向刚列出的真实会话；已有绑定的改绑先返回 180 秒一次性 `confirmation_id`，只有用户明确确认后以相同目标和编号再次调用才落盘。`prepare_attachment_upload` 绑定当时完整 binding snapshot，返回预分配的 `attachment_id` 和只含高熵一次性 token 的短时 HTTPS 上传页；不得复用长期 MCP Bearer。上传流式写入临时文件，校验大小、扩展名、MIME、魔数或 UTF-8 文本后原子发布；允许 JPEG、PNG、WebP、PDF、TXT、MD、CSV、JSON，拒绝未知类型、SVG/HTML、脚本、可执行文件、压缩包和 Office 文件。单文件上限 50 MiB，每条消息最多 4 个附件且合计最多 100 MiB；Hermes 图片另受 25 MiB 上限。`send_message` 要求客户端提供稳定 `request_id`，正文先受字符上限约束，可选 `attachment_ids` 只能引用已完成且绑定快照仍匹配的附件；同一进程缓存窗口内重复 ID+正文+相同附件集合只回放原结果，ID 相同而正文或附件不同则拒绝；未知结果和请求取消同样缓存且禁止自动重发。缓存 TTL 从宿主调用完成时开始，统一执行过期清理与 256 条硬上限；确定未写入宿主的未绑定/参数拒绝不占用写入去重缓存。这是有限去重，不承诺客户端用新 ID 重新发送时的 exactly-once。

可选 `deepseek_harness` 块只接受数值回环 HTTP origin、仓库外绝对 `dsh_home`、精确版本 `0.2.0-rc.2`、`0.2.1-rc.5` 或 `0.2.2-rc.1`、只保存变量名的 `launch_token_env` 和显式 `allowed_presets`。每个实例必须声明并匹配其中一个版本，不接受 `latest`、范围、自动降级或跨版本静默回退。启动凭据由 DSH Web 每次启动生成，只能暂存在父进程环境和网关内存中，不写入配置、绑定、响应或持久日志。Preset 白名单由外部配置的 `allowed_presets` 明确提供，公开示例不附带维护者的真实列表。各版本的会话接口和升级验证见 [DSH Web 兼容说明](references/deepseek-harness-compatibility.md)。

Desktop 共享模式另有一个仓库外绑定文件，默认位于 `%LOCALAPPDATA%\agent-workflow-hub\xiaozhi-hermes-bindings.json`，可用 `XIAOZHI_HERMES_BINDING_STORE` 指向绝对路径。绑定键必须使用 Xiaozhi 稳定 `device_id`，不能使用每次 WebSocket 重连都会变化的 `session_id`。文件只保存设备 ID、host、Profile/Preset、宿主 session ID、标题和 revision，不保存 Hermes/DSH 会话令牌、聊天内容或 API 密钥，并使用临时文件加原子替换写入。旧 1.0/1.1 绑定在读取时解释为 `host=hermes`，下一次写入迁移到 1.2。

Hermes Desktop 传输优先使用 0.22+ 原生 Gateway：通过 Hermes 安装器报告的运行时 Python，在同用户私有控制通道中只读发现当前 Profile 的 loopback endpoint，并为每次 WebSocket 连接签发一次性、Profile-scoped interactive ticket；不启动、重启或修改 Hermes。ticket 只在私有子进程 stdio 和 WebSocket 子协议中短暂存在，不写入绑定、配置、响应或日志。Hermes 0.21 仍可回退到已有的 live serve ledger + 匹配发布 Token，二者均继续使用同一 `session.list/resume/create/compress`、`prompt.submit` 和附件 RPC，因此 canonical session 与电脑端接手语义不变。不得在原生发现失败时改走 DeepSeek Harness。

可选控制面仅在 Xiaozhi 进程环境显式设置 `LINGXI_CONTROL_ENABLED=1` 时启用。`LINGXI_RUNTIME_CONFIG` 必须是当前 Xiaozhi `data/.config.yaml` 的仓库外绝对路径；可选 `LINGXI_CONTROL_BACKUP_DIR` 也必须是绝对路径。默认监听 `127.0.0.1:8765` 管理端和 `127.0.0.1:8766` 伴随屏，可用 `LINGXI_CONTROL_ADMIN_PORT` / `LINGXI_CONTROL_COMPANION_PORT` 改为两个不同的非特权端口。两端默认密码均为 `admin`；`LINGXI_CONTROL_PASSWORD` 可统一覆盖，`LINGXI_CONTROL_ADMIN_PASSWORD` / `LINGXI_CONTROL_COMPANION_PASSWORD` 可分别覆盖且优先级更高。空白或超过 256 字符的显式配置拒绝启动，不静默回退。密码可重复登录并换取固定 scope 的短期 HttpOnly、SameSite=Strict Cookie 和 CSRF token；登录失败有有界限流，每端活动会话数有固定上限。密码不进入命令参数、响应、日志、浏览器 DTO 或文件；旧 bootstrap 文件和 code 均不再读取或认证。管理端可列 Agent/会话、revision-safe 绑定/解绑及配置 preview/apply/rollback；伴随屏只能读取投影状态和版本化事件、停止当前播报、设置设备明确暴露的绝对音量，以及确认或取消设备当前唯一 prepared action。两端均拒绝非回环 Host、跨源 Origin、CORS、非 JSON 写请求和超限正文。默认 `admin` 仅用于用户确认的本机便利模式，不构成操作系统用户隔离；多人或不受信本机环境必须配置不同强密码。

设备能力中心还必须符合 `references/device-snapshot.schema.json`、`references/device-capability.schema.json` 和 `references/device-state-event.schema.json`。每次实体连接使用独立 `connection_generation`；MCP 完整分页后登记能力，断线按设备、会话和 generation 精确注销。所有设备即时控制都携带 `expected_generation`，旧连接、断线竞态或结果无法确认时不得伪造成功。公开能力使用稳定 capability ID，不把原始 MCP 工具名、SSID、地址、聊天正文或秘密返回浏览器。统一状态分为 device state、conversation phase 和版本化 expression；OLED 的固件本地状态机仍是设备状态权威，电脑伴随屏只消费投影。

音频诊断只允许采样率、启停状态、音量、增益、RMS、峰值、空读/丢帧计数等有界指标，禁止原始 PCM、录音、ASR 正文和持久音频。停止播报在没有设备 ACK 的上游版本中只能报告 `requested`，不能报告 `stopped` 或 `acknowledged`。首次录音不得自动播放诊断音；测试音属于需要精确确认的后续维护动作。

更新清单和预检分别符合 `references/release-manifest.schema.json` 与 `references/device-operation-plan.schema.json`。清单必须使用受信 Ed25519 key、HTTPS 制品、SHA-256、精确板型、大小、过期时间和防降级序号；预检计划在当前阶段固定 `executable=false`。在设备端签名信任锚、固件 A/B、资源 A/B、认证配网、执行层一次性确认和设备 ACK 全部完成并通过断电实机门前，浏览器不得提供 OTA、资源写入、远程重启或进入配网的执行入口。清 Wi-Fi、清 NVS 和恢复出厂永久保持 physical-only。

运行配置 UI 只允许白名单字段：`llm_mode`、TTS provider/voice、ASR/VAD provider、VAD 停顿和工具超时。预览绑定当前文件 SHA-256，apply 在摘要未漂移时创建备份并同目录原子替换；rollback 同样要求 expected digest。UI 必须分别显示 `configured`（磁盘）、`loaded`（本进程启动时）、`observed`（设备已发布事实）和 `pending_restart`，写入后不得宣称已经生效或自动重启服务。版本化状态事件只保存设备 ID、状态、操作、host/agent/session 引用、binding revision 和 action ID/有效期，不保存 ASR 正文、Agent 输出、密钥、令牌、工作目录或原始网关错误。

请求必须符合 `references/bridge-request.schema.json`，每条请求的 `caller_id` 必须匹配配置。JSON Schema 负责结构约束；Profile 唯一性、默认 Profile 属于白名单、端口范围及 URL 中 Profile 路径相等由 Python 解析器执行第二阶段语义校验。除原有 Profile 和普通消息操作外，共享会话还提供：

- `list_agents`：返回带 `host` 的统一 Agent 列表；同名 Profile/Preset 仍由 host 消除歧义；
- `list_profiles`：返回白名单和当前会话的活动 Profile；
- `switch_profile`：只切换当前 `session_id` 的内存状态；
- `dispatch`：向当前活动 Profile 提交 `input`。配置要求确认时，请求必须包含 `confirmed: true`；它是受信 stdio 父进程对“已完成真实用户确认”的声明，不是桥接自行完成的身份认证。
- `list_sessions` / `bind_session` / `show_binding` / `unbind_session`：列举、绑定、查看或解绑稳定设备与既有宿主对话；`list_sessions` 和 `bind_session` 显式接受 `host`，省略时仅为旧客户端默认 `hermes`。语音兼容调用可省略 `binding_revision`；浏览器 bind/unbind 必须携带当前 revision，未绑定为 `0`，桥接在设备锁内比较后才写入；
- `dispatch_shared`：供桌面和兼容客户端向稳定绑定发送消息，始终要求真实的 `confirmed: true`；
- `dispatch_bound_voice`：仅供受信 Xiaozhi 私有父进程提交最终普通语音；只有 `bound_voice_dispatch_mode=direct` 时免逐条确认，并禁止携带 `confirmed`。两种共享发送都必须携带读取绑定时的 `host`、Profile/Preset、宿主 session ID 和 binding revision；桥接在跨进程设备操作锁内重新匹配该不可变快照，再完成唯一一次宿主写入；
- `dispatch_bound_voice` 只有在私有父进程显式发送 `stream_progress: true` 后，才可在最终响应前发送 `status=progress` 的累计公开正文；旧单帧客户端未协商时永远只收到最终帧。Hermes 可投递累计增量，DeepSeek Harness 仅在 durable `turn/end` 后投递已确认的最终正文，避免中间 assistant message 覆盖最终结论。父进程只把进度帧用于同一设备、同一 TTS generation 和不可变 sentence ID 的动态字幕与有界分句 TTS，不把它当作第二次宿主写入；停止后旧 generation 的进度、最终补句和字幕必须全部丢弃。`cancel_bound_voice` 只能由受信私有父进程携带当前 in-flight 快照调用，通过独立控制子进程执行 Hermes `session.interrupt` 或 DeepSeek Harness `session/cancel`，并保留 cancelling 墓碑直至数据面与控制面都收敛，期间禁止下一回合；DSH `{accepted:true}` 只返回 `requested`，不得冒充已观察到 `cancelled`；
- `create_session`：使用准备时的绑定快照调用原生 `session.create`，必须包含不可复用到其他逻辑操作的 `idempotency_key` 和 `confirmed: true`；明确成功后才以 CAS 原子改绑设备，旧会话不删除；
- `compress_session`：Hermes 按准备时的绑定快照恢复会话后调用原生 `session.compress`，始终要求 `confirmed: true`；只有明确 `compressed` 且返回 replacement stored session ID 时才以 CAS 更新绑定。DSH 三个受支持版本目前均未提供经过网关验证的压缩契约，因此明确返回不支持，不把文字“压缩上下文”作为普通 prompt 发送。

## 输出与命名规则

stdio 响应符合 `references/bridge-response.schema.json`，每次输出一行紧凑 UTF-8 JSON。响应包含 `schema_version` 和 `status`；请求已解析时还包含 `operation` 与 `session_id`。实时提交返回 Hermes 的 `run_id`、终态和完整 `output`。`rejected` 表示在外部调用前被契约、白名单、禁用开关、确认门或长度限制拒绝；只有确定未创建 run 的本地失败或 Hermes 明确 `failed` 才使用 `failed`。提交结果或轮询结果不确定时使用 `outcome_unknown`，已知时必须保留 `run_id`。输出不包含 API 密钥值。小爱 MCP 只投影上述六个工具所需的收敛字段；普通回复超过配置上限时明确截断并提示去电脑端同一会话查看全文，不返回 runtime session ID、宿主 URL、工作目录、密钥或底层异常正文。

小智语音中的“Profile”“智能体”和“Agent”默认都指统一目录，必须通过 `agent_list_agents` 按宿主分别列出 Hermes Profile 与 DeepSeek Harness Preset；只有用户明确限定“Hermes Profile”时才使用兼容的 `hermes_list_profiles`。统一热切换绑定的是完整 `{host, agent, session}`，不是旧 `hermes_switch_profile` 的临时内存选择：同名跨宿主先消歧，目标只有一条已有会话时可直接绑定，多条会话必须按稳定序号选择，没有会话时不得暗中新建。消歧期间保留旧绑定但不把普通正文转发给旧 Agent；取消后恢复正常路由。切换或绑定已有会话不增加二次确认。已有稳定绑定时，每个最终、非空的普通语音正文直接提交到当前 canonical session；不要求“交给当前智能体”前缀，也不逐条确认。创建新会话、压缩上下文及由“清除上下文”澄清得到的生命周期操作仍必须 prepare/confirm。

网关 LLM 支持 `bound_agent`、`local` 或 `remote` 模式。生产默认使用确定性的 `bound_agent` 路由：它不调用基础聊天模型，只识别管理短语，并把其他普通正文直接交给当前绑定的 Hermes/DSH Agent。`remote` 模式使用 OpenAI-compatible HTTPS endpoint，并只从 `api_key_env` 命名的进程环境变量读取密钥；配置、日志、响应和测试夹具不得保存密钥值。远端密钥缺失时必须启动失败或保留已经运行的旧配置，禁止静默回退到本地模型。DeepSeek Harness 的 launch token 不得当作 DeepSeek 模型 API key。本地 Ollama 和远端基础 LLM 都只作为用户显式选择的可选配置。

普通 Agent 完成态在 Xiaozhi 插件呈现边界生成最长 200 字的确定性设备投影：短答案去除 Markdown 格式后完整返回；表格、代码块、本机路径和长证据不逐字朗读，优先保留结论并提示完整内容位于电脑端当前 canonical 会话。桥接 `output` 和宿主历史保持全文不变。确认、取消、审批等待、失败、结果不确定、冲突、新会话和压缩状态绕过该投影，必须保留动作、确定性、下一步和禁止重试语义。MVP 不调用二次 LLM，也不增加配置字段。

`desktop-*` 命令使用 Hermes 原生 stored/runtime session ID。`desktop-list-sessions` 只返回人类可见会话摘要；`desktop-bind` 只接受目标 Profile 最近列表中精确存在的 ID；`desktop-send` 必须显式带 `--confirmed`，使用当前完整绑定快照，先 `session.resume`，再 `prompt.submit(surface=voice-live)`，并等待同一 runtime session 的 `message.complete`。语音生命周期操作通过私有 stdio 契约执行，不把 `/new` 或 `/compress` 当普通 prompt 发送。输出绝不包含 WebSocket URL 或会话令牌。

DeepSeek Harness 路径使用同一 Web 进程的 Session Controller：`agentPresets/list` 列 Agent，`session/list` 列会话，`session/prompt` 向已绑定 session 写入，`session/page`/`session/projections` 读取同一回合结果，`session/create` 创建新会话。网关以 prompt 的 `requestId` 对齐 user event 的 `rpcId`，只接受其后的最终 assistant text 和完成 turn；不得因为超时而静默新建会话、切回 Hermes 或盲目重放。

Hermes 原生 `session.create` 会立即生成 live runtime/stored ID，但无 seed 的空白会话要到第一条 `prompt.submit` 才写入 canonical `state.db` 并出现在 `session.list`。因此“开启新会话”成功后可以立即改绑和接收下一条语音，但电脑端侧栏在首条正文前可能不可见；这与客户端打开空白 composer 后尚未发送内容一致，不得伪造占位消息来强制显示。

## 依赖和运行前检查

先用绝对配置路径运行 `doctor`。它只验证本地配置，计算供启动确认使用的文件字节 SHA-256，并报告各 `api_key_env` 是否存在，不读取或打印值，也不发起网络请求。`configuration_ready_live_not_checked` 只表示配置和环境变量已就绪，绝不表示 Hermes 可用。Desktop 共享模式随后用 `desktop-doctor` 只读确认 Hermes Agent 0.21 legacy serve 或 0.22+ native gateway，并逐个确认白名单 Profile 可执行 `session.list`；0.22+ 原生模式不要求 `platforms.api_server` 或 `API_SERVER_KEY`。只有旧的非绑定 Runs API `dispatch` 路径才继续要求目标 Profile 启用 `platforms.api_server.enabled: true` 并提供对应 `API_SERVER_KEY`。配置与请求路径必须是已存在的绝对文件。

自建服务端集成固定使用 `xiaozhi-esp32-server` v0.9.7 的 commit `87c6df77b0220ddbd8fc804f984f83f69f3225e5`，细节见 `references/xiaozhi-server-v0.9.7.md`。安装器只向该精确版本写入四个工作流自有适配文件，必须先完整预检全部目标，再以同目录临时文件和原子替换逐项安装；遇到不同内容且不属于已登记受管版本的已有文件时拒绝覆盖。音频模块和 ESP32 不是 `doctor`、`simulate`、插件验证或回环 WebSocket 文本测试的前置条件。固件构建和板级接线由 Xiaozhi 源码工程负责，不在本工作流内复制 ESP-IDF 构建系统。

小爱入口复用官方 Python MCP SDK 的 stateless Streamable HTTP，不手写 MCP 协议；当前 Hub 固定 `mcp==1.27.2`，受管 Xiaozhi Python 运行时必须至少提供已验证兼容的 `mcp 1.22.0`、Starlette 与 Uvicorn。启动器在生成 DSH launch token 后才启动 MCP 子进程，使它与 Xiaozhi 共用既有私有桥接配置和仓库外绑定存储；MCP Bearer token 只注入该子进程，不注入 DSH 或 Xiaozhi 主进程，控制台密码和旧 Runs API key 不注入 MCP 子进程。MCP 子进程异常退出不得修改 canonical 历史或绑定；受管启动阶段未能建立回环 listener 时整体启动失败，退出清理必须终止 MCP 进程树。

## 系统修改与权限影响

`doctor` 和 `simulate` 不修改系统、不访问宿主。`desktop-doctor` 与 `desktop-list-sessions` 只读附着所选宿主；`desktop-bind` / `desktop-unbind` 只修改仓库外绑定文件。`desktop-send` 会把用户消息写入已绑定的 Hermes 或 DeepSeek Harness 对话，并可能触发对应 Agent 已获授权的工具；`create_session` 会在绑定宿主创建空白会话并改绑设备，`compress_session` 会压缩当前绑定会话。桥接不代答宿主 Agent 的审批、澄清、sudo、密钥或窗口请求，电脑端仍是这些交互的责任端。旧 `serve-stdio` 的 `dispatch` 继续通过回环 HTTP 创建独立 Hermes run。

配置只接受 `127.0.0.1` 或 `[::1]` 数值回环 HTTP，HTTP 客户端禁用环境代理和重定向，并把 Profile 限制在显式白名单中。stdio 必须是调用方创建并只传给桥接子进程的私有匿名管道，不监听命名管道、TCP 或共享终端。可选浏览器控制面只监听 `127.0.0.1`，且 admin/companion 使用不同 origin 和固定权限；网页永远不能把 `caller_id` 当网络凭据，也不能返回 API key、DSH launch token、Hermes/DSH URL 或 cookie、原始会话行、原始异常。小爱 MCP 同样只监听 `127.0.0.1`，使用独立 Bearer token、请求体上限、速率限制、固定服务端 device ID 和 `request_id` 去重；附件上传使用另一个短时一次性 token，固定 `application/octet-stream`，文件名从单独编码请求头读取，上传页禁止外部资源并设置严格 CSP。token 不进入页面正文，浏览器加载后立即从地址栏历史中移除；暂存文件只位于专用仓库外目录，发送前再次校验路径不逃逸、非符号链接、大小和 SHA-256。私有 `caller_id` 只存在于它与桥接子进程的匿名管道中，绝不作为网络凭据。常规 snapshot、版本化状态事件和持久日志继续禁止聊天正文、工作目录与 prompt/output。唯一例外是伴随屏经过 companion scope 登录后订阅的当前回合临时字幕：它可以原样投影灵溪当前问题和对应 Agent 回答，包括路径、URL、换行和 Markdown 字符，但只用 `textContent` 显示，不解析 HTML/Markdown；该投影不落盘、不进入事件回放和日志，也不提供历史查询。Profile 自身的工具、工作目录、外部账号和确认策略仍是实际权限边界；本工作流不扩大这些权限。

## 执行步骤

1. 完整读取本 Skill、桥接三个 JSON Schema；涉及能力中心、诊断、更新或恢复时再完整读取五个 `device-*` / `release-manifest` Schema；同时读取 `references/xiaozhi-server-v0.9.7.md` 和外部桥接配置，确认请求来自配置命名的唯一受信 Xiaozhi 服务端父进程。
2. 检查上游 checkout 的精确 commit，再以安装器 dry-run 检查四个目标；只有用户已批准的软件部署范围内才使用 `--apply`。不同内容的目标文件必须停下审查，不能强制覆盖。
3. 运行 `doctor` 检查配置、摘要和桥接环境变量；它不宣称实时就绪。随后用 Hermes 原生命令只读检查版本、Profile 注册和每个目标的 `platforms.api_server.enabled`；若配置 DSH，再检查 Web 包精确匹配配置声明的 `0.2.0-rc.2`、`0.2.1-rc.5` 或 `0.2.2-rc.1`、同一 Web origin 可认证以及白名单 Preset 健康。任一项不足时明确报告对应宿主不可用，不把请求回退到另一宿主。
4. 用 `simulate` 验证桥接协议，再运行插件验证器。启动回环服务后，用 `simulate-websocket` 验证“列表 → 切换 → 再列表”、普通正文直接进入当前绑定，以及“生命周期准备 → 取消”。静音测试 TTS 只证明协议事件，不代表真实语音验收。
5. 明确控制短语由本地兼容适配器确定性路由：统一列出 Agent、按 host 列会话、绑定、确认、取消、开启新会话和压缩上下文；其余最终非空语音直接交给当前稳定绑定。会话控制不得交给基础聊天模型猜测或作为普通 prompt 提交。“清除/清空/重置上下文”必须先追问是压缩当前会话还是开启空白新会话，澄清后再准备并确认，绝不删除历史。正文中只是提到这些短语时按普通正文处理。
6. 用户明确批准当前配置的白名单、调用方身份、确认规则和实时提交范围后，把 `doctor` 返回的精确 `config_sha256` 作为启动参数。字节不一致必须拒绝启动。
7. Windows 受管启动器必须为 Xiaozhi 子进程设置 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`，退出时恢复原环境，避免 SenseVoice 情绪 emoji 经 Loguru 写入 GBK 控制台时抛出编码异常；不得通过删除识别文本或 emoji 掩盖该问题。
8. 启用控制面时，先运行软件测试和隔离本地 demo，再检查密码配置优先级、失败限流、活动会话上限、两个回环端口、固定 scope、CSRF/Host/Origin 拒绝路径、浏览器 DTO 不泄密、revision 冲突和唯一 pending action；只有用户再次确认生产配置及短暂中断后才部署并重启。
9. 启用小爱 MCP 时，先用假适配器和本机 MCP 客户端验证六工具清单、401、MCP 请求体上限、固定 device ID、首次绑定、改绑确认和 `request_id` 去重；附件另验证一次性上传 token、过期/重放、类型与魔数、精确 50 MiB 边界、超限流式中止、绑定漂移、路径逃逸、部分文件清理和宿主原生附件接口。再由受管启动器以回环端口启动。公网试验只允许用已有且已认证的 HTTPS 隧道把该单端口发布，先验证错误 Bearer 和错误上传 token 全部失败，再把外部 `/mcp` 与 Bearer token 填入小爱。任何隧道 URL、进程或 token 变化都视为新的临时入口，不自动写入固件或长期配置。
10. 上游对每个请求写入一行 JSON，并逐行读取响应；同一对话始终复用稳定且不含秘密的 `session_id`。观察终态且不得在超时或未知结果后盲目重放。

## 人工确认门

创建或编辑配置、运行 `doctor` 和 `simulate` 不代表授权实时控制。首次运行 `serve-stdio` 前，必须让用户确认精确配置路径及 SHA-256、允许的 Profile、`dispatch_enabled`、`bound_voice_dispatch_mode`、旧 Runs API 的 `require_confirmation` 和 `trusted_caller_id`。扩大 Profile 白名单、启用设备语音直写、改变旧 Runs API 确认规则、改用新的上游服务或改变目标 Hermes 权限后会改变配置摘要，必须重新确认。

首次启用小爱 MCP 前必须让用户确认：逻辑 `device_id`、回环端口、六工具范围、现有桥接配置摘要、附件类型与 50 MiB 上限、需要短暂重启现有服务，以及将创建一个临时公网 HTTPS 入口。Bearer token 由高熵随机源生成，只在启动进程和用户当前手机配置之间传递，不写进仓库、桥接配置、日志或命令行。一次性上传 token 独立生成、绑定完整会话快照、默认 10 分钟失效且只能成功消费一次；已上传未发送附件默认 30 分钟失效并在最长一小时内清理。公网入口建立后先做未授权/错误 token 负向检查和只读工具检查；首次绑定由用户明确选择，已有绑定的任何改绑仍需工具返回的一次性确认编号。停止测试时关闭隧道即可立即撤销公网 URL；轮换 token 需要受管重启 MCP 子进程，不得复用默认 `admin`、Hermes API key 或 DSH launch token。

绑定已有会话是本地可逆状态变更，必须来自用户明确选择的设备 ID、host、Profile/Preset 和 session ID/标题。启用 `bound_voice_dispatch_mode=direct` 后，只有私有 `dispatch_bound_voice` 普通语音不逐条确认；`desktop-send` 和 `dispatch_shared` 仍要求真实确认。无绑定、绑定失效或 revision 漂移时零宿主写入并明确失败。

控制台 preview 只生成内存计划，apply/rollback 只写磁盘配置并创建保护备份；任何服务重启仍需单独取得用户对目标配置摘要和短暂中断的确认。伴随屏确认按钮只能消费设备当前公开的精确 `action_id`，不能构造任意 dispatch、新会话或压缩请求；停止播报和绝对音量只作用于当前注册的 live connection，设备离线或未暴露能力时必须禁用并明确失败。

旧非绑定 `dispatch` 是否需要二次确认由 `require_confirmation` 决定；为 `true` 时缺少 `confirmed: true` 必须在外部调用前失败。`dispatch_shared` 和 `desktop-send` 始终要求真实确认；`dispatch_bound_voice` 只在独立直写策略启用时免确认，且不得携带或伪造确认字段。`create_session` 与 `compress_session` 永久强制二次确认，不受上述配置开关影响。共享生命周期准备项绑定 device ID、host、Profile/Preset、Xiaozhi session ID、宿主 session ID、binding revision、内部操作 ID 和 180 秒有效期，以覆盖设备播报和一次语音重试；同一设备同时只能有一个待确认项。确认时先重新读取绑定，再把准备快照随执行请求提交。绑定、解绑、正文发送、创建和压缩在所有桥接进程间共用按 device ID 派生的操作锁，锁内重新匹配快照并覆盖宿主写入，避免复核与提交之间换绑导致目标漂移。直接正文提交期间也登记进程内单设备 in-flight 状态；播放停止先本地清队列，再以不可变绑定快照尝试取消宿主回合，未获得宿主确认时只能报告 `requested`。`pending`、`outcome_unknown`、`conflict`、`partial_failure` 或 `reconciliation_required` 后不得重复提交或自动重放，桥接进程重启后也不得恢复或重放未知请求。旧兼容 `dispatch` 路径只绑定其 Xiaozhi session、活动 Profile、正文和内部操作 ID，并使用相同 180 秒 TTL。桥接把私有 stdio 父进程列为可信计算基：生命周期确认字段只能由匹配 `trusted_caller_id` 的父进程在完成一次真实用户确认后写入，不得由设备端或桥接层无条件补写。小爱网络适配器使用独立 Bearer 认证、服务端固定设备身份、有限速率和显式 `request_id` 防重放契约；不能复用 caller_id 字符串充当凭据，也不能把小爱能访问 MCP 误写成拥有生命周期确认权限。

## 失败恢复

契约错误、未知 Profile、禁用提交、缺少确认、输入过长或密钥变量缺失都安全失败且不调用 Hermes。HTTP、认证、响应格式或轮询失败返回概括错误，不回显响应正文或 Authorization 值。POST 结果不明时返回无 `run_id` 的 `outcome_unknown`；已取得 `run_id` 后的任何轮询异常或超限都返回带该 ID 的 `outcome_unknown`。`waiting_for_approval` 不是失败重试信号；由用户在 Hermes 侧处理后发起新的明确请求。

桥接进程异常退出后，重新运行会清空会话内 Profile 选择并回到默认值。普通消息和压缩结果未知时不自动重放。新会话创建使用 Hermes 原生进程内幂等键，但该键不跨 Hermes 重启且只在同一待确认操作中复用；结果未知时同样不自动重试。Hermes 已完成而 CAS 改绑冲突时返回 `conflict`，写盘失败时返回 `partial_failure`，均保留新 stored session ID 供电脑端核对。

空白新会话在首条 prompt 前只有 live runtime；若 Hermes 后端恰在此窗口重启，绑定可能指向尚未落库的 ID。下一次发送必须让 `session.resume` 明确成功后才能提交；找不到目标时应报错并由用户重新选择或重新开启会话，不能静默回退到旧会话，也不能自动重放创建操作。

## 重跑、幂等与覆盖策略

`doctor`、`desktop-doctor`、列举会话、显示绑定和绑定到同一目标可安全重跑；解绑可逆但会丢失目标选择。绑定文件使用跨进程设备操作锁、revision 和 compare-and-swap，防止较早的宿主写入或生命周期操作与新的用户改绑交错。`simulate` 永不提交。每个 `dispatch`、`dispatch_shared`、`dispatch_bound_voice` 或 `desktop-send` 都可能创建新用户回合，不视为幂等；普通共享正文超时、进程退出或终态未知后不得重放，压缩也不得在 `pending` 或终态未知后重放。工作流不写配置、不覆盖请求文件、不保存对话内容或密钥。

## 验收标准

- 配置只接受数值回环 HTTP 和精确 `/p/<profile>` 路径，禁用环境代理，拒绝 URL 凭据、查询、片段、重定向及重复 Profile。
- 启动绑定已确认的配置文件 SHA-256；stdio 仅由配置命名的受信父进程通过私有匿名管道调用，caller_id 不被描述成网络认证。
- Profile 列表和切换完全受白名单约束，且不同 `session_id` 的活动 Profile 相互隔离。
- `dispatch_enabled: false` 时所有提交零外部调用；旧 `dispatch`、`dispatch_shared`、`desktop-send` 和生命周期操作确认缺失时零外部调用；`dispatch_bound_voice` 只有独立直写策略为 `direct` 时免确认。输入长度在调用前检查。
- API 密钥仅从命名环境变量读取，配置、请求、响应、异常和测试夹具均不保存真实值。
- Runs API 使用 `POST .../v1/runs` 和有界 `GET .../v1/runs/<run_id>`；run ID 被编码为单个路径段，未知状态立即转为 `outcome_unknown`，遇到审批等待或终态立即停止，不自动批准、不盲目重放。
- 单行请求、响应和内存会话数均有固定上限；坏行、超长行和单请求内部错误不得终止后续 stdio 请求。
- 无硬件条件下完成契约、路由、拒绝路径和模拟测试；既有确认策略的真实语音和音频链路已验收。新 `bound_agent` 直达策略仍需一次实体普通语音 smoke；HID 和会议工作流继续标记为等待后续验证。
- `desktop-doctor` 能安全发现实时 Hermes 后端且不泄露令牌；七项 Profile 均可通过 `session.list` 列出原生会话。
- 设备绑定跨桥接进程和 Xiaozhi WebSocket 重连保持；重连后的首条最终非空普通语音无需前缀或确认，直接使用同一 stored session，电脑端可看到小智消息与回复，电脑续聊后小智下一轮继承新增历史。
- `list_agents` 同时返回 Hermes Profile 和 DSH Preset 且每项携带 host；绑定 DeepSeek Harness 后发送只调用 DSH Web，绑定 Hermes 后只调用 Hermes Desktop，不广播、不自动回退。
- 普通“Profile / 智能体 / Agent 列表”必须确定性调用统一目录并完整按 host 返回；同名 Agent 的热切换先选择 host，再选择已有会话，切换未完成期间同一设备的任何连接都不能把正文提交给旧绑定，取消或过期后原绑定保持不变。
- `bound_agent` 是生产默认网关模式，不构造 OpenAI/Ollama 客户端；远端基础 LLM 与本地 Ollama 是两个可选显式配置，缺少远端 API key 时必须拒绝切换或继续保留已运行的旧配置，二者都不得作为故障回退。
- DeepSeek Harness 只接受精确版本 `0.2.0-rc.2`、`0.2.1-rc.5` 或 `0.2.2-rc.1` 和数值回环 Web origin；每个实例只选择一个匹配已安装包的版本，launch token 不出现在配置、响应、绑定文件或残留 stdout 日志。
- “开启/新建/重新开始会话”必须确定性进入准备门，确认后恰好调用一次原生 `session.create`，成功后 CAS 改绑，失败或结果未知时保留旧绑定；旧会话不删除。
- 会话控制短语只按规范化后的完整用户话语精确匹配；正文中仅包含“开启新会话”或“压缩上下文”不得触发生命周期工具。显式“交给当前智能体：<正文>”作为兼容别名剥离前缀后直接提交一次，不进入普通消息确认门。
- “压缩上下文/压缩当前会话”必须确定性进入准备门，确认后恢复当前会话并调用原生 `session.compress`；压缩使用不少于 660 秒的独立 RPC 等待，恢复时省略历史传输。只有明确 `compressed` 与 replacement ID 才改绑；`aborted`、`pending`、畸形响应或断线不得宣称成功。
- “清除/清空/重置上下文”只返回“压缩或新会话”的澄清，不产生 Hermes 写调用；用户选择后仍需独立准备和确认。
- 回环 WebSocket 文本测试必须实际看到工具调用事件，并证明完整 Agent 目录、稳定设备绑定、普通正文直接路由及生命周期 prepare/cancel 确认门；模型只口头声称成功不算通过。
- 管理端和伴随屏分别使用独立回环 listener、可分别配置且默认 `admin` 的密码、固定 scope Cookie 和 CSRF；专用密码覆盖共享密码，旧 bootstrap code 永远不能登录。admin 不能调用伴随屏即时控制，companion 不能列会话、改绑或写配置；跨端 Cookie、恶意 Host/Origin、暴力猜测、未知字段和超限正文均失败。
- 所有浏览器写操作调度回拥有语音适配器的 Xiaozhi event loop；不得由独立 sidecar 构造新的 `SharedConversationService` 或网关直接写宿主。网页 bind/unbind 使用 expected binding revision，语音热切换进行中拒绝网页换绑。
- 控制台配置事务完整保留 YAML 未修改区域和注释，摘要漂移拒绝 apply/rollback；页面明确区分 configured/loaded/observed/pending restart，不自动重启。状态事件有 runtime epoch、单调 event/state version 与固定上限，且不含聊天正文或秘密。
- 伴随屏的停止播报和音量按钮必须调用当前 live device 的真实服务端/设备 MCP 路径；能力不可用时显示禁用，不伪造成功。麦克风动效只由真实采集生命周期驱动，发声表情只由真实 TTS 生命周期驱动。当前回合字幕使用独立、无历史回放的临时 broker：订阅令牌绑定浏览器 session、设备和 connection generation；首次订阅以当时 sequence 建立不可后退的 baseline。同一页面可用只存于 JS 内存、绑定 session/device/generation/stream epoch 且在最后一次有效轮询后最多保留 5 分钟的高熵续接令牌恢复原 baseline；刷新、新标签页、隐私模式、显式退订、登出、断线、设备切换或 generation 变化必须撤销续接资格。前端轮询和恢复必须 single-flight，旧订阅响应不得覆盖新 turn/revision。问题最多 512 字符；回答临时窗口最多 16384 字符，超限时保留开头 2048 字符和最新结尾 14336 字符，并单独显示中间省略数量。TTS 播报结束后正文最多保留 60 秒；超时后只可暂存无正文的当前 turn tombstone 以接收迟到结果，新回合开始后旧 Agent 或 TTS 结果必须拒绝。字幕按 Agent 原文保留路径、URL、换行和 Markdown 字符，但前端只以 `textContent` 展示。不得把 ASR、Agent 正文或续接令牌写入 snapshot、状态事件、浏览器持久存储、磁盘、日志或可回放历史。
- 能力中心只在当前 connection generation 上有效；重连后旧 generation 的绑定、确认、停止、音量和诊断请求全部拒绝。只读状态不暴露 SSID、IP、原始 MCP 名称或未知字段，音频诊断不含 PCM/录音/正文。
- 签名发布清单的错误 key、错误板型、降级序号、过期、HTTP URL、摘要篡改和未知字段均在任何设备写入前失败；当前维护预检始终 `executable=false`，UI 明确列出安全门阻断项，不出现 OTA、资源写入、远程重启或配网执行按钮。
- 小爱 MCP 工具清单必须精确为六项，输入 Schema 不含 `device_id`、任意 URL、shell、本机路径、Base64 或生命周期操作；只有 `prepare_attachment_upload` 可签发上传页，`send_message` 只接受不透明 `attachment_ids`。无/错 Bearer 为 401，错误/过期/重放上传 token 失败，超限 MCP 正文和速率请求在工具执行前失败。附件只允许 JPEG/PNG/WebP/PDF/TXT/MD/CSV/JSON，单文件精确 50 MiB 可接受、超过一个字节即拒绝，Hermes 图片超过 25 MiB 拒绝；每条消息最多 4 个且合计 100 MiB，换绑后旧附件不得发送。Hermes 使用原生 `image.attach`/`pdf.attach`/`file.attach`，DeepSeek Harness 使用原生流式上传 receipt，不把文件转成 MCP JSON Base64。首次绑定只接受真实列举会话，已有绑定改绑必须消费匹配目标且未过期的一次性确认编号；同一 `request_id`+正文+附件集合重复调用不产生第二个宿主回合，ID 相同而正文或附件不同必须拒绝。手机成功发送后，电脑端同一 canonical 会话能看到消息、附件与回复并继续接手；小爱客户端能否展示并打开上传 URL 必须实机验证，其是否完整展示、朗读、流式输出、保留自身 UI 历史或用新 ID 重试不属于服务端保证。

## 清理方式

停止前台 Xiaozhi 服务会连同私有 `serve-stdio` 子进程清除待确认内存状态，但不会删除设备绑定。附件暂存目录中的过期授权、失败临时文件和未发送附件由网关有界清理；明确发送完成后删除对应暂存文件，结果未知时不自动重发。用户要求解除绑定时优先运行 `desktop-unbind`；只有用户明确要求删除整个绑定文件时才删除该仓库外文件。不得删除 Hermes Profile、聊天记录、`.env`、API 密钥、Xiaozhi 源码、模型、固件产物或 ESP-IDF 工具链。
