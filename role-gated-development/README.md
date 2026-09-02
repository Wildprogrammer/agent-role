# Role-Gated Development

Role-Gated Development 是一种通用、风险驱动的四角色工作模式，可独立用于软件开发、方案设计、审查和跨会话交接。它不依赖 Agent Workflow Hub 的 Python 包、CLI、配置或业务工作流。

权威维护源位于 [Agent Role 仓库的 `role-gated-development/` 目录](https://github.com/Wildprogrammer/agent-role/tree/master/role-gated-development)；[独立仓库](https://github.com/Wildprogrammer/role-gated-development)由该目录发布，内容不单独维护。

## 四个角色

- Role 1：主脑，始终启用，负责澄清阻塞、决策、实施、验证和整合审查意见。
- Role 2：业务逻辑审查，仅在产品目的、业务规则、范围、非目标、验收标准或方向性取舍发生实质变化时触发。
- Role 3：技术审查，仅在复杂正确性、安全、隐私、权限、外部副作用、公共契约、兼容性、架构或高风险重构需要独立判断时触发。
- Role 4：文档记录，仅在信息必须跨会话保留、存在可复用决定或独立审查结论、需要暂停恢复、用户要求交接或明确要求文档时触发。

Role 2、Role 3 和 Role 4 是按需门，不是每个任务都必须依次执行的固定流水线。

## 任务状态

- `direct`：实现和验证清晰、有界、低风险。
- `light`：边界明确，但存在普通实现复杂度或一个可选判断。
- `planned`：方向、业务含义、技术方案、验收、权限、顺序或重大风险尚未收敛。

状态可随真实信息和风险双向升降级；不能仅因文件多、步骤多、时间经过或任务接近结束而升级或降级。

## 安装

从 Agent Role 仓库根目录安装：

```powershell
Copy-Item -Recurse -LiteralPath .\role-gated-development `
  -Destination "$env:USERPROFILE\.codex\skills\role-gated-development"
```

从独立仓库根目录安装时，只复制 Skill 的 12 个发布文件，不复制仓库的 `.git` 元数据：

```powershell
$destination = "$env:USERPROFILE\.codex\skills\role-gated-development"
New-Item -ItemType Directory -Path $destination | Out-Null
Copy-Item -LiteralPath .\LICENSE, .\README.md, .\SKILL.md -Destination $destination
Copy-Item -Recurse -LiteralPath .\agents, .\assets -Destination $destination
```

如果目标目录已经存在，请先自行备份并确认差异，不要直接覆盖正在使用的版本。安装后重新启动或刷新支持 Skill 发现的宿主。

## 使用

显式调用：

```text
$role-gated-development 请用四角色模式完成这项开发任务。
```

常用模式控制：

```text
启用四角色模式
暂停四角色模式
恢复四角色模式
退出四角色模式
```

简单、低风险任务通常只使用 Role 1。业务方向变化、安全或公共契约变化、以及跨会话交接分别按 `SKILL.md` 中的触发条件启用相应角色。

## 文件

- `SKILL.md`：四角色模式的核心行为与边界。
- `agents/openai.yaml`：Codex UI 名称、说明和默认调用提示。
- `assets/status-template.md`：唯一权威当前状态模板。
- `assets/business-review-template.md`：需要持久化的独立业务审查模板。
- `assets/tech-review-template.md`：需要持久化的独立技术审查模板。
- `assets/decisions-template.md`：可复用决定和替代方案记录模板。
- `assets/plan-template.md`：必须跨会话保存时使用的实施计划模板。
- `assets/progress-template.md`：确有审计需求时使用的可选时间线。
- `assets/session-contract-template.md`：没有其他权威交接源时使用的最小交接契约。
- `assets/new-session-prompt-template.md`：用户明确要求新会话交接时使用的续接提示词模板。

模板不是必建文件。只在 `SKILL.md` 规定的触发条件成立时使用，并删除未使用字段和模板说明。

## 兼容性

`SKILL.md` 是核心契约，`agents/openai.yaml` 提供 Codex UI 元数据。其他宿主只有在支持兼容的 Skill 发现、子代理和文件操作时才能使用；本发布不声明未经验证的宿主兼容性。

## 许可证

MIT，见 [LICENSE](LICENSE)。
