# Agent Workflow Hub

Agent Workflow Hub 是一个面向 Agent 的模块化、可组合工作流框架。用户只需描述目标，Agent 会从根 `SKILL.md` 发现所需的角色、能力和领域工作流，并按任务需要组合执行。

它不是一个固定用途的单体 Agent：每个模块都可以独立使用，也可以成为更大业务流程中的一个环节。

## 模块如何组合

- **角色（Role）**提供领域判断，例如需求分析、测试结果分析或技术审查。
- **能力（Capability）**声明工具、依赖、版本和运行条件，例如 Git、Jenkins、LanceDB 或 AsyncSSH。
- **单一工作流（Workflow）**封装一个可独立复用的领域功能，例如操作 Git、查询 MySQL 或生成测试报告。
- **复合工作流（Composite Workflow）**按业务目标编排多个单一工作流，管理跨步骤状态和业务节点，但不复制单一工作流已有能力。

例如，一个 Agent 可以把公开工作流组合成持续更新的专题知识解答 Agent：

```text
agent-role 的 README、工作流说明和相关网页
                         ↓
             information-collection
                         ↓
        形成带来源和采集时间的本地材料
                         ↓
             knowledge-support-agent
                         ↓
             建立可检索的本地知识库
                         ↓
用户提问：“agent-role 怎么使用？有哪些可组合工作流？”
                         ↓
       检索相关知识并返回带来源位置的回答
                         ↓ 来源更新
          重新采集材料并增量刷新知识库
```

这只是组合方式示意：`information-collection` 负责采集和固化资料，`knowledge-support-agent` 负责索引、检索与来源化回答，两者也都可以独立使用。同样的组合还可以用于 GitHub 热点、开源项目动态或某股票的公开公告与资讯；股票相关材料是带采集时间的公开信息快照，不代表实时行情或投资建议。

另一个常见组合是完成从需求澄清到测试报告的测试交付链路：

```text
需求澄清与用例设计
  requirements-analysis
          ↓
代码版本管理与推送
     git-operations
          ↓
持续集成执行
  jenkins-operations
          ↓
测试结果整理
    test-reporting
```

其中，每个工作流只负责自己的领域能力，也可以单独使用；Agent 根据测试目标把它们组合成一条完整链路。

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
| `specialized-agent-deployment` | 把一个主工作流及其选定的关联 Skill 固定为可部署的专用 Agent，并适配已有 Hermes 或 DeepSeek Harness 宿主。 | 业务专用 Agent、固定能力组合、宿主部署 |
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
- “使用资料采集工作流收集 agent-role 的说明资料，交给知识解答工作流建立知识库，并回答 agent-role 的使用问题。”
- “把这个主工作流和我选定的辅助 Skill 部署成一个 DeepSeek Harness 专用 Agent。”

用户通常不需要直接运行 `workflows/*/scripts/` 中的脚本；这些脚本是工作流提供给 Agent 的确定性执行接口。

## 四角色协作模式

[Role-Gated Development](role-gated-development/README.md) 是仓库内置的通用四角色 Skill，可独立使用，也可以配合复杂工作流按需启用主脑、业务审查、技术审查和文档记录。它也可以从[独立仓库](https://github.com/Wildprogrammer/role-gated-development)单独获取。

## 支持宿主

根工作流合约支持 Codex、OpenClaw、Claude Code、Hermes 和 OpenCode。具体工作流能否执行，以其 `SKILL.md`、当前宿主适配证据、本机能力和目标系统权限为准。

## 参与贡献

诚挚邀请您参与更多工作流的开发，分享您的经验与技能。开发、验证和提交约定见 [CONTRIBUTING.md](CONTRIBUTING.md)，也欢迎您提出宝贵意见或需求。

## 参考与致谢

本项目在角色设计过程中参考了 [agency-agents](https://github.com/msitarzewski/agency-agents) 项目；该项目采用 MIT 许可证。详细的固定来源与本地修改记录保存在项目内的角色来源文件中。该参考项目不是 Agent Workflow Hub 的预安装内容或运行时依赖。

## 许可证

MIT
