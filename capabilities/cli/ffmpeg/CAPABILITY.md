---
spec_version: "1.0"
id: "cli.ffmpeg"
type: "cli"
locked_version: "ffmpeg-8.1-full_build-www.gyan.dev-windows-x64.exe"
version_requirement: ">=8.1.0"
recommended_version: "8.1.0"
official_source: "https://ffmpeg.org/download.html"
official_docs: "https://ffmpeg.org/ffmpeg-devices.html"
license: "LGPL-2.1-or-later"
last_verified: "2026-07-10"
integrity:
  method: "sha256"
  value: "d1e2a156261ecc675081943197a85f08f2868784a0af499171ede89353edad31"
  locked_version: "ffmpeg-8.1-full_build-www.gyan.dev-windows-x64.exe"
  source: "local-installed-binary"
systems:
  os: {windows: "verified-local", macos: "documented", linux: "documented"}
  arch: {x64: "verified-local"}
  runtimes: ["ffmpeg version 8.1-full_build-www.gyan.dev"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect: {mode: "read-only", command: "ffmpeg -version"}
permissions: ["microphone only after approval", "read approved media", "write approved run directory"]
network:
  required_for_install: true
  required_for_core_use: false
data_access: ["local audio/video source", "normalized local audio copy"]
installation:
  policy: "user-managed"
  scope: "system"
  methods: ["existing", "manual", "official-artifact", "package-manager"]
automation_status: "conditional"
---
# FFmpeg

## Purpose

Capture or normalize approved meeting audio/video before local transcription.

## Install

Use FFmpeg's official download guidance and record the exact distribution
source, executable version, and SHA-256. The current Windows host has a verified
local binary hash recorded in frontmatter.

## Security

Microphone capture requires explicit approval naming the device and duration.
Never overwrite source media; write only normalized copies under the run output
or private workspace directory.

## Success

`ffmpeg -version` works, the selected device/backend is listed read-only, and a
synthetic clip can be normalized without touching original media.

## Known limitations

Windows capture uses `dshow`; macOS uses `avfoundation`; Linux uses ALSA/Pulse.
Each device backend must be verified locally before recording.

## Alternatives

Use an existing user-provided media file when microphone permission is absent.

## Rollback

Remove only generated normalized copies and private checkpoints after retention
review. Never delete original media.

## 能力用途和非目标

用途是采集或规范化音频；非目标是上传、转写、总结或删除原始媒体。

## 官方获取与文档

官方入口为 `ffmpeg.org/download.html`，设备文档为 `ffmpeg-devices.html`。

## 系统、架构、运行时和硬件支持

当前 Windows x64 本机二进制已记录 hash；其他系统按官方 backend 单独验证。

## 五种宿主兼容矩阵

五种宿主均为 `unverified`，直到各宿主 doctor/smoke 通过。

## 只读检测

只读检测命令是 `ffmpeg -version`，设备枚举不得开始录音。

## 各系统安装

按官方下载页选择系统构建；记录来源、版本、架构和 SHA。

## 调用示例和成功判据

成功判据是版本可读、设备可枚举、合成音频可规范化。

## 权限、网络、数据和遥测

安装可能需要网络；核心处理本地音频。麦克风权限必须显式批准。

## 卸载或回滚

删除可重建运行缓存，保留原始媒体。

## 已知限制

设备 backend 和权限模型因系统而异。

## 替代能力

可使用已有音视频文件输入。
