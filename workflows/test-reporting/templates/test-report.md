# 测试报告

## 报告基本信息

- 报告标识：`<report-id|run-id>`
- 生成时间：`<generated-at>`
- lifecycle 扩展行（仅 automated-test-lifecycle 提供 lifecycle 上下文时由统一 renderer 追加；模板不是第二 renderer）：`candidate_commit` / `candidate_tree` / `jenkins_evidence_sha256` / `environment_evidence_sha256`

## 测试目标与范围

- 测试目标：`<objective>`
- 测试范围：`<scope>`
- 不在范围：`<out-of-scope>`

## 测试环境与版本

- 环境：`<environment>`
- 版本：`<versions>`

## 材料清单与来源

| 材料类型 | 路径/来源 | 状态 |
| --- | --- | --- |
| `<type>` | `<source>` | `<readable|missing|conflict|unreadable>` |

## 执行汇总

- `<summary>`（如用例数、通过/失败/跳过/未执行；构建状态与测试结论分离）

## 详细结果/失败项

- `<failed-items>`

## 缺陷汇总

- `<defects>`（未提供缺陷材料时如实标注，不写“无缺陷”）

## 结论

- `<conclusion>`（证据不足不得写“测试通过”）

## 风险、限制与缺失信息

- `<risks-and-missing>`
