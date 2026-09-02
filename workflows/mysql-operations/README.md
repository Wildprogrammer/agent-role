# MySQL Operations MCP

`mysql-operations` 是 Agent Workflow Hub 的独立受控 MySQL stdio MCP 工作流。本工作流只按用户明确提出的数据库任务运行，不会被其他工作流隐式调用。

运行约束见 [SKILL.md](SKILL.md)。真实配置必须从仓库内样例复制到仓库外的私有、非链接目录；请勿提交真实 INI、YAML、CA 文件、迁移文件或凭据。

## 固定 MCP 工具

仅暴露 14 个固定工具：

| 类别 | 工具 | 作用 |
| --- | --- | --- |
| 能力 | `mysql_get_capabilities` | 返回脱敏配置、固定工具面和可执行限制。 |
| 元数据 | `mysql_list_schemas` | 列出当前有效 metadata 策略允许的 schema。 |
| 元数据 | `mysql_list_tables` | 列出一个允许 schema 内的受控表。 |
| 元数据 | `mysql_describe_table` | 返回策略允许列的元数据。 |
| 读取 | `mysql_read_query` | 执行一条解析并参数化的受限 SELECT，强制行数和分页上限。 |
| 读取 | `mysql_explain_query` | 返回一条受限 SELECT 的固定脱敏 EXPLAIN 投影。 |
| DML | `mysql_insert` | 结构化、参数化列值插入；仅当显式配置的确认门命中时才返回确认挑战。 |
| DML | `mysql_update` | 结构化、参数化列值更新；策略要求 WHERE/主键和行数上限，按需确认后执行。 |
| DML | `mysql_delete` | 结构化、参数化条件删除；策略要求 WHERE/主键和行数上限，按需确认后执行。 |
| DML | `mysql_execute_transaction` | 逐条结构化 DML 独立授权（如配置 policy）后，按需以一次性确认在单一事务中执行。 |
| 迁移 | `mysql_plan_migration` | 只读解析 DDL、生成 schema 摘要与计划指纹。 |
| 迁移 | `mysql_apply_migration` | 仅当策略/`require_confirmation` 要求时才返回确认挑战；重放时在创建写连接前消费确认与已签发计划，再按账本与 schema 快照幂等应用。 |
| 迁移 | `mysql_schema_snapshot` | 读取受控 schema 的脱敏结构摘要。 |
| 执行 | `mysql_execute_sql` | 一条或多条标准 MySQL SQL 原样转发；支持多语句、CALL（含多结果集）、DDL 与显式事务（BEGIN/COMMIT/ROLLBACK 同调用内，默认 autocommit）。可选 `params`（仅单条语句）、`connection_string`/host/port/database/username/password（仅本次连接使用，不进响应/日志）与 `max_result_rows`；响应含每段 status（success/error/outcome_unknown/not_executed）、result_sets（columns/rows/row_count/truncated）或 affected_rows、classification；可选 `confirmation_id` 仅在显式确认门命中时重放。 |

没有 Shell、宿主文件系统读写或客户端本地命令执行通道：`DELIMITER`、`SOURCE`、`\!` 等客户端命令没有服务器语句，明确拒绝。标准服务端 SQL（包括服务端文件导入导出语句）由 MySQL 账号权限决定。`mysql_read_query` 不是 raw 执行接口：它仅允许经 AST 解析的一条、单关系、参数化 SELECT；任意 DML/DDL 请使用 `mysql_execute_sql`。

## 配置与策略

将 [mysql.ini.example](references/mysql.ini.example)、[mysql-policy.yaml.example](references/mysql-policy.yaml.example)（可选）或 [mysql-environment.ini.example](references/mysql-environment.ini.example) 复制到仓库外相同的安全目录。传给 CLI 的 INI 必须是绝对路径；INI 内的策略、CA 和迁移路径仅能相对 INI 目录解析，不能越界或使用符号链接/目录联接。

INI 支持 `[mysql]` 与 `[environment]` + `[target.mysql]` 双结构，容忍其它 section。`[mysql]` 声明目标名、环境、主机、端口、数据库、TLS、可选策略路径、只读环境列表与凭据来源；环境式 INI 只提供环境名与 host/port，`database` 缺失时不猜测默认库，必须由调用方在本次调用显式提供（`database` 或 `connection_string`）。凭据二选一：direct `username + password`，或环境变量 `username_env + password_env`；混用或任一半缺失均拒绝。`connection_string` 与账号/密码允许作为运行输入，但只用于本次连接，不进入任何响应或日志。

policy、read-only environment 与写前确认默认关闭，仅显式配置时收窄：未配置 policy 时服务正常启动，raw 与 typed 写均按数据库账号权限直接执行；配置后 policy 规则 ALLOW 直接、DENY 拒绝、NEEDS_USER_CONFIRMATION 才挑战，raw 无法表达的 CALL/DDL/事务返回 policy_not_applicable，无法匹配单规则的 raw 语句按 DENY 或 policy_not_applicable 处理。`require_confirmation` 与 `max_result_rows` 均显式配置；策略从不扩大 MySQL 账户权限。

`read_only_environments` 使用配置环境名精确匹配，仅显式配置时生效。命中时所有 DML、事务写入、DDL 与迁移都是不可覆盖的硬只读门：它们在任何策略、确认、客户端连接、SQL 执行或迁移账本创建前被拒绝；策略、工具参数和用户文字确认均不能覆盖该门。

## 调用顺序与确认

先运行 `workflow-hub mysql-mcp <绝对 INI 路径>`，这只启动 stdio 服务，不注册或改写任何宿主 MCP 映射。每次会话先调用 `mysql_get_capabilities`，再进行 metadata/read。当前没有用户提供的真实非生产 INI；因此只能完成 fake client / contract 验收，未进行真实连接、metadata/read smoke、DML、DDL 或迁移，也不得将测试结果表述为真实环境验证。

写入前需要读取当前快照、生成受控 diff、验证单条策略规则（如配置）与行数约束；仅当显式配置的 policy 命中 NEEDS_USER_CONFIRMATION 或 `require_confirmation=true` 时，写操作先返回 `needs_user_confirmation` 挑战，携带相同请求参数与 `confirmation_id` 重放并原子消费后才执行一次；默认未配置确认门时按账号权限直接执行。`confirmation_id` 是当前会话的一次性服务绑定标识，不是密码学凭据，也不能作为配置字段传入或覆盖 `read_only_environments` 硬门。若无法证明最终结果，返回 `outcome_unknown`，随后只读对账，不自动重试。

事务失败和行数超限会回滚。DDL 不假设可回滚；迁移先计划、再基于 schema 摘要、源 SHA-256、迁移 ID 和账本执行幂等判定，最后读后核验。账本不可用或摘要冲突时停止。

## 代码结构

`src/agent_workflow_hub/mysql_mcp/` 是本工作流的受控实现：

- `config.py`：仓库外 INI 双结构（`[mysql]` / `[environment]` + `[target.mysql]`）、相对安全路径、TLS、可选 policy 与 direct/env 凭据模式校验；缺失连接字段返回可操作错误，不猜测。
- `policy.py`：可选 YAML 最小权限规则与精确只读硬门（仅显式配置时生效）。
- `sql_guard.py`：typed 只读查询的单语句、无副作用、参数化 AST 守卫。
- `client.py`：PyMySQL TLS 连接与受控 cursor 行为；connection_string/凭据仅内部使用。
- `executor.py`：raw 逐段执行、多结果集/CALL、状态矩阵（success/error/outcome_unknown/not_executed）、客户端命令拒绝与脱敏截断。
- `reads.py`：元数据、分页读取和 EXPLAIN 的策略检查（如配置）与脱敏。
- `writes.py`：结构化参数化 DML、事务、行数上限、回滚与读后核验。
- `migrations.py`：DDL 计划、schema 摘要、账本、幂等和未知结果对账。
- `server.py`：唯一的 MCP 工具注册点（14 个固定工具）；`mysql_execute_sql` 注册于此处，会话确认挑战由进程内 `SessionConfirmationStore` 生成，`confirmation_id` 是固定写工具与 `mysql_execute_sql` 的可选参数，不注册额外工具。

公开仓库可复现的工作流合约测试位于 `workflows/mysql-operations/tests/`。真实集成 smoke 始终需要用户提供的非生产 INI、明确授权和测试数据。
