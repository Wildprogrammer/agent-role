---
name: knowledge-support-agent
description: Use when an agent must build, refresh, query, or improve a local evidence-backed knowledge support agent from configured repositories, documents, or previously collected web material.
compatibility: Agent Workflow Hub spec 1.0; requires Python 3.11+, one agent-local LanceDB directory, and optional loopback Ollama embedding.
metadata:
  spec-version: "1.0"
  workflow-version: "0.1.0"
  display-name: "Knowledge Support Agent"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '["python.lancedb"]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{"health":"python <HUB_ROOT>/workflows/knowledge-support-agent/scripts/knowledge_support_agent.py health --config <ABSOLUTE_JSON>","build":"python <HUB_ROOT>/workflows/knowledge-support-agent/scripts/knowledge_support_agent.py build --config <ABSOLUTE_JSON>","query":"python <HUB_ROOT>/workflows/knowledge-support-agent/scripts/knowledge_support_agent.py query --config <ABSOLUTE_JSON> --request <ABSOLUTE_JSON>","refresh":"python <HUB_ROOT>/workflows/knowledge-support-agent/scripts/knowledge_support_agent.py refresh --config <ABSOLUTE_JSON>","feedback":"python <HUB_ROOT>/workflows/knowledge-support-agent/scripts/knowledge_support_agent.py feedback --config <ABSOLUTE_JSON> --request <ABSOLUTE_JSON>"}'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---
# 知识解答 Agent

## 用途与触发条件

当用户要把一个或多个已授权知识源构建为可持续更新、能给出来源的本地答疑 Agent 时使用。适用于代码仓库与内部文档组合，也适用于只有产品文档的客服场景；领域能力只由该 Agent 配置决定，不把某个测试、电商或研发场景写成通用前提。

## 非目标

- 不实现 HTTP 服务或 MCP 服务，不安装、启动或部署 Hermes、DeepSeek Harness 等宿主。
- 不复制 Git、网页采集、Jenkins、禅道或其他领域系统的协议和实现。
- 不全量向量化原始源码，不用云 Embedding，不在模型缺失时停止主流程。
- 不提供知识库删除、批量经验删除、OCR、图片/音视频解析或复杂登录网页采集。

## 输入

唯一权威输入是绝对路径 Agent JSON 配置。一个 Agent 一个独立 LanceDB，固定写入 `<workdir>/knowledge-support/lancedb`；同一 Agent 可有多个 `git`、`local-file` 或 `collected-document` 来源。Windows 下该数据库根路径不得超过 170 字符，以便 LanceDB 内部文件保持在可写范围。配置不保存账号、密码、Token、Cookie 或连接串。

Git 来源只读取当前 HEAD 已提交的树和 Blob。URL/Wiki 先由 `information-collection` 采集为本地材料，再作为 `collected-document` 输入。补充工作流按 Agent 配置显式列出，不存在默认 Jenkins、禅道、商品或订单能力。

## 输出与命名规则

CLI 每次只在 stdout 输出一个 UTF-8 JSON 对象。构建和刷新返回来源版本、索引数量、降级状态与缺失材料；查询返回证据候选、检索模式、过期状态、代码核实候选、按需摘要请求和已配置补充工作流。最终答复由宿主 Agent 先给结论，再列 1–3 个来源，并区分明确事实、代码推断和用户确认经验；不得向用户展示内部检索分数。

## 依赖和运行前检查

先运行 `health`。LanceDB 缺失时按 `python.lancedb` 能力契约准备工作流私有运行时；普通查询不得偷偷切换其他数据库。Ollama 与 `qwen3-embedding:0.6b` 是可降级增强：可用时做全文与向量混合检索，不可用时自动降级全文检索。DOCX/PDF 解析依赖只在配置实际使用相应格式时需要。

## 系统修改与权限影响

构建、刷新和反馈只写该 Agent 的 workdir。Git 读取由 `git-operations` 的 `head-sha`、`list-tree`、`show-file` 提供，不 checkout、不读取 working tree、不改目标仓库。普通查询与来源版本检查只读；补充工作流和部署仍受各自权限边界控制。

## 执行步骤

1. 读取配置并运行 `health`；配置歧义会改变知识边界时，简述询问原因后再向用户澄清。
2. 运行 `build`。Git 只索引精确 HEAD 的允许文档与代码事实；收集网页缺本地材料时交回 `information-collection`，不在本工作流自行抓取。
3. 收到问题后运行 `query`。来源变化时自动增量刷新；刷新失败继续查询上一次有效索引并标记 `stale=true`。
4. 有足够证据时按来源作答。代码事实不足时按返回的 Commit SHA 核实原始 Blob，必要的代码用途摘要按需生成、标记推断并通过 `refresh --enrichment` 缓存。
5. 证据不足时，仅调用配置中显式选择的补充工作流；仍无可靠答案才向用户询问，不发明未配置能力。
6. 用户提供答案时，先复述用户答案并确认正确；确认后调用 `feedback`，直接写入该 Agent 的 LanceDB。纠正生成新版本并 `supersedes` 当前版本，旧版本保留追溯但不参与检索。
7. 用户要求部署时转交 `specialized-agent-deployment`；知识构建与宿主部署是两个独立阶段。

## 人工确认门

普通只读查询、来源检查、增量刷新、全文降级和已确认经验写入不增加确认门。反馈只保留“复述用户答案并确认正确”这一业务确认，不再追加写入 token。普通 Python 依赖准备遵循能力契约；约 639 MB 模型下载、批量删除或宿主部署分别由其所有者在实际发生前确认。

## 失败恢复

Embedding 不可用时返回 `fts_degraded` 并继续回答。来源刷新失败时保留旧索引并标记过期；本地采集材料缺失时返回 `needs_materialization`；LanceDB 缺失时返回 `needs_dependency`。不得通过读取脏工作树、调用云 Embedding、跳过来源标记或复制领域协议来掩盖失败。

## 重跑、幂等与覆盖策略

相同来源版本和内容哈希不重建；HEAD 变化时仅读取变化 Blob，未变化内容复用。降级构建在 Embedding 恢复后可补齐向量。新 generation 成功前保留旧索引；相同确认经验重放不新建版本，纠正必须指向当前有效版本。

## 验收标准

- Git 脏工作树不进入知识库，证据绑定完整 Commit SHA。
- 文档可全文命中；Embedding 可用时混合检索，不可用时全文检索仍返回结果。
- 未说明用途的代码只返回结构事实和摘要请求，不编造业务含义。
- 用户确认经验立即可检索，纠正后旧版本不再参与结果。
- 纯产品文档 Agent 不要求代码、Jenkins 或禅道；测试 Agent 也只使用配置选择的补充能力。
- 构建结果可独立交给部署工作流，不修改宿主配置。

## 清理方式

第一版没有删除命令。若用户明确要求清理，先列出精确 Agent workdir、数据库大小和影响，确认目标不是仓库、用户目录或其他 Agent 数据，再由通用文件操作执行；不得删除知识源、模型、宿主配置或其他 Agent 的 LanceDB。
