---
spec_version: "1.0"
id: "app.prusaslicer"
type: "app"
locked_version: "version_2.9.6"
version_requirement: ">=2.9.6"
recommended_version: "2.9.6"
official_source: "https://github.com/prusa3d/PrusaSlicer/releases/tag/version_2.9.6"
official_docs: "https://help.prusa3d.com/article/install-prusaslicer_1903"
license: "AGPL-3.0"
last_verified: "2026-07-10"
integrity:
  method: "sha256"
  value: "5aaf22e42f95accecfa122d23a835911f289ecc2ff606db3e83d637ddcc0a209"
  locked_version: "version_2.9.6"
  source: "https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.9.6/PrusaSlicer-2.9.6.zip"
systems:
  os: {windows: "verified-artifact", macos: "documented", linux: "documented"}
  arch: {x64: "verified-artifact"}
  runtimes: ["PrusaSlicer 2.9.6"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "prusa-slicer-console --help"}
permissions: ["read model/profile", "write approved output directory"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["local model", "local printer/material profile", "local G-code"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "conditional"
---
# PrusaSlicer

## Purpose

Slice validated 3D models into G-code using an explicitly selected printer and
material profile.

## Install

This is a `user-managed` system capability. The Agent performs read-only
detection and provides guidance in the active user's language; the user chooses
and performs any download, installer, or package-manager setup.

Use the official PrusaSlicer GitHub release or Prusa download guidance. Verify
the selected release asset SHA-256 before use.

## Security

Read only the selected model and profile. Write only to the approved workflow
output directory. Never send, upload, queue, or start a print.

## Success

`prusa-slicer-console --help` works for the locked version, and an opt-in cube
smoke test can produce non-empty G-code with the confirmed profile.

## Known limitations

Automation remains conditional until the locked binary passes local CLI and cube
slicing smoke evidence on the target system.

## Alternatives

Use manual slicer review or OrcaSlicer manual flow when PrusaSlicer is missing
or unsuitable.

## Rollback

Remove downloaded release assets and generated private workspace state. Preserve
approved workflow outputs unless separately selected and confirmed.

## 能力用途和非目标

用途是切片和生成 G-code；非目标是发送、排队或启动打印。

## 官方获取与文档

官方来源为 PrusaSlicer GitHub release，安装文档为 Prusa Knowledge Base。

## 系统、架构、运行时和硬件支持

Windows x64 release asset 已记录 SHA；其他系统需按官方 release 重新取证。

## 五种宿主兼容矩阵

五种宿主均为 `unverified`，直到对应 host smoke 通过。

## 只读检测

只读检测命令是 `prusa-slicer-console --help`。

## 各系统安装

从 Prusa 官方下载页或 GitHub release 获取，按 OS/arch 选择资产。

## 调用示例和成功判据

成功判据是 CLI help 可读、profile 明确、G-code 非空且引用确认的 profile。

## 权限、网络、数据和遥测

安装需要网络；核心切片使用本地模型、profile 和输出目录。

## 卸载或回滚

删除可重建下载和私有 workspace 状态，保留用户确认的 outputs。

## 已知限制

未经 smoke 的版本不得宣称可自动切片。

## 替代能力

可切换到 OrcaSlicer manual provider，但必须重新确认 Gate B。
