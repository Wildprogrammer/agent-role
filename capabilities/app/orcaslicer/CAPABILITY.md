---
spec_version: "1.0"
id: "app.orcaslicer"
type: "app"
locked_version: "v2.4.2"
version_requirement: ">=2.4.2"
recommended_version: "2.4.2"
official_source: "https://github.com/OrcaSlicer/OrcaSlicer/releases/tag/v2.4.2"
official_docs: "https://www.orcaslicer.com/"
license: "AGPL-3.0"
last_verified: "2026-07-10"
integrity:
  method: "sha256"
  value: "f602f9d646f49bee387d8f342a0193a7ef20756eabb845e19795c5a62b90a58a"
  locked_version: "v2.4.2"
  source: "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/OrcaSlicer_Windows_Installer_V2.4.2_x64.exe"
systems:
  os: {windows: "verified-artifact", macos: "documented", linux: "documented"}
  arch: {x64: "verified-artifact", arm64: "documented"}
  runtimes: ["OrcaSlicer 2.4.2"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "orcaslicer --help"}
permissions: ["read model/profile", "write approved output directory"]
network:
  required_for_install: true
  required_for_core_use: false
  cloud_sync_default: "disabled"
data_access: ["local model", "local printer/material profile", "local G-code"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "manual"
---
# OrcaSlicer

## Purpose

Provide a manual or explicitly smoke-tested fallback slicer for validated 3D
models.

## Install

This is a `user-managed` system capability. The Agent performs read-only
detection and provides guidance in the active user's language; the user chooses
and performs any download, installer, or package-manager setup.

Use OrcaSlicer's official website or GitHub release page. Verify the selected
release asset SHA-256 before use.

## Security

Disable cloud/profile sync unless the user separately approves it. Read only the
selected model and profile; write only to the approved workflow output
directory. Never send, upload, queue, or start a print.

## Success

Manual success requires the user to confirm profile selection, slicing warnings,
preview, and exported G-code path. Automation requires a separate locked-version
smoke test and remains unavailable in this record.

## Known limitations

This capability is `manual` until a stable, documented automation invocation is
proved for the locked version on the target system.

## Alternatives

Use PrusaSlicer as the default conditional automated provider when its smoke
evidence exists.

## Rollback

Remove downloaded release assets and private workspace state. Preserve approved
workflow outputs unless separately selected and confirmed.

## 能力用途和非目标

用途是作为手动或未来经验证的切片 provider；非目标是默认自动化或控制打印机。

## 官方获取与文档

官方网站声明官方站点和 GitHub release；本记录锁定 GitHub release `v2.4.2`。

## 系统、架构、运行时和硬件支持

Windows x64 installer SHA 已记录；其他系统和自动化入口需要单独 smoke。

## 五种宿主兼容矩阵

五种宿主均为 `unverified`。

## 只读检测

只读检测命令记录为 `orcaslicer --help`，但自动化状态仍为 manual。

## 各系统安装

只从 `www.orcaslicer.com` 或官方 GitHub release 获取。

## 调用示例和成功判据

成功判据是人工确认 preview、warnings、profile 和导出的 G-code。

## 权限、网络、数据和遥测

安装需要网络；核心使用本地文件。云同步默认禁用，除非另行批准。

## 卸载或回滚

删除可重建下载和私有 workspace 状态。

## 已知限制

没有锁定版本 smoke 前不得自动化。

## 替代能力

优先使用已通过 smoke 的 PrusaSlicer 条件自动化 provider。
