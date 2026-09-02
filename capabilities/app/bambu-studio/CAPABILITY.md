---
spec_version: "1.0"
id: "app.bambu-studio"
type: "app"
locked_version: "Bambu_Studio_win-v02.07.01.62-20260616174358.zip"
version_requirement: ">=2.7.1"
recommended_version: "2.7.1"
official_source: "https://github.com/bambulab/BambuStudio/releases/download/v02.07.01.62/Bambu_Studio_win-v02.07.01.62-20260616174358.zip"
official_docs: "https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage"
license: "AGPL-3.0"
last_verified: "2026-07-12"
integrity:
  method: "sha256"
  value: "790a6811ac480fa9cc6612ade6b9f74e3beec43a01be9340fbb6a04087226fdb"
  locked_version: "Bambu_Studio_win-v02.07.01.62-20260616174358.zip"
  source: "https://github.com/bambulab/BambuStudio/releases/tag/v02.07.01.62"
systems:
  os: {windows: "verified-artifact", macos: "documented", linux: "documented"}
  arch: {x64: "verified-artifact"}
  runtimes: ["Bambu Studio 2.7.1.62"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "bambu-studio --version"}
permissions: ["read model/profile", "write approved output directory"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["local model", "local printer/material profile", "local G-code 3MF"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "conditional"
---
# Bambu Studio

## Purpose

Provide the Bambu-specific slicer path for a confirmed printer, nozzle,
material, and profile. It is the only provider in this workflow that may
produce a Bambu G-code 3MF delivery.

## Install

This is a user-managed desktop capability. The Agent performs read-only
detection and provides guidance in the active user's language; the user
chooses and performs installation or package-manager setup.

Use the locked official release and verify its SHA-256 before use. A local
installation may be reused when its detected version satisfies the minimum.

## Security

Read only the confirmed model and profile. Write only to the approved workflow
output directory. Never upload, send, queue, or start a print. GUI automation
is not an approved fallback for a missing CLI capability.

## Success

Read-only version detection succeeds and an opt-in provider smoke proves the
exact CLI invocation, profile, and single-plate package structure required by
the active workflow.

## Known limitations

The Bambu CLI is conditional until the host-specific smoke evidence exists.
The documented CLI slicing/export flags do not establish a documented Cut,
create-plate, or move-piece API. Do not infer one from GUI behavior.

## Alternatives

PrusaSlicer and OrcaSlicer remain optional generic slicer providers. Their
outputs do not silently become Bambu G-code 3MF artifacts.

## Rollback

Remove only user-approved installer/cache state. Preserve workflow outputs and
verification evidence unless the user separately selects an exact path.

## 能力用途和非目标

用途是为用户确认的 Bambu 打印机提供切片和 Bambu G-code 3MF 校验入口。
非目标是拆件、创建打印盘、自动排盘、上传或启动打印。

## 官方获取与文档

只使用 frontmatter 中锁定的官方 release、SHA-256 和命令行文档。

## 系统、架构、运行时和硬件支持

当前锁定 Windows x64；运行时版本为 Bambu Studio 2.7.1.62；目标验证机型
由每次 Gate B 的 profile 明确记录。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes、OpenCode 均为 unverified，必须在目标
宿主上单独完成只读检测和 provider smoke。

## 只读检测

使用 frontmatter 中的 bambu-studio --version；如果程序不在 PATH，用户提供
绝对可执行文件路径后再检测，Agent 不启动 GUI。

## 各系统安装

安装由用户完成。Agent 只提供当前用户语言的安装指导，不运行安装器、包管理器
或下载命令。

## 调用示例和成功判据

只有用户确认 Bambu 输出格式并通过 provider smoke 后，才可调用已记录的 CLI。
每个确认盘必须独立验证 package、Metadata/plate_1.gcode、MD5 和 profile。

## 权限、网络、数据和遥测

安装可能需要网络；核心处理本地模型和 profile。只写批准的输出目录，默认
DISABLE_TELEMETRY=true，不上传或发送打印数据。

## 卸载或回滚

按用户指定的安装位置删除软件或私有缓存；保留 workflow outputs 和验证证据，
除非用户另行确认精确路径。

## 已知限制

Bambu CLI 仍需当前主机和版本的 smoke 证据；文档化的切片参数不等于存在
Cut、创建打印盘或移动对象的 CLI API。

## 替代能力

PrusaSlicer 和 OrcaSlicer 是可选通用切片器，不能替代 Bambu G-code 3MF
交付，也不能被当作拆件工具。
