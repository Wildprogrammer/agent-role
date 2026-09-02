---
spec_version: "1.0"
id: "mcp.blender"
type: "mcp"
locked_version: "git:6e99eb5a442b83766a5796975ec7bb5bfc791341"
version_requirement: ">=1.6.4"
recommended_version: "1.6.4"
official_source: "https://github.com/ahujasid/blender-mcp"
official_docs: "https://github.com/ahujasid/blender-mcp#readme"
license: "MIT"
last_verified: "2026-07-10"
integrity: {method: "git-commit", value: "6e99eb5a442b83766a5796975ec7bb5bfc791341"}
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x64: "unverified-until-adapter-test", arm64: "unverified-until-adapter-test"}
  runtimes: ["Blender >= 3.0", "Python >= 3.10", "uv/uvx"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "uvx --from blender-mcp blender-mcp --help"}
permissions: ["local TCP connection", "arbitrary Blender Python after explicit approval"]
network:
  required_for_install: true
  required_for_core_use: false
  default_policy: "DISABLE_TELEMETRY=true"
data_access: ["Blender scene", "optional online asset services after opt-in"]
installation:
  policy: "agent-managed"
  scope: "workspace-shared"
  methods: ["existing", "git"]
workspace_source: "workspace/shared/mcp/blender-mcp"
automation_status: "conditional"
---
# Blender MCP

## Purpose

Control Blender through MCP in small, reversible steps after the user approves
the host MCP configuration and any Blender Python to execute.

## Install

The source is an `agent-managed` shared workspace dependency. When no matching
source exists, the Agent may clone the official repository to
`workspace/shared/mcp/blender-mcp` at the locked commit and verify `git HEAD`.
The Codex MCP host mapping remains user-managed: the Agent must not register or
edit host configuration without a separate explicit approval.

Use the checked-out source at `workspace/shared/mcp/blender-mcp` when preparing
the workflow. Pin the source to the recorded commit; never execute an unpinned
installer copied from a search result.

## Security

Set `DISABLE_TELEMETRY=true` by default. Show any Python code, affected files,
requested network services, and local TCP connection details before execution.

## Success

The host lists Blender MCP tools, Blender connects on the configured local port,
and a read-only scene inspection succeeds without modifying the scene.

## Known limitations

This record verifies the upstream repository identity, not local host readiness.
Each host must still prove MCP discovery and Blender connectivity.

## Alternatives

Use manual Blender instructions or direct Blender scripting only after explicit
approval when MCP is unavailable.

## Rollback

Remove the host MCP entry and Blender add-on. Preserve user `.blend` files and
workflow outputs.

## 能力用途和非目标

用途是让 Agent 通过 MCP 检查和操作 Blender；非目标是静默执行任意 Python 或控制实体打印机。

## 官方获取与文档

官方来源和文档均为 `https://github.com/ahujasid/blender-mcp`，锁定提交为 frontmatter 中的 git SHA。

## 系统、架构、运行时和硬件支持

系统支持以 upstream 文档和本地 smoke 为准；未跑适配器测试前所有架构均不得视为 verified。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes 和 OpenCode 均保持 `unverified`，直到各自 smoke evidence 通过。

## 只读检测

只读检测命令是 `uvx --from blender-mcp blender-mcp --help`，不得修改 Blender 配置。

## 各系统安装

按 upstream 文档安装，并记录 OS、架构、uv/uvx、Python 和 Blender 版本。

## 调用示例和成功判据

成功判据是宿主列出 MCP tools、Blender 建立本地连接、只读场景查询成功。

## 权限、网络、数据和遥测

安装需要网络；核心使用默认离线。遥测默认关闭；场景数据和资产服务必须显式授权。

## 卸载或回滚

移除 MCP 配置和 add-on，保留用户文件。

## 已知限制

未验证宿主不得宣称支持。

## 替代能力

可退回手工 Blender 操作或经批准的直接脚本。
