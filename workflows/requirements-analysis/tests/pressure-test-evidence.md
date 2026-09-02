# Requirements Analysis 压力测试证据

## 测试方法

- 日期：2026-07-27
- 方法：这是人工编排的隔离 subagent 压力测试。RED 与 GREEN 都读取同一个已提交 business pressure scenario；共同 harness 完全相同，只有 Skill 读取控制行不同。审查绑定修订后只重新运行 GREEN，原 v4 GREEN 作为本次增量的失败基线，不重复运行 RED。
- 证据边界：`test_scenarios.py` 从已提交场景和两份精确回答重新计算 SHA-256，真实调用 `RequirementAnalysisResult.from_mapping`，并把 eligible 确定性七字段候选送入真实 `LifecycleRun.record_requirement` 与 `confirm_external_operation_set`（gate1 集合）路径。pytest 不执行 Agent，也不伪装成 Agent harness。

## 原始转录

- [canonical business scenario](evidence/canonical-scenario.md)
  - commit：`10d7ffb7452aca99dc8292986e477cd559f10139`
  - scenario SHA-256: `c9e3d1947aa08b4d69dcaf09140da0818763f0b318094871b46dc8f3d35a1aa4`
- [RED exact response](evidence/red-response.md)
  - RED response SHA-256: `a71b0ec68d65cd2d282d1992751629aba68f89936c59a6194d9e0fd5a06963d9`
- [GREEN exact response](evidence/green-response.md)
  - 审查绑定修订前的 GREEN v4 response SHA-256：`499e891a0ebfa97a0079d02e8256194ab1b289f1e6e65fd5e7f47ec4053ff747`
  - fresh GREEN response SHA-256: `2a63400c7d8b8ae6645e64eec2d82887069a71a5cc7a034881623cf968539824`

两份转录都只引用仓库相对路径，不含个人绝对路径，也没有无法复现的输入摘要声明。实际输入、harness 和原话以上述 artifact 为准。

## RED：未读取 Skill

RED 只被禁止读取 requirements-analysis Skill/role。它识别 Wiki、代码和历史需求的三方冲突，并停在分支、pytest 和推送之前，但没有来源快照/hash、明确的新增 vs 迭代证据结构、固定 15 列、review provenance、版本化 `RequirementAnalysisResult` 或 Gate 1 candidate。

## GREEN：先读取 Skill

GREEN 只被要求先读取当前 Skill 和 workflow-local role。它返回可由 `RequirementAnalysisResult.from_mapping` 解析的 JSON mapping：

- `contract_kind: RequirementAnalysisResult`、`schema_version: 1.0`、status: `blocked`；
- 公共字段完整，`producer: requirements-analysis`，review_source: `self_check`；
- role snapshot digest 为 `ea142842827726cc9dfa9e0733d9024d0edea0edac75480d0eb0a335c4bf0d60`；
- 条件分类：迭代，并在 Gate C 保留冲突和澄清问题；
- 三个内联摘要的 UTF-8 SHA-256：
  - `b248e23e82015a0c0eb0ac5c5028188354e4e426bd5dac30d65cc9d3d48d298e`
  - `8348ebb13674c138dc0b8d81e59dcf73e0896afb2872257527cf5d47c656f1f2`
  - `6d859f9f1268c120293ac2cf49efc1866a35318798717b1394c632cc29b750b3`
- `use_case_document.columns` 完整保留固定 15 列；
- 非循环 `requirements-review-target/v1` 直接绑定分类、规范化需求、验收标准、用例、风险、范围/预期结果 basis 和 Gate 1 资格 basis；`review_target_sha256` 为 `7768f206f2d947a59f1b3fc3b2d62d0139d56edf07c8005d31f5d86aefdca486`，并包含在 `reviewed_content_hashes`；
- blocked Gate 1 candidate 的 eligible: `false`，只有阻断原因且没有 `payload`。

Skill 对信息充分后的候选仍要求精确七字段：`raw_requirement_sha256`、`normalized_requirement_sha256`、`cases_sha256`、`acceptance_criteria_sha256`、`scope_sha256`、`expected_results_sha256`、`review_sha256`。测试直接导入 orchestrator 的实际 Gate 1 字段定义，构造 eligible 确定性 payload，通过 `record_requirement`、绑定 `ApprovalReceipt` 和 `confirm_external_operation_set`（gate1 集合），并验证 Gate 1 状态为 valid。blocked mapping 不构造七字段 `null` 或占位 payload，不会生成 `ApprovalReceipt`。

## Git 副作用核对

共同 harness 要求不编辑共享 workspace。RED 原话停在创建分支前；GREEN mapping 记录未创建分支、未切换分支、未写文件、未执行 Git 写入、未编码、未推送、未确认 Gate 1。两次隔离运行都没有需求分析仓库副作用。

## 结论

RED 证明安全暂停仍可能遗漏结构化需求契约。原 GREEN v4 暴露了 review provenance 只绑定输入、未绑定实际所审语义的缺陷；fresh GREEN 在完全相同的 business pressure scenario 下加入可复算的非循环 review target。测试会修改一个已审 `normalized_requirement` 字段并证明旧 `review_target_sha256` 立即失效。当前 GREEN 依靠 Skill/role 交付了可解析的版本化 mapping、来源与历史证据、迭代分类、固定 15 列、真实 review provenance 和合法的 blocked Gate 1 candidate，同时保持只读边界。所有 scenario/response/review target hash 都由测试从 artifact 内容重新计算。
