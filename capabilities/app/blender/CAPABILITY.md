---
spec_version: "1.0"
id: "app.blender"
type: "app"
locked_version: "blender-5.1.2-windows-x64.zip"
version_requirement: ">=4.4.0"
recommended_version: "5.1.2"
official_source: "https://download.blender.org/release/Blender5.1/blender-5.1.2-windows-x64.zip"
official_docs: "https://docs.blender.org/manual/en/latest/addons/mesh/3d_print_toolbox.html"
license: "GPL-3.0-or-later"
last_verified: "2026-07-10"
integrity:
  method: "sha256"
  value: "345bedea7b0acf7cc9666423d8553f9129622aea34ded65c23e8cb70f83f14ff"
  locked_version: "blender-5.1.2-windows-x64.zip"
  source: "https://download.blender.org/release/Blender5.1/blender-5.1.2.sha256"
systems:
  os: {windows: "verified-artifact", macos: "documented", linux: "documented"}
  arch: {x64: "verified-artifact", arm64: "documented"}
  runtimes: ["Blender 5.1.2"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "blender --version"}
permissions: ["read/write user-selected .blend files", "write approved export paths"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["local Blender scene", "user-selected model exports"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "conditional"
---
# Blender

## Purpose

Create and revise printable 3D models, save `.blend` checkpoints, and export
mesh artifacts for validation and slicing.

## Install

This is a `user-managed` system capability. The Agent performs read-only
detection and provides guidance in the active user's language; the user chooses
and performs any download, installer, package-manager, or portable setup.

Use the official Blender download archive locked in frontmatter. Verify the
SHA-256 against Blender's official checksum file before extraction or use.

## Security

Do not execute arbitrary Python unless the exact code, affected files, and
expected scene changes are shown to the user and approved.

## Success

`blender --version` reports Blender 5.1.2 or an explicitly accepted equivalent,
and the workflow can save a checkpoint and export a mesh into an approved path.

## Known limitations

Blender is not a slicer and does not prove printability by itself. Mesh
validation and slicer review remain required gates.

## Alternatives

For precision CAD work, route to a dedicated CAD tool only after documenting the
tool, source, version, and export path.

## Rollback

Remove the downloaded archive or extracted app directory from shared workspace
state. Preserve user-authored `.blend` and exported workflow outputs.

## 能力用途和非目标

用途是建模、检查和导出网格；非目标是切片、发送 G-code 或启动打印。

## 官方获取与文档

官方获取地址为 Blender release archive；3D Print Toolbox 文档来自 Blender Manual。

## 系统、架构、运行时和硬件支持

Windows x64 artifact 和 SHA 已记录；其他系统为 documented，需本地 smoke 后才能 promoted。

## 五种宿主兼容矩阵

五种宿主均为 `unverified`，因为尚未完成 host-specific smoke。

## 只读检测

只读检测命令是 `blender --version`。

## 各系统安装

仅从 blender.org 或 download.blender.org 获取；不要使用第三方镜像。

## 调用示例和成功判据

成功判据是版本可读、可保存 `.blend`、可导出 mesh 到批准目录。

## 权限、网络、数据和遥测

安装需要网络；核心使用本地文件。只读检测不应上传场景或启用在线资产服务。

## 卸载或回滚

删除可重建安装缓存，保留用户产物。

## 已知限制

不能单独证明模型可打印。

## 替代能力

可使用经记录和批准的 CAD 工具作为建模替代。
