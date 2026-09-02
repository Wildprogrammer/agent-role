# New Conversation Continuation Prompt

Use when the user explicitly requests a new conversation with context transfer. Persist the rendered prompt as the selected handoff source's paired continuation-prompt file and use the same text as the new conversation's initial instruction.

Choose either the staged-project or compact-task variant. Replace every angle-bracket field with confirmed project data or omit the field/section. The rendered prompt must contain no placeholders, empty fields, or template instructions. Generate it after updating the authoritative source and immediately before transfer. It is a dated snapshot; the authoritative source and live state win every conflict.

## Staged Project Variant

```text
继续<动作> <项目名称>。

工作目录：
<绝对工作目录>

交接来源：<权威交接源路径>
提示词生成时间：<带时区时间>

本文是交接来源在上述时间的执行快照。若本文与交接来源或实时项目状态冲突，以交接来源和实时状态为准；交接来源变化后必须重新生成本文。

请启用 role-gated-development 四角色模式，并完整读取：

1. <项目根契约或入口说明>
2. <当前权威交接文档或状态文件>
3. <仍有效且直接相关的设计、计划、验证证据或目标模块说明，按优先级逐项列出>

启动读取和恢复规则：

- 上述列表是启动时必须先读的权威顺序；执行和验证时可以按需检查其他当前项目文件与实时环境。未列出的历史总结不能自动成为权威来源。
- 交接中记录的 SHA、测试数字、工作树状态、服务可用性和外部环境结果都是历史证据。先确认代码、配置、基线版本和环境是否未变；条件仍有效时复用证据，条件未知、失效或明确要求时才重跑。
- 保留用户和其他角色已有修改，不覆盖、不清理、不提交明确排除的路径或文件。

按照以下阶段连续执行。

阶段 0：恢复基线

1. 读取权威状态、交接和仍有效的计划，确认当前目标、未完成范围、阻塞项与下一步。
2. 检查项目当前实时状态；仅运行项目已经确认存在的版本控制、契约、测试或环境验证命令。
3. 对交接证据与实时状态进行对账，复用仍有效的证据并重跑已失效或必要的验证；差异先定位原因，不通过削弱生产约束来恢复通过。
4. 记录实际基线。需要修复时按项目约定验证和提交；无需修改时如实记录，不制造提交。

<阶段 1..N：只列未完成工作。每个阶段写明目标、确认步骤、验收证据、完成后如何进入下一阶段，以及真正需要暂停的条件。简单任务没有阶段时删除本段。>

通用执行约束：

- <已确认的分支、提交、测试、审查与文档规则；不适用则删除。>
- <用户授权范围、外部安装或写入边界、受保护路径和必须保留的修改。>
- 普通安全步骤连续执行，不反复等待批准；每个阶段完成后汇报实际修改、验证证据和剩余风险，然后进入下一阶段。
- 只有缺少用户专属凭据、外部实例、必要权限、不可推断的业务选择，或命中高风险确认门时才暂停。
- 不伪造文件、工具、命令、测试结果、真实环境验证或访问能力；没有真实环境时明确区分模拟/契约验收与真实验证缺口。
```

## Compact Task Variant

Use for simple, non-repository, or non-staged work. Do not copy repository-only instructions from the staged variant.

```text
继续处理 <任务名称>。

工作目录：
<绝对工作目录>

交接来源：<权威笔记或状态文件路径>
提示词生成时间：<带时区时间>

请启用 role-gated-development 四角色模式，并先完整读取上述交接来源。本文是该来源在上述时间的执行快照；若两者或实时状态冲突，以交接来源和实时状态为准。

确认当前目标、有效结论、下一步和适用约束后，直接执行交接来源记录的下一步。可以按需检查其他当前材料与实时环境，但不要把未列出的历史总结当作权威事实。

普通、安全且已授权的步骤连续执行。只有缺少用户专属信息或权限、需要不可推断的选择、关键证据无法取得且没有可靠替代，或操作超出已确认权限时才暂停，并准确说明所需输入。

完成后，如目标、有效结论、阻塞项或下一步发生了需要跨会话保留的变化，再更新权威交接来源；随后汇报实际结果、证据、未决问题和下一步。不要创建额外状态、计划或进度文件。
```
