---
name: ssh-operations
description: Use when an agent must connect to configured SSH targets to run commands, execute related remote steps, transfer or manage files, traverse jump hosts, or open SSH port forwarding.
compatibility: Agent Workflow Hub spec 1.0; requires Python 3.11+, AsyncSSH, and authorized Windows, macOS, or Linux SSH targets.
metadata:
  spec-version: "1.0"
  workflow-version: "0.1.0"
  display-name: "SSH Operations"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '["python.asyncssh"]'
  config-templates: '{"main":"references/ssh.ini.example"}'
  config-requirements: '{"main":{"scope":"repository-external","required":true}}'
  entrypoints: '{"doctor":"python <HUB_ROOT>/workflows/ssh-operations/scripts/ssh_operations.py doctor --config <ABSOLUTE_INI>","exec":"python <HUB_ROOT>/workflows/ssh-operations/scripts/ssh_operations.py exec --config <ABSOLUTE_INI> --target <TARGET> --command <COMMAND>","run-steps":"python <HUB_ROOT>/workflows/ssh-operations/scripts/ssh_operations.py run-steps --config <ABSOLUTE_INI> --request <ABSOLUTE_JSON>","sftp":"python <HUB_ROOT>/workflows/ssh-operations/scripts/ssh_operations.py sftp --config <ABSOLUTE_INI> --request <ABSOLUTE_JSON>","upload":"python <HUB_ROOT>/workflows/ssh-operations/scripts/ssh_operations.py upload --config <ABSOLUTE_INI> --request <ABSOLUTE_JSON>","download":"python <HUB_ROOT>/workflows/ssh-operations/scripts/ssh_operations.py download --config <ABSOLUTE_INI> --request <ABSOLUTE_JSON>","forward":"python <HUB_ROOT>/workflows/ssh-operations/scripts/ssh_operations.py forward --config <ABSOLUTE_INI> --request <ABSOLUTE_JSON>"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# SSH 操作

## 用途与触发条件

当 Agent 需要按用户配置连接 Windows、macOS 或 Linux SSH 服务，执行一个命令或有前后依赖的命令步骤，使用 SFTP/SCP 管理与传输文件，经过单跳或多跳跳板机，或建立本地、远端、SOCKS 转发时使用。本工作流拥有通用 SSH 协议能力；复合工作流只声明任务编排和业务节点，不复制这里的连接、认证、TOFU、文件传输或转发实现。

普通已授权命令与文件写入连续执行，不添加通用写确认。只有结构化删除，或主 Agent 根据用户本次意图明确标为大数据量、影响范围大的高影响命令，才要求一次确认。

## 非目标

- 不把 environment-validation 的只读限制、其他复合流程的阶段门或宿主配置限制带入通用 SSH 操作。
- 不安装或配置远端 SSH 服务，不修改防火墙、网络、系统 SSH 配置、账号权限或认证策略。
- 不绕过服务端 RBAC，不自动接受已记录后发生变化的 Host Key，不使用 `shell=True` 调用本机命令。
- 第一版不支持交互式编辑器、长时间交互安装器、X11、TUN/TAP、远程桌面或通用终端复用。

## 输入

输入是用户私有 INI 的绝对路径。INI 可独立使用，也可与其他工作流共享同一个 INI；加载时只解析 SSH 自有的 `[ssh]`、`[target:<name>]` 和 `[group:<name>]`，忽略其他节，但仍严格拒绝 SSH 自有节中的未知字段。`[target:<name>]` 定义主机、端口、用户名、认证、操作系统、Shell、超时和可选 `via`；`[group:<name>]` 定义多目标与最大并发。支持密码、私钥、SSH Agent 和自动模式。`password`、`sudo_password` 允许按用户选择以明文写入私有配置，也可通过对应环境变量名读取；同一秘密同时声明直接值和环境变量来源时拒绝。`sudo_password` 为空时默认复用登录密码。

私钥路径相对于 INI 解析；配置和 JSON 请求本身必须是已存在的绝对路径。关联步骤可声明工作目录、环境变量、依赖步骤、超时、`sudo`、PTY 与失败后继续策略，并通过 `${steps.<id>.stdout}` 引用已经完成且显式依赖的前序输出；引用值由目标 Shell 适配器引用，不拼成未转义文本。

## 输出与命名规则

每次短命令、步骤、文件操作和多目标任务向 stdout 输出一个 UTF-8 JSON 对象，包含 `success|partial|failed|cancelled|needs-elevation`、目标、退出码、stdout、stderr、耗时和可证明的完成事实。进度只写 stderr。转发启动后先输出一个 `ready` JSON 对象，再保持进程直到关闭。

配置中出现的密码、sudo 密码和私钥口令按精确值脱敏，不进入命令行参数或结构化结果。远端程序自行输出的任意未知秘密无法可靠识别，Agent 必须按任务的数据敏感性控制命令和结果使用。

## 依赖和运行前检查

先运行 `doctor`，验证绝对配置、目标引用、跳板环、私钥文件、专用 known-hosts 路径和 AsyncSSH 2.24.0。doctor 只读，不建立 SSH 连接。缺少依赖时返回 `needs_dependency`，按 `python.asyncssh` 能力契约在工作流私有运行时中使用哈希锁安装；依赖准备只确认一次，不为 Git、命令或普通写入新增确认。

首次真实连接使用 TOFU：在握手中记录该目标实际地址与端口的 Host Key；同一键后续直接接受，键变化立即拒绝且不覆盖。每个跳板和最终目标分别验证。Agent Forwarding 默认关闭，只有目标明确配置 `forward_agent=true` 时启用。

## 系统修改与权限影响

命令仅以 SSH 登录账号当前权限执行。Linux/macOS 的 `sudo` 使用 `sudo -S -p '' --` 从 stdin 提供密码，密码不进入命令文本和结果；没有配置密码时先按目标自身无密码 sudo 行为执行。Windows 不模拟 Unix sudo，也不在 SSH 会话里绕过 UAC；当前权限不足时返回 `needs-elevation`。

SFTP 支持 list、stat/lstat、read/write、mkdir、rename/move、chmod、symlink/readlink、remove/rmdir，以及使用同目录临时文件、可兼容偏移恢复和完成后重命名的上传下载。SCP 支持上传、下载、递归和保留属性；SCP 不支持断点续传，发生中断只报告 `partial` 和已知完成事实。SFTP 路径遵循 SFTP 服务端语义，不套用 Windows 命令 Shell 路径转换。

端口转发只绑定请求明确指定的地址和端口；支持本地转发、远端转发和 SOCKS。端口 `0` 返回实际绑定端口。关闭或取消时释放 listener、叶连接和全部跳板连接。

## 执行步骤

1. 读取私有 INI 和请求，严格校验未知字段、凭据来源冲突、目标/组、跳板引用和环；不输出秘密。
2. 对每个连接在真实握手执行 TOFU。按密码、私钥或 Agent 认证；仅在认证前网络连接失败时有限重试，认证失败和 Host Key 不匹配不重试。
3. 显式 OS/Shell 先执行固定可用性探针；自动模式先探测 Windows，再用 `uname -s` 区分 Linux 与 macOS。不能确认时停止，不猜测 Shell。
4. 单命令通过 AsyncSSH `run` 执行。有依赖的步骤在同一个远端 Shell 中依次执行，保持 cwd 和环境；每步使用随机 128-bit nonce 标记结果、限制输出，并只允许引用已完成依赖。
5. 文件操作直接使用 SFTP API；传输按选定 SFTP 或 SCP 语义执行。端口转发返回 ready 后保持，直到用户取消或连接关闭。
6. 多目标按配置串行或有限并发；单个目标失败不取消其他目标，最终保持输入顺序并给出 `partial`。

## 人工确认门

只有两类操作需要一次确认：`remove/rmdir` 等结构化删除；主 Agent在调用前已明确归类为大数据量或影响范围大的高影响命令。`sudo`、明确覆盖文件、普通上传下载、创建目录、修改 Pipeline 或一般远端命令本身不是新增确认理由。确认绑定当前操作摘要和精确目标列表；请求发生实质变化时重新询问。

首次 TOFU 记录不询问；后续键不匹配直接拒绝并报告旧/新指纹，由用户在工作流外核对。工作流不会自动删除或替换 known-hosts 条目。

## 失败恢复

配置错误、缺依赖、DNS/TCP、认证、Host Key、跳板、Shell 探测、权限、超时和远端非零退出分别返回明确类别。认证失败不换凭据，Host Key 变化不覆盖，权限不足不尝试绕过。网络中断只重试尚未认证且尚未执行命令的连接；命令结果未知时不自动重跑可能产生副作用的命令。

关联步骤在同一 Shell 中遇到超时、输出上限或连接关闭时停止该目标，并保留已经完成步骤。`on_failure=continue` 只适用于已获得明确非零退出的步骤，不把连接状态未知当成可继续。

## 重跑、幂等与覆盖策略

只读查询可按相同目标重跑。写命令是否幂等由调用任务决定，本工作流不假设；结果未知时先查询远端状态再决定。SFTP 使用请求专属 `.agent-workflow-hub-<request-id>.part`，仅在大小与方向兼容时续传；完成后重命名，服务端不支持原子替换时如实报告。明确 `overwrite=true` 允许覆盖，不额外确认；未声明时目标已存在即冲突。

SCP 永远报告 `resume_supported=false`。多目标重跑只重跑用户或上层流程选择的失败目标，不自动重复已成功目标。

## 验收标准

Hub 合约验证和本工作流测试通过；doctor 不连接远端；AsyncSSH 版本和哈希锁固定。临时本地 SSH 服务端验证密码、密钥、TOFU 首次记录与变更拒绝、跳板清理、命令、关联步骤、SFTP/SCP 和三类转发。Windows、macOS、Linux 真实设备按实际执行分别标记 `verified|documented|not-tested`，不得以临时服务端替代实机声明。

普通写入无通用确认；删除和明确高影响命令仅一次确认。Agent Forwarding 默认关闭；Windows 权限不足返回 `needs-elevation`；SCP 不声明续传；任何凭据都不进入命令行和 JSON 输出。

## 清理方式

关闭转发和 SSH/SFTP 连接，删除自动化测试创建的临时目录、临时密钥和临时服务端数据。工作流私有 Python runtime 可按能力契约单独回滚。默认不删除用户 INI、私钥、known-hosts、已传输文件或远端数据；删除这些对象必须由用户点名精确目标，并按高影响删除的一次确认执行。
