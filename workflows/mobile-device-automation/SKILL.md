---
name: mobile-device-automation
description: Use when an Agent must explore and automate an Android physical device or emulator with agent-device, then generate a verified mixed replay with conditional Airtest image matching.
compatibility: Agent Workflow Hub spec 1.0; Android via agent-device 0.21.19 and optional Airtest 1.4.3 image replay; iOS reserved but unsupported in v1.
metadata:
  spec-version: "1.0"
  workflow-version: "0.1.0"
  display-name: "Mobile Device Automation"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '["cli.agent-device"]'
  capability-slots: '{"image-replay":["python.airtest"]}'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"doctor":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py doctor --mode <MODE> [--serial <SERIAL>]","devices":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py devices [--serial <SERIAL>]","capture":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py capture --serial <SERIAL> --output <ABSOLUTE_PNG> [--x <X> --y <Y> --width <WIDTH> --height <HEIGHT>]","locate-image":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py locate-image --serial <SERIAL> --template <ABSOLUTE_IMAGE> [--threshold 0.85 --timeout-seconds 10]","click-image":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py click-image --serial <SERIAL> --template <ABSOLUTE_IMAGE> --evidence-dir <ABSOLUTE_DIRECTORY> --action-id <ID> [--threshold 0.85 --timeout-seconds 10]","compile":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py compile --request <ABSOLUTE_JSON> --output-root <ABSOLUTE_DIRECTORY>","compose":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py compose --request <ABSOLUTE_COMPOSITION_REQUEST_JSON> --output-root <ABSOLUTE_DIRECTORY>","preview":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py preview --bundle <ABSOLUTE_BUNDLE_DIRECTORY>","replay":"python <HUB_ROOT>/workflows/mobile-device-automation/scripts/mobile_device_automation.py replay --bundle <ABSOLUTE_BUNDLE_DIRECTORY> [--parameters <ABSOLUTE_YAML>] [--confirmed-plan-sha256 <SHA256>]"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# 手机设备自动化

## 用途与触发条件

当用户要求 Agent 操作 Android 真机、本机 Android 模拟器、Android 原生应用、第三方应用、桌面或系统界面时使用。首选 `agent-device` 的无障碍快照、稳定 selector、动作和 `.ad` 回放；只有控件树不能可靠表达目标且具备安全图片模板时，才使用 Airtest 在同一 Android 设备上识图和触摸。

支持 USB ADB、无线 ADB 和模拟器。首版不支持 iOS；接口预留不代表 iOS 可执行。动态 Agent 可以直接调用上游 agent-device MCP 探索未知界面，Hub 不代理通用 MCP 工具，也不重新实现上游动作协议。Hub 的 CLI 负责只读诊断、设备绑定、图片步骤、混合 bundle 编译、预览、授权校验、回放和证据归档。

## 非目标

- 不复制、分叉或重写 agent-device、Airtest、ADB、uiautomator2 的控件、触摸、截图或匹配源码。
- 不从主机桌面点击模拟器窗口；Airtest 直接通过 Android serial 连接真机或模拟器。
- 不支持 Root、验证码绕过、DRM/安全截图绕过、设备安全机制规避或隐藏式权限提升。
- 不实现 Appium、Artemis、Maestro 或 STF 管理层，也不把它们设为首版依赖。
- 不实现大规模库存、预约、租赁、配额和远程实验室管理；DeviceFarmer/STF 只作为未来 provider 扩展边界。
- 图片失败不得静默回退到历史坐标、探索坐标或另一执行器。
- 不自动修复失效脚本，不自动安装 Node.js、Android SDK、USB 驱动，不修改宿主 MCP 注册或系统权限。
- 不把 serial、无线 ADB 地址、截图、日志、账号或本机绝对路径写入公开源码。

## 输入

动态探索输入为用户任务、目标 Android 设备范围、目标应用或系统界面，以及允许的外部影响。执行前用 `devices` 枚举目标；请求指定 serial 时精确绑定，未指定且有多个同等在线目标时返回 `ambiguous_device`，不得猜选。设备身份同时包括 transport、宽高、方向和 density；动作前身份变化返回 `target_identity_changed` 或 `viewport_changed`。

固化输入是符合 `references/automation-request.schema.json` 的绝对路径 JSON。它只接受已清理并由用户确认的有效路径，包含 Android 目标、唯一 serial、USB/无线/模拟器 transport、viewport、参数、按顺序执行的步骤、后置断言和固化决策。语义步骤引用绝对 `.ad` 文件；图片步骤引用绝对 PNG/JPEG。敏感参数只能在运行时提供，不得在 bundle 中保存默认明文。

工作流参数继续使用 schema 中的可读小写名称；执行 agent-device 时，Hub 在隔离 state-dir 内把脚本占位符确定性转换为上游要求的全大写环境变量，并用安全转义后的实际值解析 selector 断言。转换后重名（例如 `foo-bar` 与 `foo_bar`）或落入保留的 `AD_*` 命名空间必须在设备输入前拒绝，不能把小写 `-e` 参数直接传给 agent-device。

同一应用内的公共步骤应建模为“已完成后可验证”的状态转换组件，而不是单次点击。私有 catalog 放在 `workspace/workflows/mobile-device-automation/apps/<app-id>/`，按 `components/<component-id>/vNNN/component.yaml` 保存精确不可变版本，并由 `scenarios/*.yaml` 线性组合。组件、场景和组合请求分别遵守 `app-component.schema.json`、`app-scenario.schema.json` 和 `app-composition-request.schema.json`；真实 selector、serial、账号和证据不进入公开目录。

组件在编译期展开为现有 agent-device 步骤，bundle 不保留运行时 include 或 catalog 绝对路径。每个组件必须有主要动作和强制 postcondition；通常还要有 precondition。状态归一化组件可省略 precondition，但必须有 already-complete 且 effect 不高于 idempotent。already-complete 命中时不执行动作；前置状态不满足时最多执行一次恢复，随后重新验证，不能循环恢复或自动重放有外部影响的动作。

步骤副作用为 `none|read|idempotent|create|submit|send|delete`。卸载应用、清除应用数据、删除用户文件或业务数据、恢复出厂设置、删除账号/配置/凭据及上游声明的其它不可逆动作必须标为破坏性，不能伪装成普通 selector 或图片点击。

## 输出与命名规则

探索截图、候选模板、视频、Logcat、工具结果和临时证据写入 `workspace/workflows/mobile-device-automation/`；该目录是私有运行时，不进入 Git。确认固化的 bundle 默认写入 `workflows/mobile-device-automation/outputs/<name>-vNNN/` 或用户明确给出的绝对目录；outputs 同样不进入 Git。

每次编译创建新版本，不覆盖已有目录。bundle 包含 `workflow.yaml`、`metadata.json`、`parameters.example.yaml`、`flows/*.ad`、`images/*` 和 `assertions/*`。所有执行资产复制进 bundle 并记录 SHA-256；清单只保存相对路径。初始状态为 `generated-unverified`，独立回放通过后为 `replay-verified`，失败为 `replay-failed`。没有真实回放证据不得宣称已验证。

运行证据使用新的 evidence 目录，至少记录脱敏后的步骤结果、动作前后截图、模板哈希、匹配分数和合格匹配数量、设备 transport、耗时和最终状态。最终结果不得保存参数值、无线地址或未裁剪的上游敏感输出。

## 依赖和运行前检查

完整阅读 `capabilities/cli/agent-device/CAPABILITY.md` 和选用图片能力时的 `capabilities/python/airtest/CAPABILITY.md`。先执行 `doctor --mode explore|image|full`，再执行 `devices`；doctor 是只读检查，不自动安装或连接未授权设备。

`cli.agent-device` 是必需能力，锁定 `agent-device@0.21.19`，要求 Node.js `>=22.12`。本地存在 CLI 不等于宿主已经连接 agent-device MCP；动态 MCP 路径必须由当前宿主实际工具清单证明。安装只按能力契约允许的固定版本、来源、完整性和目标执行。

`python.airtest` 只在 `image-replay` 槽位被选择时需要，锁定 Airtest 1.4.3 并使用工作流私有 Python 3.11 runtime。Windows x64 移动 profile 具有 hash lock；macOS arm64 和 Linux x64 当前因上游 `pywin32` 依赖无法生成可安装 lock，doctor 必须报告 `unverified-runtime-lock`，不得声称图片路径 ready，也不得伪造哈希或放宽 `--require-hashes`。

ADB、Android SDK、驱动、设备开发者模式、USB 调试和无线调试属于用户/系统环境。缺失时报告准确缺口和官方修复指引；不得擅自提升权限或换用未声明安装器。安全截图页、厂商禁用 ADB 输入或设备离线时如实返回失败。

Android 真机输入非 ASCII 文本时，优先使用与锁定版 agent-device 配套的官方 IME Helper，并在建立会话的 `open` 命令中显式加入 `--test-ime`。doctor 的 `ime_helper` 字段报告 `installed`、`missing`、`wrong-version`、`unverified` 或 `not-checked`；`text_input_mode` 报告 `direct-ime`、`pinyin-fallback` 或 `not-checked`。厂商系统可能在 ADB 安装时返回 `INSTALL_FAILED_USER_RESTRICTED` 并要求用户在设备上确认“通过 USB 安装”或“危险应用”；不得循环安装、绕过安全设置或改用未声明 APK。

Helper 缺失、版本不符或无法验证时，普通文本输入可静默采用拼音降级，不因 Helper 单独阻塞 doctor。作者必须把目标中文转换为 ASCII 拼音，聚焦目标输入框后 `fill` 拼音，等待系统输入法候选或应用内建议出现，按目标中文的可访问文本唯一定位并点击，最后用新快照验证输入框值或业务结果等于目标中文。候选缺失、不唯一或最终文本不一致时停止并返回失败；不得把拼音本身或未经验证的近似候选当作成功。该策略不要求引入拼音库，允许智能体在生成私有脚本时完成常见中文转写；无法可靠转写的内容不得降级。

请求或运行参数使 `fill` 的实际输入包含非 ASCII 字符、但脚本此前没有 `open --test-ime` 时，仍必须在任何设备动作前返回 `test_ime_required`，因为已编译脚本不能在运行时凭空补出候选选择步骤。生成或修复脚本时，根据 doctor 的 `text_input_mode` 选择直接中文脚本或上述拼音候选脚本，无需再次询问用户。IME 不可用本身不要求改用 Airtest，图片定位仍只用于语义目标无法唯一定位的场景。

## 系统修改与权限影响

只读 doctor、设备枚举、截图、界面快照和定位不应修改设备业务状态。普通点击、输入、滑动、应用启动/停止和系统按键会改变当前设备界面；只能在用户任务范围内执行，并应在动作后重新读取状态验证结果。

安装或更新 agent-device/Airtest 只能写入能力契约声明的 Hub runtime。工作流不修改防火墙、系统服务、Android SDK、驱动、MCP 配置、设备开发者选项或账号设置。设备端出现系统授权弹窗时，应由用户明确处理，不能规避系统授权。

破坏性动作必须先 `preview`，再获得与本次 `plan_sha256` 完全一致的明确授权，并通过 `--confirmed-plan-sha256` 传入 replay。计划、目标、参数、副作用、模板、断言或资产变化都会使旧授权失效。普通安装应用、发送消息、支付、发布等虽然不一定属于 delete，仍只能来自用户明确任务范围并受宿主与上游安全策略约束。

## 执行步骤

1. 运行 doctor 和 devices，绑定唯一 Android serial，记录 transport 与 viewport；多个候选返回 `ambiguous_device`。
2. 动态探索每一步都执行“读取最新界面 → 按稳定 selector 选择目标 → 动作 → 重新读取界面 → 验证后置状态”。优先资源 ID、测试 ID、文本、标签、角色及组合 selector；短生命周期引用和坐标不得跨快照复用。
3. 宿主拥有 agent-device MCP 时可以直接调用上游 MCP 完成动态探索；否则使用已声明 CLI/.ad 能力。Hub 不代理通用 MCP 工具，也不在 Python 中复制 generic tap/type/swipe API。
4. 路径成功后去掉误点、回退、重复等待和探索坐标，只保留有效 `.ad` 语义片段及明确断言。
5. 控件路径不足时，用 `capture` 从同一设备的新鲜截图裁剪不敏感模板。模板记录截图哈希、裁剪框、viewport、方向、density、阈值和说明。
6. 先 `locate-image`。每次匹配读取新鲜设备画面并要求恰好一个候选达到阈值；低于阈值、不唯一、超出视口或 viewport 改变时停止。
7. 需要真实图片动作时执行 `click-image`。它保存 before，重新定位唯一中心，向同一 serial 点击一次，再保存 after 并由 selector、前台包名或独立图片断言验证。图片失败不得静默回退坐标。
8. 单次专用流程可把已确认步骤写成请求，用 `compile` 生成递增 bundle。同一应用的重复流程优先把公共状态转换写入私有 catalog，按 doctor 返回的 `direct-ime` 或 `pinyin-fallback` 选择文本实现，再用 `compose` 在编译期展开。连续语义动作仍由 agent-device 原生 `.ad` 承载；Airtest 图片步骤保持独立 runner，不伪装成 `.ad`。
9. 用 `preview` 校验资产哈希，查看顺序、effects、破坏性步骤和 `plan_sha256`。存在破坏性步骤时完成计划绑定授权。
10. 在 fresh session 中运行 `replay`。runner 先验证清单、资产、参数、设备和 viewport，再严格按 manifest 顺序调用 agent-device 与 Airtest；第一处分歧即停止。
11. 回读 `run-result.json` 和 evidence；只有最终状态为 `replay-verified` 才通过。真实设备验收按 `references/real-device-acceptance.md` 执行。

平台矩阵：Windows x64 支持 agent-device，并有可安装的 Airtest 移动 lock 和首版真实验收目标；macOS arm64、Linux x64 保留 agent-device 源码结构支持，但当前图片 runtime lock 未验证；USB ADB、无线 ADB、模拟器均进入合同，只有产生真实证据的 transport 才标记已验收；iOS 返回 `unsupported_platform`。

设备农场扩展不改变现有动作和证据协议。未来 DeviceFarmer/STF 或同类 provider 只负责“申请设备 → 返回远程 ADB 标识 → 执行现有流程 → 释放设备”，不能替代 agent-device 或 Airtest，也不能把 provider 的租约或账号偷偷写入 bundle。

## 人工确认门

动态探索中的只读观察和可逆导航无需工作流额外确认，但仍受用户任务范围约束。是否固化、仅生成还是生成并回放，应在清理路径后由用户确认一次。

任何卸载应用、清除应用数据、删除用户数据、恢复出厂设置、删除账号/配置/凭据或同等不可逆动作都必须在 preview 中列出，并取得绑定到精确小写 `plan_sha256` 的明确授权。缺少、大小写错误、过期或不同摘要时返回 `destructive_authorization_required`，不得执行任何设备输入。

破坏性 agent-device 动作及其恢复脚本不得引用运行时参数；否则同一计划摘要可能通过替换参数改变删除目标。编译和回放旧 bundle 都必须在设备输入前返回 `destructive_parameterization_unsupported`。需要删除不同目标时，应生成目标写死在脚本中的新组件或新 bundle，重新 preview 并重新授权。

真实验收只允许非生产目标和无害动作；不得把“测试工作流”解释为授权卸载、清除数据、恢复出厂、账户变更、支付、发送消息或截取个人信息。

## 失败恢复

- `environment_missing`：保持环境不变，按能力契约报告缺失版本、路径和允许的安装方式。
- `device_not_found|device_offline`：重新做只读枚举；不要向旧 serial 发送输入。
- `ambiguous_device`：列出脱敏候选并要求明确 serial；不得默认第一个。
- `target_identity_changed|viewport_changed`：停止，重新观察和生成模板；不得缩放旧坐标。
- `selector_not_found|selector_ambiguous`：获取新快照，优先改进语义 selector；不要直接降级图片或坐标。
- `test_ime_required`：在输入发生前停止；若 doctor 为 `direct-ime`，在对应会话的 `open` 中加入 `--test-ime`，若为 `pinyin-fallback`，重新生成“拼音输入 → 唯一中文候选 → 最终中文验证”的脚本。
- `image_below_threshold|image_not_unique`：保存只读证据并停止；图片失败不得静默回退。
- `postcondition_failed|replay_diverged`：结果未知时只对账，不重做具有 create/submit/send/delete effect 的步骤。
- `component_precondition_failed`：恢复不存在或执行一次后仍未到达组件入口；停止并重新探索状态转换边界。
- `component_recovery_failed|component_postcondition_failed`：保留组件分支证据并停止，不循环恢复或重放主要动作。
- `component_parameter_invalid|component_variant_unavailable`：在设备输入前修正参数映射，或为 doctor 选择的文本模式提供明确实现；不得静默换用不兼容脚本。
- `bundle_integrity_mismatch`：拒绝执行，重新审阅源资产后生成新 bundle。
- `destructive_authorization_required`：重新 preview 并让用户确认当前摘要。
- `unsupported_platform`：保持适配边界，不尝试未经声明的 iOS 或其它驱动。

Hub 必须在第一次 agent-device replay 前创建并传入本次运行专用的显式 `state-dir`，后续 selector 断言和 `finally` 关闭都复用同一路径；不能只在成功响应后才发现 state-dir。所有异常都应在 `finally` 中关闭 agent-device/Airtest 会话；失败截图和已完成步骤证据保留，但未知上游文本需限长并脱敏。

## 重跑、幂等与覆盖策略

doctor、devices、capture、locate-image 和 preview 可在环境不变时重跑；每次仍须读取当前设备和新鲜画面。compile 始终分配新的 `-vNNN` bundle，不覆盖旧版本。click-image 和 replay 会产生设备输入及新证据，不能复用旧匹配坐标或覆盖既有 evidence。

语义选择器失效、模板变化、设备 identity/viewport 变化、断言变化或副作用分类变化必须重新探索、编译和 preview。包含 create、submit、send 或 delete 的结果未知步骤不得自动重放；先核对实际业务状态。`generated-unverified` 不能通过复制旧结果升级，必须由本 bundle 的独立执行产生 `replay-verified`。

组件描述、脚本、参数、effect、状态断言或恢复任一变化都必须创建新的 `vNNN`，不得覆盖旧版本。更新场景中的精确版本引用后重新执行 `compose`、`preview` 和两个不同参数的 fresh-session 回放；旧 bundle 与旧 `plan_sha256` 不能为新版本背书。

## 验收标准

- doctor 正确报告 Node、agent-device、IME Helper、`text_input_mode`、Airtest profile 和 lock 状态；devices 唯一绑定目标或稳定返回 `ambiguous_device`。
- 一个无害 selector 流程在 Android 真机或模拟器上成功，动作后状态经过新快照验证。
- 一个 Airtest 图片流程使用设备新鲜截图、唯一匹配、阈值和前后证据，不点击主机模拟器窗口。
- 混合 bundle 中 `.ad`、模板和断言均为相对路径且哈希有效；修改资产会在设备输入前失败。
- 破坏性流程没有精确 `plan_sha256` 时不产生输入，计划变化会使旧授权失效。
- 独立 fresh session 回放完成并产生 `replay-verified`；图片失败没有坐标兜底。
- 同一私有场景以两个不同查询参数回放同一个 bundle；组件 ID、精确版本、描述哈希和计划哈希保持一致，步骤结果为 `component-verified` 或 `already-complete`。
- 自动测试、Hub 仓库校验和 `git diff --check` 通过，公开文件没有真实 serial、IP、账号、截图、日志或本机路径。
- macOS、Linux、无线 ADB、iOS 和设备农场只按实际证据声明状态，不因源码结构存在而虚报实机支持。

## 清理方式

默认只报告 `workspace/workflows/mobile-device-automation/` 下可重建的临时截图、候选模板、日志和证据，以及 `workflows/mobile-device-automation/outputs/` 下用户点名的 bundle；不自动删除。清理前给出规范化绝对路径、类型和大小，并遵守 Hub clean 对 outputs 的精确列表与二次确认。

停止或清理时先关闭 agent-device/Airtest 会话和设备农场租约（若未来 provider 存在）。不得卸载目标应用、清除设备数据、删除用户文件、移除 ADB 授权、修改无线调试、删除其它工作流 runtime，或把私有运行时内容提交到 Git。
