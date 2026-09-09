---
name: desktop-client-automation
description: Use when an Agent must complete a natural-language Windows or macOS desktop task through Cua Driver, refine the successful path, and optionally generate a user-confirmed Windows Airtest script for deterministic replay.
compatibility: Agent Workflow Hub spec 1.0; Cua Driver MCP provides Windows/macOS exploration, while Airtest 1.4.3 replay remains Windows x64 only.
metadata:
  spec-version: "1.0"
  workflow-version: "0.1.0"
  display-name: "Desktop Client Automation"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '["mcp.cua-driver"]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"doctor":"python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py doctor --host <HOST> --mode <MODE> --cua-mcp <STATUS> --native-computer-use <STATUS>","capture":"python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py capture [--window-title-regex <REGEX> OR --desktop --display-id primary] --output <ABSOLUTE_PNG> [--x N --y N --width N --height N]","locate-image":"python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py locate-image [--window-title-regex <REGEX> OR --desktop --display-id primary] --template <ABSOLUTE_IMAGE> [--threshold 0.8 --timeout-seconds 10]","click-image":"python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py click-image [--window-title-regex <REGEX> OR --desktop --display-id primary] --template <ABSOLUTE_IMAGE> --evidence-dir <ABSOLUTE_DIRECTORY> --action-id <LOWERCASE_ID> [--threshold 0.8 --timeout-seconds 10]","compile":"python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py compile --request <ABSOLUTE_JSON> --output-root <ABSOLUTE_DIRECTORY>","replay":"python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py replay --bundle <ABSOLUTE_AIR_DIRECTORY>"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# 客户端自动化

## 用途与触发条件

当用户希望 Agent 用自然语言完成一个 Windows 或 macOS 桌面客户端任务，并在成功后整理有效路径时使用。动态探索首选 Cua Driver MCP；Windows 上还可以把用户确认的路径固化为可编辑、可回放的 Airtest 脚本。工作流允许在多个应用和相关系统对话框之间切换，并拥有客户端选择、探索轨迹、图片模板、参数、脚本生成、回放和视觉断言的完整领域能力。

Codex、OpenClaw、Claude Code、Hermes 或 OpenCode 只有在当前任务实际列出 Cua MCP 工具时才可使用 Cua 路径；本地存在 `cua-driver` 二进制或配置文件中出现名称都不构成 MCP 已连接的证据。Codex 原生 Computer Use 保留为兼容路径。生成的 Airtest 脚本与探索宿主无关，但第一阶段只支持 Windows x64 桌面回放。

## 非目标

- 不依赖或复制 `automated-test-lifecycle`、Git、Jenkins、禅道或测试报告的状态机和 Gate；复合工作流以后可调用本工作流。
- 不把 Cua Agent 框架或独立视觉模型引入 Hub；当前 Agent 负责规划，Cua Driver 只提供桌面控制。
- 不把 macOS 上的 Cua 探索声明成 Airtest 回放能力；第一阶段不支持 Airtest 桌面回放。
- 不生成任意条件图、无限循环、通用事件录像或跨机器无条件可用的脚本。
- 不自动输入登录凭据、验证码、Token、Cookie，不读取密码管理器，不自动处理身份验证界面。
- 不支持通用图片、富文本、Excel对象或二进制剪贴板的保存与恢复。

## 输入

探索输入是用户的自然语言任务、明确目标应用及允许的外部影响。Agent 先建立应用清单；每个 `application` 声明用于生命周期和进程精确绑定的绝对 `executable`、窗口标题正则和 `restart|reuse|attach-only` 生命周期。Windows 打包应用等需要通过单独入口启动时，可额外声明绝对 `launch_executable`；可见窗口由宿主进程承载时，可声明绝对 `window_process_executable`。两者未声明时均默认使用 `executable`。相关 Windows 文件选择框等声明为 `target_kind=system-dialog` 且固定 `attach-only`，不启动、重启或强杀。用户明确要求操作可见桌面时，单独声明 `target_kind=desktop`、`lifecycle=attach-only`、`display_id=primary`，并省略可执行文件和窗口标题；桌面目标不等同于 Explorer 窗口。

固化输入是绝对路径 JSON，结构见 `references/automation-request.schema.json`。它只接收经过清理并由用户确认的有效步骤；误点、回退、重复等待、试探窗口切换不得进入。图片步骤引用已存在的绝对 PNG/JPEG；动态普通文本使用具名参数，固定文本可直接保留，敏感文本只能成为 `manual-step`，不得带默认值。

每一步声明副作用等级。整条路径按主流程和可选步骤中的最高等级归类；任一步未知则总路径未知。图片锚点步骤与明确坐标型步骤是两种独立类型，图片失败不得静默回退坐标。

## 输出与命名规则

私有探索截图和候选轨迹写入 `workspace/workflows/desktop-client-automation/`。用户固化的结果默认写入 `workflows/desktop-client-automation/outputs/<name>-vNNN.air/` 或用户明确指定的绝对输出目录。每次生成新版本，不覆盖已有脚本或模板。

`.air` 包包含可编辑 Python 入口、本地回放模块、`manifest.json`、`assets/` 图片模板和非敏感 `parameters.example.json`。它不依赖 Codex 或已安装的 Hub 包即可由准备好的 Airtest 私有解释器回放。生成状态为 `generated-unverified`；独立回放断言通过后结果为 `replay-verified`，失败为 `replay-failed` 并尽力保存失败截图。没有实际回放证据时不得声称脚本已验证或可用。

`.air` 是用户可编辑的可执行自动化产物，不是可从不可信来源直接运行的数据文件。`replay` 只接受本工作流刚生成、或用户已审阅并明确选择的 bundle；下载所得、来源不明或在确认后发生修改的 bundle 必须先回到输入合约重新审阅和编译。保留可编辑与独立回放语义意味着 Hub 不把普通本地哈希误称为防篡改签名。

## 依赖和运行前检查

### 运行入口与部署验证

先从当前 Agent 工具清单判断 Cua MCP 是否真实可调用，并把结果作为 `doctor --cua-mcp` 的 `available`、`unavailable` 或 `unknown` 参数。Codex 原生 surface 状态通过 `--native-computer-use` 传入；旧 `--computer-use` 参数仍是兼容别名。Doctor 分为 `explore|replay|full`：探索检查 Cua 或 Codex 兼容 surface，Windows 回放检查 Airtest，macOS 请求回放返回 `needs-replay-backend`。Doctor 只检测环境，不列窗口、截图或操作应用。

### Cua Driver MCP 动态探索

动态探索首选 Cua Driver MCP。Agent 直接调用 Cua MCP，Hub 不封装 Cua 点击，也不在 Python 中复制 Cua 的动作协议。

1. 先调用 `list_windows`，要求用户目标唯一解析为精确的 `pid + window_id`。
2. 每个动作前调用 `get_window_state`，同时检查结构化 Accessibility/UIA 元素和该窗口截图。
3. 元素动作使用同一新鲜快照中的 `snapshot_id + element_token`；新的状态读取会替换索引映射，不得复用旧 `element_index`。Cua Driver 0.23.2 的 Windows 实机验收要求动作调用在 `element_token` 存在也显式传入 `pid + window_id`；如果遗漏字段被驱动拒绝，先获取新快照再补齐字段，不能假定原动作已经送达。
4. Accessibility/UIA 可信时优先元素动作。树为空、不完整或目标位于 WebView/Canvas 时，只能依据同一次窗口截图使用像素动作；窗口归属、截图尺寸、缩放或坐标系无法证明时停止。
5. 默认使用后台投递。只有 Cua 返回结构化拒绝或已经观察到动作未送达，才对当前动作切换前台投递，不把前台模式设成全局默认。
6. 动作后再次调用 `get_window_state` 验证业务结果；结果未知时只对账，不重复有副作用动作。动作返回 `unverifiable` 或建议升级前台，但新鲜后置状态已确认业务结果时不得前台重试。嵌入式 WebView 的 `verify_state` 可能返回 `unknown_reason=untrusted_source`；该值既不是成功也不是失败，必须把同次窗口截图、URL 或文档值、输入框值和结果锚点一起保存并如实记录判断依据。

显式桌面目标不通过 `list_windows` 或窗口 UIA 绑定。Agent 使用同一会话中的 `get_desktop_state` 获取主显示器新鲜截图，再把该截图中观察到的坐标发送给目标 `{kind:"desktop",display_id:"primary"}`。Cua Driver 0.23.2 的桌面双击使用一次 `click` 且传入 `count=2`；不得把两次独立点击当成双击，也不得复用旧截图坐标。桌面动作只在桌面确实可见且未被其他窗口遮挡时执行，不主动发送 `Win+D`。这一动态桌面路径在 Windows 和 macOS 均可用，仍以当前 Cua 工具实际暴露的能力为准。

### Codex 原生 Computer Use 兼容与排障（非永久强制条件）

截至 2026-09-06，若 Cua MCP 不可用但需要使用 Codex 原生兼容路径，且 Codex 桌面任务没有提供 Windows 原生应用 surface，优先按 [OpenAI Computer Use 官方说明](https://learn.chatgpt.com/zh-Hans/docs/computer-use) 检查以下当前产品路径：

1. 在 ChatGPT 桌面应用中切换到 Codex，打开“插件 > Computer Use”，按界面提示安装或启用插件，并打开 Computer Use server 与 Skill。
2. 在任务输入框的 `@` 菜单中实际选择“电脑”（英文界面为 `@Computer`；当前中文界面的结构化提及可显示为 `[@电脑](plugin://computer-use@openai-bundled)`），再提交任务。仅键入普通文本“@电脑”不应被当作插件已经载入的证据。
3. 在当前任务或按产品提示开启的新任务中重新读取工具与应用清单，确认能够列出 Windows 原生应用，而不是只存在浏览器控制能力。

这是当前版本的推荐启用与排障路径，不是工作流的永久调用语法，也不要求每次运行都重新安装插件或必须重复使用 `@电脑`。Cua MCP 已可用时不要求安装或提及 `@电脑`。Codex 后续版本可能自动载入该能力、改变入口或调整提及方式；兼容路径始终以当前任务实际暴露的 Windows 原生应用 surface 为准。surface 已可用时直接继续，不因缺少上述操作记录而阻塞；surface 不可用时先向用户给出这一路径，不要在载入条件没有变化时反复分叉或创建任务。

### Airtest 原生桌面与条件式窗口识图（Windows）

正常探索始终优先使用 Cua MCP；Codex 原生 Computer Use 处于兼容路径。两者能够读取和操作目标控件时，不要求启用 Airtest 单步识图。窗口目标只有在已观察到 WebView/UIA/AX 无法提供可信元素动作，或宿主截图/坐标能力明确报错，例如 `coordinate input geometry is unavailable`、`SetIsBorderRequired failed: 不支持此接口 (0x80004002)`，并且当前是 Windows，才使用条件式 Airtest 识图。显式桌面目标和已有模板的固定验收流程可直接使用 Airtest：`Windows:///` 是无 HWND 的原生 Windows 桌面设备，不是模拟兜底。未来 Cua、Codex 或 Computer Use 修复相关行为后，窗口动态探索可继续使用首选路径；这些错误字符串不是永久触发协议。

1. 窗口模式用窗口标题正则绑定唯一可见的目标顶层窗口并将其置前；桌面模式用 `--desktop --display-id primary` 明确绑定主显示器桌面。桌面必须已由用户显示在前台；被应用遮挡时返回 `desktop-not-foreground`，不得发送 `Win+D` 改变用户布局。桌面模式不要求 UIA，也不依赖 Explorer ListView 语义定位。
2. 用 `capture` 保存所选目标的绝对 PNG，再从该图裁剪小而稳定的模板。窗口模板只能来自所选窗口截图，桌面模板只能来自显式桌面截图；不得跨目标复用截图，也不得从黑屏或被遮挡画面制作模板。模板应包含足够辨识度，避开头像、计数、时间、轮播图和其他动态内容。
3. 先运行只读的 `locate-image` 验证模板和阈值。它每次绑定窗口并执行一次新的 Airtest 匹配，只报告本次位置；不得把位置保存后给后续命令复用。
4. 定位通过后运行 `click-image`。它先检查全部输入和证据文件冲突，保存 `<action-id>-before.png`，重新匹配当前画面，只点击新匹配中心一次，等待界面稳定，再保存 `<action-id>-after.png`。
5. `not-found` 时只保留 before 证据、不点击、不生成 after。图片识别失败不得静默回退到历史坐标、猜测坐标或 Computer Use 坐标点击；需要坐标动作时必须由工作流显式声明 `click-coordinate`。
6. 登录、验证码、凭据和其他身份验证仍交给用户手工完成；识图兜底不扩大原任务授权，也不绕过 Computer Use 或宿主的确认策略。

桌面回放每次都在主显示器的新鲜截图中要求唯一匹配，不保存探索坐标。`click-coordinate` 的值是主显示器局部坐标，运行时会换算为 Airtest 虚拟桌面图像坐标后再输入；桌面右键使用 `touch(..., right_click=True)`，图片拖拽使用 `swipe`，均不发送未经换算的 pywinauto 坐标。最终断言也在新鲜主显示器截图中重新唯一匹配；应用打开后可以遮住 Shell，因此最终断言不要求桌面 Shell 继续位于前台。

一次性命令示例：

```powershell
python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py locate-image --window-title-regex 'BambuStudio' --template 'C:\absolute\target.png' --threshold 0.8 --timeout-seconds 10
python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py click-image --window-title-regex 'BambuStudio' --template 'C:\absolute\target.png' --evidence-dir 'C:\absolute\evidence' --action-id 'open-favorite' --threshold 0.8 --timeout-seconds 10
python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py capture --desktop --display-id primary --output 'C:\absolute\desktop.png'
python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py locate-image --desktop --display-id primary --template 'C:\absolute\desktop-icon.png' --threshold 0.85 --timeout-seconds 10
python <HUB_ROOT>/workflows/desktop-client-automation/scripts/desktop_client_automation.py click-image --desktop --display-id primary --template 'C:\absolute\desktop-icon.png' --evidence-dir 'C:\absolute\evidence' --action-id 'select-desktop-icon' --threshold 0.85 --timeout-seconds 10
```

Airtest 使用 `python.airtest` 能力契约和 `workspace/workflows/desktop-client-automation/runtime/` 下的工作流私有 CPython 3.11 运行时。先以 `uv venv` 创建该目录，再按完整哈希锁安装；启动脚本发现该解释器后会整进程切换过去，绝不把 3.11 的 `site-packages` 注入系统 Python。AirtestIDE 不是依赖；系统 Python 3.13 与 Airtest 1.4.3 的锁定 NumPy/OpenCV 组合不兼容时不得强装。只复制 Skill 目录不包含 Hub 编译器和私有运行时。

## 系统修改与权限影响

探索阶段通过 Cua MCP 或已验证的宿主原生 Computer Use 操作用户点名的 Windows/macOS 应用。截图、轨迹和脚本只写工作流 workspace 或明确输出目录；不安装目标客户端，不修改系统设置、网络、安全策略或宿主 MCP 配置。Cua Driver 的安装、服务启动、macOS TCC 授权和宿主 MCP 注册分别遵循 `mcp.cua-driver` 能力契约，不能从本工作流的执行授权中推导。

`restart` 先正常关闭唯一精确绑定实例。出现保存或关闭确认时暂停给用户处理，不自动选择丢弃；无新确认窗口且超时后，只有该应用已声明 `force_terminate=true` 才强制结束精确可执行路径匹配的唯一进程。多实例无法唯一绑定时停止，不批量结束同名进程。

文本剪贴板在回放期间临时使用并尽力恢复原文本；异常进程终止时不承诺恢复。文件只通过明确路径或文件选择框传递。

## 执行步骤

1. 从当前工具清单检查 Cua MCP 与 Codex 原生 surface，再按目标运行 `doctor --mode explore|replay|full`；任何所需能力不满足都不宣称可执行。Cua 二进制存在不等于 MCP 已连接。
2. 根据用户任务建立跨应用清单、各自生命周期、窗口选择和初始页面。身份验证页由用户手工完成，Agent 重新绑定后继续。
3. Cua 窗口目标每次按 `list_windows → get_window_state → 一个动作 → get_window_state` 执行；桌面目标按 `get_desktop_state → 一个桌面动作 → get_desktop_state` 执行。兼容的 Computer Use 也必须依据最新目标状态。执行动作前保存可固化步骤所需的目标图片或区域，记录应用别名、动作参数和前后状态。
4. 观察结果未知时先只读刷新和对账，不重复触发可能有副作用的动作。有限重试只用于识图等待、窗口激活、无副作用导航和已证明未提交的输入。
5. Agent 观察到用户目标后，选择目标窗口中的稳定区域作为最终断言，记录必须出现的锚点、阈值、超时和稳定时长；动态区域不进入模板。
6. 从完整探索轨迹删除误点、回退和重复动作，生成有效步骤摘要、应用生命周期、动态参数、最终断言、总副作用等级和回放影响。
7. 向用户一次展示并询问：不固化、仅生成、生成并回放。该确认只决定本次是否生成产物及是否立即验证回放，不追溯授权已经发生的探索操作，也不永久禁止用户以后手工回放已生成脚本。
8. 用户选择生成 Windows Airtest 产物时写入确认后的 JSON 并运行 `compile`。图片步骤失败即停止；只有显式 `click-coordinate` 使用坐标，窗口目标采用窗口相对坐标，桌面目标采用主显示器局部坐标。macOS 可由 Cua 动态操作桌面，但请求生成或回放 Airtest 产物时返回 `needs-replay-backend`。
9. 对普通查询和可证明幂等流程，用户选择固化后可自动回放。创建、提交、发送、删除或未知流程按用户同一次选择执行“仅生成”或“生成并回放”。
10. 回放时按应用策略关闭、启动或绑定应用，导航到初始页面，执行线性主流程、可选步骤和有限重试，最后要求用户确认的最终锚点持续可见。输出实际状态和证据。

## Bambu Studio 真实回放示例

当前工作区已经固化一条真实验收过的 Airtest 流程，入口请求位于
`workspace/workflows/desktop-client-automation/bambu-studio/bambu-studio-favorites-search-request.json`。
它使用 `reuse` 生命周期，先点击“模型库”归位，再点击“在线模型”；如果 Bambu Studio 恢复了上一次的在线模型子页面，
就用可选的“返回”图片步骤回到在线模型首页，然后进入收藏夹并搜索 `BB枪`。这样不会因为正常关闭耗时或未保存的 Bambu
Studio 状态而强制终止应用。

编译并回放：

```powershell
python <HUB_ROOT>\workflows\desktop-client-automation\scripts\desktop_client_automation.py compile `
  --request <HUB_ROOT>\workspace\workflows\desktop-client-automation\bambu-studio\bambu-studio-favorites-search-request.json `
  --output-root <HUB_ROOT>\workspace\workflows\desktop-client-automation\bambu-studio

python <HUB_ROOT>\workflows\desktop-client-automation\scripts\desktop_client_automation.py replay `
  --bundle <生成的 bambu-studio-favorites-search-bbgun-vNNN.air 绝对路径>
```

回放只使用请求中引用的图片模板；图片匹配失败会停止并保留失败截图，不会改用猜测坐标。成功时 bundle 的
`run-result.json` 为 `replay-verified`，并生成 `replay-success.png`。

## 人工确认门

初次探索遵循用户原始任务授权、Cua 权限模式和宿主自身的强制确认策略；本工作流不复制或削弱这些策略，也不添加每次点击确认。登录和敏感输入固定交给用户手工完成。

本工作流自己的唯一业务确认发生在有效路径整理完成后，一次给出“不固化、仅生成、生成并回放”。确认摘要必须包含全部应用及生命周期、有效步骤、动态参数、最终断言、总副作用等级，以及回放是否会重复创建、提交、发送或删除。摘要发生实质变化时才重新确认。

## 失败恢复

Cua 未安装返回 `needs-cua-driver`，二进制存在但当前宿主未暴露 MCP 返回 `needs-cua-mcp`，工具清单未知返回 `needs-surface-probe`；Codex 原生 Computer Use 可作为明确标注的兼容路径。Windows Airtest 依赖缺失返回 `needs-airtest`，macOS 请求回放返回 `needs-replay-backend`。窗口缺失或多实例歧义停止并报告候选；登录页和保存确认返回 `manual-step`；图片缺失停止并保存当前截图，不回退坐标。

动作结果未知时刷新窗口状态并检查业务结果，不能自动重做副作用动作。生成失败删除本次未完成的新版本目录，不影响旧版本。回放失败保留脚本、日志和失败截图，状态为 `replay-failed`；修正后生成新版本，不覆盖失败版本。

## 重跑、幂等与覆盖策略

探索是否可重跑由用户任务和宿主确认策略决定。回放总副作用等级取所有可能步骤的最高值；任一步未知则不得自动视为幂等。副作用步骤只有明确证明尚未发生时才能再次执行，结果未知时只对账。

编译永远创建递增 `vNNN` 目录。参数变化不改写脚本；非敏感参数通过运行时 JSON 提供。改变应用、步骤、图片模板、断言或副作用分类必须生成新版本并重新获得一次固化选择。

## 验收标准

- Cua MCP 在当前任务真实可调用时，Codex、OpenClaw、Claude Code、Hermes 或 OpenCode 可进入 Windows/macOS 探索；Codex 原生 surface 仅是兼容路径。
- 元素动作绑定新鲜 `snapshot_id + element_token`，像素动作绑定同一次窗口截图；结构化拒绝后才按当前动作升级前台投递。
- macOS 第一阶段不支持 Airtest 桌面回放，必须如实返回 `needs-replay-backend`。
- 能记录并清理跨应用有效路径，普通文本参数化，敏感输入不落盘。
- `restart/reuse/attach-only` 与 `application/system-dialog` 语义分离；强杀只作用于唯一精确实例。
- `desktop` 目标只绑定 `display_id=primary`，前台未显示或匹配不唯一时停止，不发送 `Win+D`，且每次回放重新定位而不使用探索坐标。
- 图片锚点失败不静默点击坐标；坐标步骤必须显式。
- 重试不重复结果未知或有副作用的动作。
- 用户一次确认覆盖固化及回放选择；未回放脚本保持 `generated-unverified`。
- 生成 `.air` 包可编辑，独立回放后最终视觉断言稳定通过并产生 `replay-verified` 证据。
- 合约、编译器和安全边界测试通过；当前环境缺少原生 surface 时如实保留真实端到端验证缺口。

## 清理方式

默认只列出工作流 staging 中已明确标为临时的截图和候选轨迹，不自动删除。删除 staging 或输出 `.air` 包前必须给出精确绝对路径和大小，并按 Hub 输出清理规则获得确认。不得删除目标应用、用户文件、运行参数、其他工作流 runtime 或既有脚本版本。
