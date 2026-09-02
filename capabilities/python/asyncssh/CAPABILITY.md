---
spec_version: "1.0"
id: "python.asyncssh"
type: "python"
locked_version: "2.24.0"
version_requirement: ">=2.24.0"
recommended_version: "2.24.0"
official_source: "https://pypi.org/project/asyncssh/2.24.0/"
official_docs: "https://asyncssh.readthedocs.io/en/stable/"
license: "EPL-2.0 OR GPL-2.0-or-later"
last_verified: "2026-09-01"
integrity:
  method: "sha256"
  value: "9abd46300adcb6d4b73269b34c53cd0d17a138b9a22b5b38008ce7d5808734b7"
  locked_version: "2.24.0"
  source: "https://files.pythonhosted.org/packages/3e/29/908ce0ca5e8cae76662e354a0f08df552d6d221844748b9e5ca06051cc44/asyncssh-2.24.0-py3-none-any.whl"
systems:
  os: {windows: "verified-wheel", macos: "documented", linux: "documented"}
  arch: {x64: "verified-wheel", arm64: "documented"}
  runtimes: ["CPython 3.13 Windows x64 for agent-managed install", "Python >= 3.10 source compatibility"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "python -c \"import asyncssh; print(asyncssh.__version__)\""}
permissions: ["open configured SSH connections", "read and write configured remote paths", "write workflow-private host-key and result state"]
network: {required_for_install: true, required_for_core_use: true}
data_access: ["configured SSH targets", "remote command input and output", "configured file transfer paths", "workflow-private known-hosts state"]
installation: {policy: "agent-managed", scope: "workspace-workflow", methods: ["existing", "uv"]}
automation_status: "conditional"
---

# AsyncSSH

## 能力用途和非目标

用于 `ssh-operations` 工作流的 SSHv2 连接、命令执行、SFTP/SCP、跳板连接和端口转发。它不替代远端系统权限管理，不绕过 SSH 服务端认证或授权，也不把 Windows 权限提升伪装成 Unix `sudo`。

## 官方获取与文档

Windows x64 自动准备只使用 frontmatter 锁定的 AsyncSSH 2.24.0 官方通用 wheel，以及 `workflows/ssh-operations/references/runtime-windows-py313.lock` 中完整的哈希依赖集。不得使用 latest、第三方镜像或开发分支替代。

## 系统、架构、运行时和硬件支持

已核对 CPython 3.13、Windows x64 官方 wheel。AsyncSSH 2.24.0 要求 Python 3.10 及以上。macOS、Linux 和 arm64 第一版提供配置与行为支持，但必须在对应真实设备验证后才能标记为实机验证通过。

## 五种宿主兼容矩阵

五种宿主都必须在各自宿主内运行 CLI doctor、临时 SSH 服务端行为测试和至少一次已授权真实目标 smoke 后，才可标记为完整可执行。Python 包可导入不等于真实远端系统已经验证。

## 只读检测

执行隔离 Python import metadata probe，读取 AsyncSSH 安装版本。检测不建立 SSH 连接、不写 known-hosts、不安装包。

## 各系统安装

安装前报告专用运行时绝对路径、锁定版本、哈希锁、PyPI 网络访问、预计写入和删除该专用运行时的回滚方式。得到一次普通环境准备确认后，在工作流私有虚拟环境中执行：

```powershell
uv pip install --python <WORKFLOW_PYTHON> --require-hashes -r <HUB_ROOT>/workflows/ssh-operations/references/runtime-windows-py313.lock
```

其他受支持系统使用同一锁定版本，并按系统生成和审查独立哈希锁后安装；不得复用 Windows 平台解析结果冒充对应系统已验证。

## 调用示例和成功判据

成功判据：版本为 2.24.0；自动化测试能够启动临时 AsyncSSH 服务端，以密码和密钥连接，验证 TOFU 记录与变更拒绝、关联命令、文件传输和端口转发；真实 Windows/macOS/Linux 结果按实际已验证范围分别记录。

## 权限、网络、数据和遥测

运行时只连接配置文件明确指定的目标、跳板和转发端点。凭据不得进入命令行参数、结构化结果或日志。AsyncSSH 不提供工作流级遥测；工作流也不得把远端输出发送到未配置服务。

## 卸载或回滚

回滚时删除工作流私有 Python 运行时，或用同一包管理器卸载锁定依赖。删除运行时不得扩大到用户 SSH 密钥、业务文件、工作流配置或已记录的远端数据；known-hosts 清理必须另行明确指定目标。

## 已知限制

第一版不承诺交互式编辑器、长时间交互安装器、X11、TUN/TAP 或远端桌面。Windows 只使用当前 SSH 会话权限；需要管理员权限但当前权限不足时返回 `needs-elevation`。SCP 不声明断点续传，断点续传由 SFTP 路径实现。

## 替代能力

缺少 AsyncSSH 时返回 `needs_dependency` 和中文准备指导。第一版没有 Paramiko、系统 `ssh` 命令或 MCP 的静默生产替代路径。
