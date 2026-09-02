---
spec_version: "1.0"
id: "app.obsidian"
type: "app"
locked_version: "v1.12.7"
version_requirement: ">=1.12.7"
recommended_version: "1.12.7"
official_source: "https://obsidian.md/download"
official_docs: "https://help.obsidian.md/"
license: "Proprietary"
last_verified: "2026-07-10"
integrity:
  method: "sha256"
  value: "f35d2a35061098400a3fafc1bfd38d8bd33f1ad76df8b78b62ccdf20b0a30d26"
  locked_version: "v1.12.7"
  source: "https://github.com/obsidianmd/obsidian-releases/releases/download/v1.12.7/Obsidian-1.12.7.exe"
systems:
  os: {windows: "verified-artifact", macos: "documented", linux: "documented"}
  arch: {x64: "verified-artifact", arm64: "documented"}
  runtimes: ["Obsidian 1.12.7"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "inspect Obsidian executable metadata or configured Vault root"}
permissions: ["write approved Markdown inside resolved Vault root", "optional URI open after approval"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["local Markdown note", "resolved Vault root"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "conditional"
---
# Obsidian

## Purpose

Store approved meeting Markdown in a user-selected Obsidian Vault without
traversal, silent overwrite, or deletion.

## Install

Use Obsidian's official download page. The current Windows installer identity is
recorded from the official release asset referenced by the download page.

## Security

Resolve the Vault root before writing. Reject path traversal. New mode creates a
unique note; append and overwrite require explicit mode selection, and overwrite
requires explicit approval.

## Success

A synthetic note can be written inside a temporary Vault, path escape is
rejected, and overwrite without approval fails.

## Known limitations

This workflow writes Markdown files directly; opening Obsidian or using URI
links is optional and requires separate approval.

## Alternatives

Deliver Markdown only in workflow outputs when Vault access is unavailable.

## Rollback

Preserve Vault files. Remove only temporary test Vaults or workflow-private
state after scoped approval.

## 能力用途和非目标

用途是将批准后的 Markdown 写入 Vault；非目标是删除 Vault、静默覆盖或同步远程服务。

## 官方获取与文档

官方入口为 `obsidian.md/download`，帮助文档为 `help.obsidian.md`。

## 系统、架构、运行时和硬件支持

Windows x64 installer SHA 已记录；其他系统需按官方 release 单独记录。

## 五种宿主兼容矩阵

五种宿主均为 `unverified`。

## 只读检测

只读检测只能检查可执行文件元数据或已配置 Vault root。

## 各系统安装

从 Obsidian 官方下载页获取，不使用第三方镜像。

## 调用示例和成功判据

成功判据是临时 Vault 内安全写入、路径逃逸拒绝、未批准覆盖拒绝。

## 权限、网络、数据和遥测

核心写入本地 Markdown；安装可能需要网络。是否同步由用户 Vault 配置决定。

## 卸载或回滚

不删除外部 Vault，只清理明确选中的临时测试目录。

## 已知限制

Vault 路径必须由用户确认。

## 替代能力

无法写 Vault 时仅交付 workflow outputs 下的 Markdown。
