---
name: test-reporting
description: Use when user-provided existing test materials must be organized into a canonical Markdown test report; Jenkins/JUnit are an optional input path, not a requirement.
compatibility: Agent Workflow Hub spec 1.0; organizes user-provided test materials into a canonical Markdown report; Jenkins/JUnit are an optional input path.
metadata:
  spec-version: "1.0"
  workflow-version: "0.3.0"
  display-name: "General Test Reporting"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '[]'
  config-templates: '{}'
  config-requirements: '{}'
  entrypoints: '{}'
  roles: '["roles/test-results-analyst.md"]'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---
# 通用测试报告

## 用途与触发条件

当用户提供已有测试材料（需求/测试范围、环境与版本、测试用例、执行记录、pytest/JUnit/Jenkins 摘要、缺陷清单、日志摘要、制品定位、已有报告或其他用户指定材料），并需要整理为规范 Markdown 测试报告时使用。开始时加载本地 `roles/test-results-analyst.md`，记录 UTF-8/LF 角色快照 digest；不得联网替换角色。只读取用户明确指定的材料，不做无范围搜索。

## 非目标

- 不触发、取消或重试测试/Jenkins 构建，不修改 Job、Pipeline、Git 分支、需求或测试代码。
- 不新增 Python parser/schema/CLI、格式适配器、安装或外部服务。
- 不复制、再实现或维护第二份 model/classification/renderer/UTF-8 字节哈希；渲染与哈希的唯一 Python 权威是 `agent_workflow_hub.test_reporting`。
- 不确认 Gate 3、不生成确认回执、不合入或推送分支；通用路径没有 Gate 3 决策权。
- 不把构建 `SUCCESS`、控制台片段或人工猜测升级为测试通过；缺失或冲突的材料不编造成数据。

## 输入

输入为用户明确提供的已有测试材料，可以覆盖：需求/测试范围、环境与版本、测试用例、执行记录、pytest/JUnit/Jenkins 摘要、缺陷清单、日志摘要、制品定位、已有报告和其他用户指定材料。Jenkins/JUnit 只是可选输入能力之一；材料不必来自 Jenkins，不要求用户提供 run ID、build number 或 commit。缺少材料时如实标记，不得补造；密码、Token、Cookie、完整敏感日志或未授权制品不得进入报告。

## 输出与命名规则

通用路径独立输出 Markdown 测试报告，默认写入 `workflows/test-reporting/outputs/` 下由工作流分配当前 report/run 标识的目录，或使用用户指定路径；不需要用户提供 run ID。报告沿用 `templates/test-report.md` 的九个固定章节：报告基本信息；测试目标与范围；测试环境与版本；材料清单与来源；执行汇总；详细结果/失败项；缺陷汇总；结论；风险、限制与缺失信息。

## 报告 Python 权威

- `agent_workflow_hub.test_reporting` 是本工作流唯一拥有的报告 Python 权威：数据模型（`TestReportModel` 及字段）、Jenkins/JUnit 分类（`classify_jenkins_attempt`）、Markdown 渲染器（`render_test_report`）与 UTF-8 字节哈希（`report_sha256`）都只由该包定义；本工作流不持有第二份 model/classification/renderer 实现。
- 自动化测试生命周期场景只在最终通过或用户明确停止后调用本工作流一次。`automated-test-lifecycle.reporting-adapter` 提供该 run 的全部候选、Jenkins/JUnit 尝试、失败决定和禅道 Bug ID；中间失败只累积为最终报告材料，不单独生成“最终报告”。adapter 验证实际写入文件的字节哈希并绑定 run/candidate/Jenkins/environment 证据；本工作流不负责最终 Git 合入确认。
- `templates/test-report.md` 只保留九个最小章节；lifecycle 字段只作为报告基本信息下的扩展行出现（仅当 automated-test-lifecycle 提供 lifecycle 上下文时由 renderer 追加），模板本身不成为第二模板/renderer。

## 依赖和运行前检查

### 运行入口与部署验证

正式报告的数据模型、分类、渲染和文件处理使用 Hub 的 `agent_workflow_hub.test_reporting` Python 包（源码在 `src/agent_workflow_hub/test_reporting/`），不是仅凭模板自由生成，也没有单独的 test-reporting CLI/MCP 服务。目标 Agent 需要文件访问和调用该包的能力；选定解释器须能导入与本 Skill 配套版本的 Hub 包，依赖约束以该版本 `pyproject.toml` 为准。只复制 Skill 目录不会复制 `src` 实现。

首次使用或环境变化时，在实际解释器中检查 `import agent_workflow_hub.test_reporting` 及模块来源，并确认本地模板和输入材料可读；无需为此连接 Jenkins 或禅道。已有结果作为输入时不要求其上游服务在线；如果任务还需要采集新结果，另按对应工作流验证连接。包可导入只证明入口可用，报告内容正确性仍按本工作流验收标准检查。

不需要安装或调用外部服务；只处理用户明确指定且可访问的材料。确认材料可访问/可读（材料可以是本地文件、粘贴文字或结构化数据）；缺失、冲突或不可读的材料必须如实标记，不能以自然语言确认代替证据。

## 系统修改与权限影响

主 Agent 默认将 Markdown 写入工作流 `outputs/` 目录；用户可指定输出路径或关闭落盘。不得覆盖目标项目文件、Jenkins 制品、用户报告或其他运行的输出。

## 执行步骤

1. 加载角色快照；列出用户明确指定的材料与来源，确认读取范围。
2. 确认材料可访问/可读；逐个读取材料；缺失、冲突或不可读时如实标记，不编造。
3. 若提供 Jenkins/JUnit 材料，先由受信适配器分类（按下方可选能力段落），分类结果进入执行汇总与结论。
4. 按模板九章节整理：报告基本信息、测试目标与范围、测试环境与版本、材料清单与来源、执行汇总、详细结果/失败项、缺陷汇总、结论、风险、限制与缺失信息。
5. 渲染 Markdown 到当前 report/run 输出目录或用户指定路径；渲染统一由 `agent_workflow_hub.test_reporting` 完成，lifecycle 绑定字段按上下文追加为扩展行，不改变九章节模板。

## Jenkins/JUnit 可选能力（automated-test-lifecycle 场景）

Jenkins/JUnit 只是可选输入能力之一，通用路径不要求材料来自 Jenkins，也不要求用户提供 run ID、build number 或 commit。仅当 automated-test-lifecycle 调用本工作流时，以下生命周期不变量生效：

- 构建 `SUCCESS` 不等于 `TESTS_PASSED`；缺失 JUnit 的 `SUCCESS` 不声称通过。
- 非零测试数（至少一个测试实际执行、并非全部跳过）且无失败/错误、并且构建成功时才分类为 `TESTS_PASSED`；只有可信 `TESTS_PASSED` 才可写“测试通过”。
- JUnit 存在失败或错误时分类为 `TESTS_FAILED`。
- 构建未成功且仅有不完整迹象、未形成完整受信执行时分类为 `TEST_EXECUTION_INCOMPLETE`。
- 证据不足时分类为 `TEST_RESULT_UNVERIFIED`；零测试或全部跳过，分类为 `TESTS_NOT_EXECUTED`；没有可读构建证据时分类为 `NO_JENKINS_EVIDENCE`。
- 本流程只渲染生命周期提供的单一最终报告材料，不发起或代替最终 Git 合入确认。
- 该场景按 automated-test-lifecycle 的受信适配契约额外返回 `TestReportResult`；`TestReportResult` 仅属于 automated-test-lifecycle，通用路径不依赖它。

## 人工确认门

- 证据前置条件：缺失、冲突或不可读的材料必须如实报告，不能以自然语言确认替代；未提供的缺陷不得写成“无缺陷”，证据不足不得写成“测试通过”。
- 通用路径没有生命周期决策权：本流程不确认 Git 合入，也不把中间失败升级为最终结论；最终合入摘要与确认由 automated-test-lifecycle 顶层编排持有。

## 失败恢复

报告生成失败时保留已读取的脱敏材料摘要并返回失败原因；不可读材料按“不可读”标记，不重触发构建。已知失败不能通过删除失败用例、隐藏尝试或只保留最后一次结果来“恢复”。

## 重跑、幂等与覆盖策略

相同材料使用相同整理规则时结果一致；报告带生成时间等元数据时不声称字节完全相同。材料变化时重新整理并生成新报告；旧报告不得被当成新 Gate 3 证据。覆盖边界明确：只替换同一 report/run 的输出，不覆盖其他 run 或用户文件。

## 验收标准

报告包含九个固定章节；覆盖用户提供的材料并如实标注缺失、冲突与不可读项；未提供的缺陷不写“无缺陷”，证据不足不写“测试通过”；Jenkins/JUnit 仅作为可选输入能力处理；通用路径输出独立 Markdown，不依赖 `TestReportResult`；仅当 automated-test-lifecycle 调用时保留其不变量。

## 清理方式

默认不清理。当前 report/run 的输出只能由用户或顶层明确的保留策略清理；不得删除 Jenkins Job、构建、制品、日志、Git 分支、需求、测试代码或其他 run 输出。
