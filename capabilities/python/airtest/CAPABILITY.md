---
spec_version: "1.0"
id: "python.airtest"
type: "python"
locked_version: "1.4.3"
version_requirement: ">=1.4.3"
recommended_version: "1.4.3"
official_source: "https://pypi.org/project/airtest/1.4.3/"
official_docs: "https://airtest.readthedocs.io/en/latest/"
license: "Apache-2.0"
last_verified: "2026-09-05"
integrity:
  method: "sha256"
  value: "6208e83ca8d3618e32b8eee23b3e857a0077cd59accf158dd567a81df2a3b84c"
  locked_version: "1.4.3"
  source: "https://files.pythonhosted.org/packages/66/52/6affbca8e27276568013caab46c95177c77c4bf4ad43f4ad8388b70ba37e/airtest-1.4.3.tar.gz"
systems:
  os: {windows: "documented", macos: "unsupported-desktop-v1", linux: "unsupported-desktop-v1"}
  arch: {x64: "documented", arm64: "unverified"}
  runtimes: ["CPython 3.11 Windows x64 workflow-private runtime"]
hosts:
  codex: "conditional"
  openclaw: "conditional"
  claude-code: "conditional"
  hermes: "conditional"
  opencode: "conditional"
detect: {mode: "read-only", command: "python -c \"import importlib.metadata as m; print(m.version('airtest'))\""}
permissions: ["capture configured Windows application windows", "send mouse and keyboard input to configured Windows application windows", "start and stop exact configured application processes", "write workflow-private traces and generated Airtest bundles"]
network: {required_for_install: true, required_for_core_use: false}
data_access: ["configured Windows application screens", "user-approved text input and clipboard text", "workflow-private screenshots, traces, logs, and generated scripts"]
installation: {policy: "agent-managed", scope: "workspace-workflow", methods: ["existing", "uv"]}
automation_status: "conditional"
---

# Airtest

## 能力用途和非目标

用于 `desktop-client-automation` 在 Windows 上捕获窗口图片、把用户确认的有效桌面探索路径编译为 `.air` Python 脚本，并执行确定性回放和最终视觉断言。它不是自然语言决策模型，不替代 Cua Driver 或宿主原生 Computer Use，也不承诺 macOS 桌面自动化。

## 官方获取与文档

只使用 PyPI 官方 Airtest 1.4.3 源码分发包及工作流的完整哈希锁。不得使用 latest、未锁定镜像、AirtestIDE 安装包或源码分支代替。AirtestIDE 不是第一版运行依赖。

## 系统、架构、运行时和硬件支持

第一版仅面向 Windows x64。Airtest 1.4.3 锁定的 NumPy、OpenCV 和 Windows 依赖不适合作为当前系统 Python 3.13 的默认安装，因此使用工作流私有 CPython 3.11 运行时。macOS 只能运行部分 Airtest 工具，不代表已支持 macOS 桌面客户端目标。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes 和 OpenCode 都可以在 Windows 上通过工作流 CLI 调用 Airtest，前提是工作流私有运行时和目标桌面会话已通过检测。宿主兼容状态不代表本机运行时就绪；生成的 Airtest 脚本可脱离探索 Agent 回放。

## 只读检测

在实际工作流解释器中读取 `airtest`、`pywinauto`、`opencv-contrib-python`、`psutil` 和 `pywin32` 的包版本；不截屏、不操作窗口、不安装软件。Cua MCP 或宿主原生 Computer Use surface 必须由当前 Agent 工具清单单独核对，Python doctor 不得从本地二进制推断 MCP 已连接。

## 各系统安装

安装前报告私有运行时绝对路径、CPython 3.11、锁定版本、哈希锁、PyPI 网络访问、预计写入和删除该运行时的回滚方式，经一次普通依赖准备确认后执行：

```powershell
uv venv <HUB_ROOT>/workspace/workflows/desktop-client-automation/runtime --python 3.11
uv pip install --python <WORKFLOW_PYTHON> --require-hashes -r <HUB_ROOT>/workflows/desktop-client-automation/references/runtime-windows-py311.lock
```

`<WORKFLOW_PYTHON>` 是上述运行时中的 `Scripts/python.exe`。不得安装 AirtestIDE，不得写宿主 MCP 配置，不得把依赖装入不兼容的系统 Python 3.13，也不得把 3.11 的 `site-packages` 注入其他 Python 版本。

## 调用示例和成功判据

成功判据包括：doctor 返回 `ready`；能够唯一绑定一个测试窗口并保存截图；编译器从确认轨迹生成新版本 `.air` 包；回放能够按应用策略启动或绑定窗口、执行跨应用文本操作并通过用户确认的最终断言。

## 权限、网络、数据和遥测

核心运行不联网。窗口截图、输入、文本剪贴板和脚本参数只保存在工作流 staging 或输出包；敏感输入不写入轨迹、图片模板、参数和日志。Airtest 不替代目标应用权限，Cua 权限模式和宿主级强制确认策略继续生效。

## 卸载或回滚

回滚只删除 `workspace/workflows/desktop-client-automation/runtime/` 中的工作流私有运行时。生成的 `.air` 输出、用户配置、目标应用和其他工作流运行时不自动删除。

## 已知限制

第一版不支持 macOS/Linux 桌面目标、通用二进制剪贴板、任意流程图、无限循环或身份验证自动输入。DPI、主题、字体、语言和应用升级可能导致图片锚点漂移；生成但未回放的脚本只能标记为未验证。

## 替代能力

Airtest 缺失时返回 `needs-airtest`。动态探索使用 Cua Driver MCP，Codex 原生 Computer Use 只作为兼容路径；macOS 请求确定性回放时返回 `needs-replay-backend`。图片失败不静默回退到 PyAutoGUI、Windows-MCP、OculiX 或坐标点击实现。
