---
name: lan-file-sharing
description: Use when an agent needs temporary local, LAN, or explicitly approved public file sharing through Dufs, Python http.server fallback, or an optional ngrok tunnel.
compatibility: Agent Workflow Hub spec 1.0; requires Python 3.11+, with pinned Dufs 0.46.0 and user-managed ngrok as optional capabilities.
metadata:
  spec-version: "1.0"
  workflow-version: "0.3.0"
  display-name: "LAN File Sharing"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '[]'
  capability-slots: '{"server":["cli.dufs"],"tunnel":["cli.ngrok"]}'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"doctor":"python <HUB_ROOT>/workflows/lan-file-sharing/scripts/lan_file_sharing.py doctor --hub-root <HUB_ROOT>","install":"python <HUB_ROOT>/workflows/lan-file-sharing/scripts/lan_file_sharing.py install --hub-root <HUB_ROOT> --confirmed-asset-sha256 <SHA256>","run":"python <HUB_ROOT>/workflows/lan-file-sharing/scripts/lan_file_sharing.py run --hub-root <HUB_ROOT> -- <DUFS_ARGS...>","serve-basic":"python <HUB_ROOT>/workflows/lan-file-sharing/scripts/lan_file_sharing.py serve-basic --directory <ABSOLUTE_DIRECTORY> --bind <ADDRESS> --port <PORT>","tunnel":"python <HUB_ROOT>/workflows/lan-file-sharing/scripts/lan_file_sharing.py tunnel --port <LOCAL_PORT> -- <NGROK_ARGS...>"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# 局域网文件分享

## 用途与触发条件

优先用固定版本 Dufs 提供本地或局域网文件服务；Dufs 不可用、用户不愿安装且只需临时下载时，可直接使用 Python `http.server`；用户明确要求公网访问时，可把已启动的本地 HTTP 端口交给用户管理的 ngrok。工作流只负责编排原生进程，不重新实现任何服务或隧道协议。

## 非目标

- 不复制 Dufs 的参数模型、HTTP/WebDAV 行为、权限系统或后台服务管理。
- 不把任一安全子集描述成 Dufs 的完整能力。
- 不把 `http.server` 冒充 Dufs：它没有上传、删除、认证、搜索、归档或 Dufs 配置能力。
- 不安装、认证或管理 ngrok 账户，不读取或记录 authtoken，也不自动选择其它隧道服务。
- 不自动修改防火墙、网络配置、系统服务或路由器设置。
- 不使用浮动版本、包管理器、Docker、Cargo、Git clone 或系统级安装替代固定运行时。

## 输入

Dufs 模式的输入是 Dufs 0.46.0 原生 argv，放在 `run` 的 `--` 之后。可直接使用服务路径和任意上游参数，也可使用 `--config <ABSOLUTE_CONFIG_PATH>`。

基础模式显式接收绝对目录、绑定地址和端口。ngrok 模式显式接收本地 HTTP 端口；`--` 后是直接追加到 `ngrok http <port>` 的原生参数，但拒绝会暴露密钥的 `--authtoken`，认证必须在工作流外预配置。需要确认参数时分别运行原生命令帮助，并以对应固定或已检测版本的官方文档为准。

工作流不得重写、过滤、补充或重新排序 `--` 后的参数。涉及凭据时优先使用权限受控的 Dufs 配置文件；不要在报告、日志或回复中复述真实密码、证书私钥或令牌。

## 输出与命名规则

`doctor` 和 `install` 输出紧凑 JSON。`run`、`serve-basic` 和 `tunnel` 保留下游原生 stdout、stderr 和退出码，均以前台进程运行；宿主负责保留各自终端会话。工作流不创建请求、计划、状态或 PID 文件。

## 依赖和运行前检查

需要 Dufs 时先运行 `doctor`。它只读验证当前平台的固定资产、工作区运行时 SHA-256 和 Dufs 0.46.0 版本，并报告唯一安装候选。缺少运行时时，展示候选 URL、大小、归档摘要、可执行文件摘要和目标目录；得到中风险安装确认后才运行 `install`。用户拒绝安装且需求仅为临时只读下载时改用基础模式，不把基础模式作为 Dufs 功能等价替代。

固定资产来自 `cli.dufs` 的 `assets-v0.46.0.json`。安装只写入 `<HUB_ROOT>/workspace/shared/runtimes/dufs/0.46.0/`，不写系统目录。

基础模式使用运行当前脚本的 Python 标准库，不新增依赖。公网模式只使用 PATH 中已安装且通过 `ngrok version` 检测的 ngrok；未认证时让用户在对话外按官方流程配置 authtoken，不要求用户把密钥发给 Agent。

## 系统修改与权限影响

`run` 会按原生参数创建网络监听，并可能读取、创建、修改或删除服务路径中的数据。风险由实际 Dufs 参数决定：例如 `--allow-upload`、`--allow-delete`、`--allow-all`、`--allow-symlink`、`--auth`、TLS、CORS 和绑定地址各自改变暴露范围或写入能力。

`serve-basic` 提供未认证的目录浏览和下载，并会跟随目录内符号链接；Python 官方不建议将它用于生产。除非用户明确要求 LAN 访问，否则绑定 `127.0.0.1`。`tunnel` 会把指定端口暴露到公网并使流量经过 ngrok 基础设施；公网授权独立于本地或 LAN 分享授权。

适配器不代替 Dufs 做权限判断，也不自动开放防火墙。若客户端不可达，先报告监听地址、端口和本机防火墙状态；任何防火墙修改必须作为独立操作另行授权。

## 执行步骤

1. 先按需求选服务端：需要完整能力时选 Dufs；只需临时下载且不安装 Dufs 时选基础模式。
2. Dufs 模式运行 `doctor`，必要时展示固定候选并确认安装，然后用 `run` 原样传递批准参数。
3. 基础模式展示目录、符号链接风险、绑定地址和端口，然后用 `serve-basic` 前台启动。
4. 从本机或获准的 LAN 客户端验证本地服务；不要在服务端尚未可用时启动隧道。
5. 仅当用户明确要求公网发布时，展示本地端口、公开范围和 ngrok 保护策略，随后用 `tunnel` 启动第二个前台进程。读取 ngrok 原生输出取得公网 URL，并从获准客户端验证。
6. 任务结束时先停止 ngrok，再停止服务端，并确认两个进程都已退出。

## 人工确认门

- 安装确认只授权下载并写入报告中的固定 Dufs 资产。
- 启动确认只授权当前原生参数所描述的监听和数据权限。
- 基础模式必须明确用户接受功能受限、无认证和符号链接风险；不得静默降级。
- 公网隧道必须单独确认；本地或 LAN 启动确认不包含公网发布。无认证公开时必须明确告知任何获得 URL 的人都可能访问服务内容。
- 新增写入、删除、外部符号链接、全接口或其它扩大暴露面的参数时，必须重新展示变化；不得沿用范围更窄的旧确认。
- 防火墙、系统服务和网络配置不属于本工作流的隐含授权。

## 失败恢复

固定运行时缺失、摘要或版本不匹配时停止 Dufs 路径并返回诊断，不回退 PATH 中的其它 Dufs 版本。用户同意且需求简单时可切换基础模式。Dufs、Python 或 ngrok 的参数错误、端口冲突、认证错误和文件权限错误均保留原生输出；不要用自建逻辑模拟失败功能。

宿主终端意外中断后，先检查该终端启动的进程是否仍存在；只处理能够归属于本次会话的进程，不按名称批量结束其它 Dufs 实例。

## 重跑、幂等与覆盖策略

`doctor` 可安全重跑。对同一固定资产重复 `install` 会重新验证并原子替换工作区运行时。三个运行入口都是原生前台执行，不自动换端口、不后台重启；重启 ngrok 可能得到不同公网 URL。文件覆盖、追加和删除完全遵循所选服务端。

## 验收标准

- 固定 Dufs 0.46.0 的归档与可执行文件摘要均验证通过。
- `run` 使用参数数组和 `shell=False`，`--` 后的参数逐项原样传给固定可执行文件。
- Dufs 的配置、认证、TLS、读写、删除、搜索、归档、哈希、CORS 和渲染参数未被适配层裁剪。
- 基础模式准确执行当前 Python 的 `http.server`，且报告其只读、无认证和非生产限制。
- ngrok 模式只执行用户管理的 `ngrok http <port>`，不接收、保存或打印 authtoken；公网发布有独立确认。
- 启动结果和退出码来自 Dufs；工作流不宣称重新实现或增强其协议行为。
- 任务结束后，本次前台服务和隧道进程均已退出；未自动修改防火墙或系统配置。

## 清理方式

停止公网分享时先终止对应 ngrok 终端，再终止 Dufs 或 Python 服务终端。用户另行要求卸载 Dufs 时，只删除能力创建且路径、版本与身份完全匹配的固定工作区运行时；工作流不卸载用户管理的 ngrok，也不得删除服务路径、配置、证书、日志、上传内容或其它实例。
