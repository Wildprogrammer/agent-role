# Agent Workflow Hub

Agent Workflow Hub 是一个面向 Agent 的模块化、可组合工作流框架。用户只需描述目标，Agent 会从根 `SKILL.md` 发现所需的角色、能力和领域工作流，并按任务需要组合执行。

它不是一个固定用途的单体 Agent：每个模块都可以独立使用，也可以成为更大业务流程中的一个环节。

## 模块如何组合

- **角色（Role）**提供领域判断，例如需求分析、测试结果分析或技术审查。
- **能力（Capability）**声明工具、依赖、版本和运行条件，例如 Git、Jenkins、LanceDB 或 AsyncSSH。
- **单一工作流（Workflow）**封装一个可独立复用的领域功能，例如操作 Git、查询 MySQL 或生成测试报告。
- **复合工作流（Composite Workflow）**按业务目标编排多个单一工作流，管理跨步骤状态和业务节点，但不复制单一工作流已有能力。

例如，一个 Agent 可以把公开工作流组合成一个电商 AI 客服：

```text
商品页面、FAQ、售后规则 → information-collection
商品说明图片、页面截图   → image-ocr（可选）
                         ↓
                  统一为本地知识材料
                         ↓
              knowledge-support-agent
                         ↓
               基于可靠来源回答顾客问题
                         ↓ 证据不足
               询问运营人员并确认答案
                         ↓
                 经验回写本地知识库
```

这只是组合方式示意：资料采集、图片识别和知识解答都可以单独使用，也能由 Agent 或复合工作流按业务需要重新编排。订单查询、退款处理等能力需要另行提供对应的领域工作流，不能从知识库能力中推断。

## 公开工作流

| 工作流 | 简短介绍 | 常见组合用途 |
|---|---|---|
| `3d-printing` | 设计、检查、拆分和切片 3D 打印模型，交付经检查的打印文件。 | 产品建模、制造准备 |
| `bead-pattern` | 把本地图片转换为固定色板的拼豆图纸。 | 图像处理、手工作品设计 |
| `daily-assistant` | 整理每日任务和进度，给出优先级建议并生成本地工作记录。 | 日程管理、工作汇报 |
| `git-operations` | 查看和操作 Git 仓库，包括提交快照读取、分支、提交、合并和推送。 | 代码开发、持续集成、发布流程 |
| `image-ocr` | 使用本地 OCR 从图片中提取按阅读顺序排列的文本。 | 文档采集、资料数字化 |
| `information-collection` | 采集、筛选和总结指定网页资料，并按需生成交付文件。 | 调研、知识库建设、需求分析 |
| `jenkins-operations` | 查询和操作 Jenkins 文件夹、视图、任务、Pipeline 与构建记录。 | 持续集成、自动化测试、发布流程 |
| `knowledge-support-agent` | 从代码仓库、文档和已采集资料构建可追溯来源的本地知识解答 Agent。 | 内部答疑、产品知识库、研发支持 |
| `meeting-notes` | 转写已授权的会议音视频，经人工审核后生成摘要和 Obsidian 会议记录。 | 会议归档、知识沉淀 |
| `mysql-operations` | 查询或操作用户配置的 MySQL 数据库，并保留数据库自身权限边界。 | 数据核查、业务运维、测试准备 |
| `requirements-analysis` | 澄清需求歧义，结合授权资料分析需求并生成可评审用例。 | 开发准备、测试设计、方案评审 |
| `ssh-operations` | 连接 Windows、macOS 或 Linux 远程设备，执行命令、传输文件和建立端口转发。 | 远程运维、环境检查、日志收集 |
| `test-reporting` | 把已有测试材料整理为结构统一、可追溯的 Markdown 测试报告。 | 自动化测试、持续集成、质量汇报 |

## 如何使用

1. 下载或克隆本仓库。
2. 按所用 Agent 宿主的接入方式注册本仓库，或让 Agent 从根 `SKILL.md` 开始读取。宿主适配说明位于 `adapters/`。
3. 直接用自然语言说明目标、输入和必要边界，由 Agent 选择并组合工作流。

例如：

- “使用需求分析工作流分析这份需求，并生成可评审用例。”
- “连接配置中的测试服务器，查询服务状态并下载日志。”
- “根据这个代码仓库和产品文档构建本地知识解答 Agent。”
- “组合资料采集、图片 OCR 和知识解答工作流，搭建一个电商 AI 客服。”

用户通常不需要直接运行 `workflows/*/scripts/` 中的脚本；这些脚本是工作流提供给 Agent 的确定性执行接口。

## 四角色协作模式

[Role-Gated Development](role-gated-development/README.md) 是仓库内置的通用四角色 Skill，可独立使用，也可以配合复杂工作流按需启用主脑、业务审查、技术审查和文档记录。它也可以从[独立仓库](https://github.com/Wildprogrammer/role-gated-development)单独获取。

## 支持宿主

根工作流合约支持 Codex、OpenClaw、Claude Code、Hermes 和 OpenCode。具体工作流能否执行，以其 `SKILL.md`、当前宿主适配证据、本机能力和目标系统权限为准。

## 参与贡献

开发、验证和提交约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 参考与致谢

本项目在角色设计过程中参考了 [agency-agents](https://github.com/msitarzewski/agency-agents) 项目；该项目采用 MIT 许可证。详细的固定来源与本地修改记录保存在项目内的角色来源文件中。该参考项目不是 Agent Workflow Hub 的预安装内容或运行时依赖。

## 许可证

MIT
