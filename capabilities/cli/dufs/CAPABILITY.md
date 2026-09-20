---
spec_version: "1.0"
id: "cli.dufs"
type: "cli"
locked_version: "0.46.0"
version_requirement: ">=0.46.0"
recommended_version: "0.46.0"
official_source: "https://github.com/sigoden/dufs"
official_docs: "https://github.com/sigoden/dufs/blob/v0.46.0/README.md"
license: "MIT OR Apache-2.0"
last_verified: "2026-09-20"
integrity:
  method: "sha256"
  value: "21d829653ac1f178b124ed384e2a57df19b99ae404935cba9a1a5719c199b0bf"
  executable_value: "3e443496f7811f931232e1f79eeca23d7d7ff1ea0c9dcac67b6586d69b47e11e"
  locked_version: "0.46.0"
  source: "https://github.com/sigoden/dufs/releases/download/v0.46.0/dufs-v0.46.0-x86_64-pc-windows-msvc.zip"
  manifest: "assets-v0.46.0.json"
systems:
  os: {windows: "documented", macos: "documented", linux: "documented"}
  arch: {x86_64: "documented", arm64: "documented", arm: "documented", armv7: "documented"}
  runtimes: ["standalone native executable; no language runtime required"]
  hardware: ["no special hardware; the asset must match the operating system and architecture"]
hosts:
  codex: "unverified"
  openclaw: "unverified"
  claude-code: "unverified"
  hermes: "unverified"
  opencode: "unverified"
detect:
  mode: "read-only"
  command: "<workspace-shared dufs executable> --version"
permissions: ["write the exact workspace-shared Dufs runtime after confirmation", "run user-approved native Dufs arguments"]
network:
  required_for_install: true
  required_for_core_use: true
data_access: ["access paths explicitly supplied through Dufs arguments or configuration", "serve data through the network listeners configured by the user"]
installation:
  policy: "agent-managed"
  scope: "workspace-shared"
  methods: ["existing", "official-artifact"]
---
# Dufs

## 能力用途和非目标

提供固定版本 Dufs 0.46.0 的完整原生 CLI/config 能力。适配层只负责固定运行时检测、受控安装和 argv 透传，不复制或裁剪 Dufs 的文件服务、认证、TLS、读写、删除、搜索、归档、哈希、CORS、日志或页面渲染功能。

## 官方获取与文档

源码与发布来自 `https://github.com/sigoden/dufs`，固定版本文档为 `https://github.com/sigoden/dufs/blob/v0.46.0/README.md`。资产只取自 `assets-v0.46.0.json` 中列出的官方 v0.46.0 Release，并同时验证归档和可执行文件 SHA-256。

## 系统、架构、运行时和硬件支持

清单包含 macOS arm64/x86_64、Windows arm64/x86_64，以及 Linux arm64/arm/armv7/x86_64。Dufs 是独立原生可执行文件，无额外语言运行时或专用硬件要求；每台主机仍须按系统、架构、摘要和版本实测。

## 五种宿主兼容矩阵

Codex、OpenClaw、Claude Code、Hermes 和 OpenCode 的登记状态均为 `unverified`。宿主只要能以前台进程执行 argv 即可使用本能力；目录登记本身不等于实机验证。

## 只读检测

检测只对固定 workspace-shared 路径中的可执行文件校验 SHA-256，并以参数数组执行 `--version`，要求唯一输出证明版本为 0.46.0。不会启动监听、修改文件或访问网络，也不会用 PATH 中的其它版本替代。

## 各系统安装

安装策略为 `agent-managed`、`workspace-shared`。安装前报告唯一官方资产、大小、目标、回滚路径及双摘要，并取得中风险确认。安装器验证下载大小和归档摘要，只提取清单指定的一个可执行文件，验证其摘要与版本后原子写入固定运行时目录。不使用系统包管理器、Docker、Cargo、Git clone、提权安装或浮动版本。

## 调用示例和成功判据

调用入口为：

```text
python <HUB_ROOT>/workflows/lan-file-sharing/scripts/lan_file_sharing.py run --hub-root <HUB_ROOT> -- <DUFS_ARGS...>
```

`--` 后的每个字符串原样传给固定 Dufs 可执行文件。例如可传服务路径、`--config`、绑定、认证、TLS、上传、删除、搜索或渲染参数。成功判据是固定运行时检测通过、实际 argv 与用户批准内容一致，并返回 Dufs 原生输出和退出码。

## 权限、网络、数据和遥测

实际权限由原生参数或配置决定。Dufs 可能监听一个或多个网络地址，并读取、创建、修改或删除显式配置范围内的数据。适配器不扩大路径、不修改防火墙，也不隐藏上游输出；使用者必须根据绑定、认证和写入参数判断暴露面。不得声称未经核实的上游遥测行为。

## 卸载或回滚

回滚仅删除能力创建且路径、版本与身份完全匹配的 workspace-shared Dufs 0.46.0 运行时。不得删除服务数据、配置、证书、日志、其它版本或系统安装。

## 已知限制

适配器以前台进程运行 Dufs，不提供自建守护进程、租约、PID 数据库、网络接口选择或防火墙管理。Dufs 参数和协议行为以固定版本官方文档及原生输出为准。

## 替代能力

目标平台无固定资产、摘要或版本无法验证时停止自动运行并提供人工指引。不得自动改用未固定版本、第三方镜像、包管理器、Docker、Cargo、Git clone 或其它文件服务。
