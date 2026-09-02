---
name: mysql-operations
description: Use when an agent must independently inspect or operate one user-configured MySQL target through the fixed MySQL MCP surface, including mysql_execute_sql raw execution and the typed MySQL tools; policy, read-only environments and write confirmation are explicit optional gates (off by default).
compatibility: Agent Workflow Hub spec 1.0; requires the project-locked Python MySQL MCP dependencies and a user-managed, repository-external MySQL INI（可选 YAML policy）.
metadata:
  spec-version: "1.0"
  workflow-version: "0.1.0"
  display-name: "MySQL Operations"
  execution-modes: '["single-agent"]'
  no-multi-agent-fallback: "serial"
  multi-agent-consent: "not-applicable"
  multi-agent-write-policy: "main-agent-only"
  approval-owner: "main-agent"
  required-capabilities: '[]'
  config-templates: '{"main":"references/mysql.ini.example","policy":"references/mysql-policy.yaml.example","target":"references/mysql-environment.ini.example"}'
  config-requirements: '{"main":{"scope":"repository-external","required":true},"policy":{"scope":"repository-external","required":false},"target":{"scope":"repository-external","required":false}}'
  entrypoints: '{"mcp":"workflow-hub mysql-mcp <ABSOLUTE_INI>"}'
  roles: '["roles/mysql-operator.md"]'
  supported-hosts: '["codex","openclaw","claude-code","hermes","opencode"]'
---

# MySQL 操作

## 用途与触发条件

本工作流独立调用受控 MySQL stdio MCP，用于在用户明确授权的单个 MySQL 目标上进行元数据读取、查询、参数化 DML、受控事务、DDL 计划、幂等迁移，以及 `mysql_execute_sql` 的一条或多条标准 MySQL SQL 原样执行。本工作流只按用户明确提出的数据库任务运行，不会被其他工作流隐式调用。

每次调用必须给出用户指定的、已经存在的仓库外 INI 绝对路径，以及目标 schema/table、操作意图和预期结果。开始时先调用 `mysql_get_capabilities`，再进行 `metadata/read`：`mysql_list_schemas`、`mysql_list_tables`、`mysql_describe_table`、`mysql_read_query` 或 `mysql_explain_query`。只有读取到配置、可选策略和当前对象状态后，才可以评估任何写入意图。

数据库账号（登录后由服务器授予的权限）是唯一授权源：policy、read-only environment 与写前确认默认关闭，仅显式配置时收窄；Hub 不解析 SQL 做二次授权，也不默认追加安全门。

使用领域判断前，Agent 必须调用 `load_role_snapshot` 并使用 `roles/mysql-operator.md` 的当前内容与 digest。角色只提供数据安全、事务一致性和测试数据生命周期判断，不授予工具权限、不替代可选 YAML 策略或用户确认。

## 非目标

- 不提供 Shell、文件系统读写或客户端本地命令执行：`DELIMITER`、`SOURCE`、`\!` 等客户端命令没有对应服务器语句，一律明确拒绝。
- 不自动安装或升级 MySQL、驱动、数据库服务、浏览器/宿主映射，也不自动注册或改写 MCP 配置。
- 不把 policy、read-only environment、写前确认、参数化、单语句、WHERE、影响行数或 SQL 类型白名单变成默认强制门。
- 不猜测业务表、凭据、缺失连接字段、策略范围、确认结果或测试数据清理方式；不把 connection_string 或凭据写入响应、日志、审计输出或状态文件，也不回显密码、环境变量值、SQL 参数值或完整敏感行。
- 不签发、延长、替代或恢复其他工作流的确认回执。

## 输入

真实 INI 和其可选 YAML 策略必须保存在仓库外的私有、非链接目录；从 `references/mysql.ini.example`、`references/mysql-policy.yaml.example` 或 `references/mysql-environment.ini.example` 复制后替换全部示例值。INI 本身必须是绝对路径。`policy_file`、`ca_bundle` 和 `migrations_dir` 仅能相对于该 INI 的同一安全目录解析，且不得穿越或使用链接。

INI 支持 `[mysql]` 与 `[environment]` + `[target.mysql]` 双结构。`[mysql]` 声明目标名、环境、主机、端口、数据库、TLS、可选策略路径、只读环境列表和一种完整凭据来源；`[environment]` + `[target.mysql]` 提供环境名与连接字段，`database` 不是固定字段，缺失时必须由调用方在本次调用显式提供（`database` 或 `connection_string`），不猜测默认库、默认账号或默认环境。同一 INI 同时存在两种结构时以 `[mysql]` 为准并记录诊断；其它 section（如 `[target.redis]`）被容忍。

direct 凭据 `username + password` 与环境变量凭据 `username_env + password_env` 互斥：混用或任一半缺失均拒绝。`connection_string` 与账号/密码允许作为运行输入（调用参数），但只用于本次连接，不进入任何响应或日志；调用参数按字段覆盖配置（`connection_string` 整体优先，其次 host/port/database/username/password，最后 `max_result_rows`），合并结果仅内部使用。

YAML 是显式可选的收窄来源，不能扩大数据库账号权限，也不能代替账号做授权；未配置 policy 时服务正常启动，raw 与 typed 写均按数据库账号权限直接执行。配置后单条规则必须精确匹配 target、environment、action、schema、table、column、行数上限，以及适用时的 WHERE/主键、DDL 类别或迁移目录、ID 和账本表；歧义、过期或跨规则拼接均拒绝。

`read_only_environments` 对当前 `environment` 使用精确匹配，仅显式配置时生效。命中时是不可覆盖的硬只读门：所有 DML、事务写入、DDL 和迁移在创建连接、执行 SQL 或创建迁移账本前拒绝；策略允许、调用方参数或用户文字确认都不能绕过它。

## 输出与命名规则

默认只返回脱敏结构化摘要：目标与环境、策略决策（如配置 policy）、对象范围、请求指纹、行数、受控 diff、读后核验和下一步。不得输出密码、凭据引用对应的值、connection_string、完整 SQL 参数值或未授权列；返回的 `confirmation_id` 仅是当前会话的一次性服务绑定标识，不是密码学凭据。`mysql_execute_sql` 的每段 statement 返回独立结果：status 固定为 success/error/outcome_unknown/not_executed，结果集带 columns/rows/row_count/truncated，非结果语句返回 affected_rows。

除非用户明确要求保存，工作流不创建文件。获准保存的非敏感结果仅写入 `workflows/mysql-operations/outputs/<run-id>/`，文件名采用稳定 run ID，且不覆盖既有文件。

## 依赖和运行前检查

先验证 Hub 根目录和本工作流，再只读检查用户提供的绝对 INI：文件身份、目录非链接、INI/YAML 解析（policy 可选）、路径边界、TLS 选项、direct/env 凭据模式的完整性、迁移目录和账本配置。解析配置不读取或显示凭据值。

随后加载可选策略并执行 `mysql_get_capabilities`。没有真实用户提供的非生产 INI 时，只能进行 fake client / contract 验收，不能声称真实连接、metadata/read smoke 或任何写入已经通过。获得明确非生产 INI 后，真实 smoke 仍必须先做 metadata/read；DML、DDL 或迁移另需对应策略（如配置）和测试数据授权。

## 系统修改与权限影响

未配置 policy、read_only environment 与 require_confirmation 时，raw 与 typed 写均按数据库账号权限直接执行。`mysql_insert`、`mysql_update`、`mysql_delete` 和 `mysql_execute_transaction` 保留结构化输入特性：受限标识符、结构化列/值和参数化值；更新/删除在策略要求时必须包含 WHERE 和/或完整主键，影响行数超过上限时回滚。

policy、read-only environment 与写前确认默认关闭，仅显式配置时收窄：policy 规则 ALLOW 直接执行、DENY 拒绝、NEEDS_USER_CONFIRMATION 才挑战；`require_confirmation=true` 或确认规则命中时，写操作先返回 `needs_user_confirmation` 挑战：`confirmation_id`、精确请求指纹与完整脱敏操作摘要。调用方必须用完全相同的请求参数携带 `confirmation_id` 重放，服务重新计算指纹、复检策略并在打开写连接前原子消费一次；漂移、过期或复用都会使确认失效。`confirmation_id` 是当前会话的一次性服务绑定标识，不是密码学凭据，也不能作为 INI/YAML 配置项传入或覆盖只读硬门。

`mysql_execute_sql` 接受一条或多条标准 MySQL SQL 原样转发，正常支持多语句、CALL（含多结果集；OUT 参数通过 SQL 变量与后续 SELECT 在同一调用内获取）、DDL 与显式事务（BEGIN/COMMIT/ROLLBACK/SAVEPOINT 在同一调用内，默认 autocommit=true，不注入隐式 commit/rollback）；`params` 只允许配合单条语句，多语句与 params 同时提供返回 parameter_error 且不执行任何语句。`DELIMITER`、`SOURCE`、`\!` 等客户端命令明确拒绝。显式 policy 下，raw 无法表达的 CALL/DDL/事务返回 policy_not_applicable；多表/JOIN/CTE/SELECT* 等无法匹配单规则的 raw 语句按 DENY 或 policy_not_applicable 处理；默认无 policy 时不受影响、按账号权限直接执行。

`mysql_plan_migration` 只读；DDL 不假设可回滚。`mysql_apply_migration` 以固定迁移目录、迁移 ID、源摘要、schema 快照和账本表完成幂等判定与执行前后核验。账本不可用、源/快照变化或 ID/摘要冲突时只停止，不用“回滚 DDL”掩盖状态。

## 执行步骤

1. 校验用户范围、仓库外绝对 INI 路径和角色 snapshot；解析配置与可选策略，不读取或输出秘密。
2. 调用 `mysql_get_capabilities`，确认 14 个固定工具（13 个 typed 工具加 `mysql_execute_sql`）、配置摘要和限制状态；先执行 metadata/read，获取最小必要对象快照。
3. 对 typed 读取请求校验单语句、解析树、参数化占位、策略范围（如配置）、列白名单与分页/行数上限，再返回脱敏结果。
4. 对 DML 先读取当前快照并生成受控 diff，检查精确环境、规则（如配置）、WHERE/主键、行数上限和确认状态；仅当策略/`require_confirmation` 命中挑战时，以相同请求携带 `confirmation_id` 重放并原子消费后才执行事务并读后核验；未配置确认门时直接执行。
5. 对事务内每条结构化语句独立授权（如配置 policy）；失败或行数超限时回滚。连接中断或提交结果无法证明时返回 `outcome_unknown`。
6. 对迁移先用 `mysql_plan_migration` 读取脱敏 schema 摘要和计划指纹；`mysql_apply_migration` 仅当策略/`require_confirmation` 要求时才返回确认挑战，重放时在创建写连接前同时消费确认与已签发计划，再按账本、源摘要、schema 快照和策略执行并读取核验。

## 人工确认门

- Gate S：首次使用目标、仓库外 INI、策略范围、schema/table 或测试数据生命周期意图时，由用户确认范围；配置文件不替代用户意图。
- Gate P：YAML 为显式可选。配置后，可预授权完整匹配的读取和明确允许的非受限低风险操作；过期、歧义、越界或跨规则组合立即拒绝。未配置时按账号权限执行。
- Gate H：仅当显式配置的 policy 命中 NEEDS_USER_CONFIRMATION 或 `require_confirmation=true` 时，写操作、迁移先返回 `needs_user_confirmation` 挑战；以相同请求携带 `confirmation_id` 重放并原子消费后才执行，漂移、过期或复用立即失效。
- 命中精确 `read_only_environments` 的硬门优先于全部 Gate，且不可覆盖；该门仅显式配置时生效。

## 失败恢复

配置、路径、TLS、认证、策略、RBAC、网络或解析检查失败时，返回脱敏失败类别并停止；不替换凭据、不降低 TLS、不扩张策略、不尝试 Shell 变通方案。

写入、事务提交、DDL 或迁移遇到超时、断线或结果不可证明时标记 `outcome_unknown`（服务器明确报错时对应段为 error，其后段为 not_executed；客户端侧超时/断线时当前受影响语句及之后语句均为 outcome_unknown，不出现 not_executed）。只使用目标、schema/table、写前快照、请求指纹、迁移 ID 和源摘要进行只读对账；在对账完成且用户作出新决定前不自动重试。

## 重跑、幂等与覆盖策略

只读操作可在相同配置和策略下重跑。写入先读快照，事务使用受控行数和回滚；`outcome_unknown` 只允许对账，不允许自动重放，不自动重试。配置、策略、输出和用户已有对象均不自动覆盖。

迁移按账本中的 migration ID 与源 SHA-256 幂等：同 ID 且同摘要可报告已应用；同 ID 不同摘要拒绝。DDL 不假设可回滚，因此 schema 快照、计划指纹与读后核验是恢复依据，不以自动补偿为前提。

## 验收标准

仓库 contract 验证通过；固定工具面恰为 14 个工具：13 个 typed 工具（`mysql_get_capabilities`、`mysql_list_schemas`、`mysql_list_tables`、`mysql_describe_table`、`mysql_read_query`、`mysql_explain_query`、`mysql_insert`、`mysql_update`、`mysql_delete`、`mysql_execute_transaction`、`mysql_plan_migration`、`mysql_apply_migration`、`mysql_schema_snapshot`）加 `mysql_execute_sql`，没有 Shell、文件系统读写或客户端本地命令执行工具。

policy、read_only environment 与写前确认默认关闭，仅显式配置时收窄；未配置 policy 时 raw 与 typed 写均按数据库账号权限直接执行。多语句、CALL、DDL 与显式事务正常支持；`DELIMITER`/`SOURCE`/`\!` 明确拒绝；`connection_string` 与凭据不进响应/日志；`params` 仅限单条语句；`outcome_unknown` 不自动重试；结果超限截断并返回 truncated。显式配置 `read_only_environments` 时，受限环境的所有写操作都在连接和账本创建前拒绝；读取和写入均经完整单条规则授权（配置 policy 时）；行数超限回滚；未知结果只对账；DDL 通过计划与幂等账本管理且不声称可回滚。没有用户提供的真实非生产 INI 时，验收仅为 fake client / contract 验收。

## 清理方式

默认不清理仓库外 INI、YAML、CA、迁移目录、账本、数据库数据或宿主 MCP 映射。只有用户点名当前工作流内可重建输出的精确路径并进行第二次确认时，才按 Hub clean 规则执行 dry-run、路径复核与清理；不会用清理来回滚数据库状态。
