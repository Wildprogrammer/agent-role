"""Raw MySQL statement execution with a per-statement status matrix."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from datetime import date
from decimal import Decimal

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .client import MySqlClientError, execute_connection, resolve_connection
from .config import ConfigError
from .models import (
    MYSQL_DML_ACTIONS,
    MySqlConnectionOverride,
    MySqlCredentials,
    MySqlErrorClassification,
    MySqlExecutionError,
    MySqlExecutionResult,
    MySqlOperation,
    MySqlResultSet,
    MySqlStatementResult,
    MySqlStatementStatus,
    MySqlTarget,
)

ConnectionFactory = Callable[[MySqlTarget, MySqlCredentials], AbstractContextManager[object]]

_READ_ONLY_KEYWORDS = frozenset({"SELECT", "SHOW", "DESC", "DESCRIBE", "EXPLAIN", "WITH"})
_CLIENT_COMMAND_KEYWORDS = frozenset({"DELIMITER", "SOURCE"})
_SERVER_LOCK_WAIT_TIMEOUT_CODE = 1205
_CONNECTION_LOST_CODES = frozenset({2006, 2013, 2055})
_EXECUTION_TIMEOUT_CODES = frozenset({3024})
_COMPOUND_END_KEYWORDS = frozenset({"IF", "LOOP", "REPEAT", "WHILE", "CASE"})


class MySqlExecutionService:
    """Execute one raw SQL batch on one connection and report per-statement status."""

    def __init__(
        self,
        *,
        target: MySqlTarget,
        credentials: MySqlCredentials,
        connection_factory: ConnectionFactory = execute_connection,
        default_max_result_rows: int = 100,
    ) -> None:
        if not isinstance(target, MySqlTarget) or not isinstance(credentials, MySqlCredentials):
            raise TypeError("target and credentials must be validated MySQL configuration")
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        if (
            not isinstance(default_max_result_rows, int)
            or isinstance(default_max_result_rows, bool)
            or default_max_result_rows < 1
        ):
            raise ValueError("default_max_result_rows must be a positive integer")
        self._target = target
        self._credentials = credentials
        self._connection_factory = connection_factory
        self._default_max_result_rows = default_max_result_rows

    def execute(
        self,
        sql: str,
        *,
        params: list[object] | tuple[object, ...] | None = None,
        max_result_rows: int | None = None,
        connection_override: MySqlConnectionOverride | None = None,
    ) -> MySqlExecutionResult:
        """Run one raw SQL batch in a single call on a single connection.

        Statements execute in order; each produces result_sets (including the
        CALL nextset loop, whose errors count against the current statement) or
        affected_rows. A server error marks the current statement error and all
        later statements not_executed. A client-side interruption during execute
        or nextset marks the current and later statements outcome_unknown with a
        timeout/connection classification. Nothing is retried automatically.
        """

        if not isinstance(sql, str) or not sql.strip():
            raise MySqlExecutionError(
                MySqlErrorClassification.PARAMETER_ERROR,
                "SQL must be a nonblank string",
            )
        statements = _split_statements(sql)
        if not statements:
            raise MySqlExecutionError(
                MySqlErrorClassification.PARAMETER_ERROR,
                "SQL must contain at least one statement",
            )
        if has_client_command(statements) is not None:
            raise MySqlExecutionError(
                MySqlErrorClassification.CLIENT_COMMAND,
                "MySQL client commands are not allowed",
            )
        if params is not None:
            if not isinstance(params, (list, tuple)) or isinstance(
                params,
                (str, bytes, bytearray),
            ):
                raise MySqlExecutionError(
                    MySqlErrorClassification.PARAMETER_ERROR,
                    "params must be a list or tuple of values",
                )
            if len(statements) != 1:
                raise MySqlExecutionError(
                    MySqlErrorClassification.PARAMETER_ERROR,
                    "params are only allowed with a single statement",
                )
            parameters = tuple(params)
        else:
            parameters = None
        if max_result_rows is not None:
            limit = max_result_rows
        elif (
            connection_override is not None
            and connection_override.max_result_rows is not None
        ):
            limit = connection_override.max_result_rows
        else:
            limit = self._default_max_result_rows
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise MySqlExecutionError(
                MySqlErrorClassification.PARAMETER_ERROR,
                "max_result_rows must be a positive integer",
            )
        try:
            merged_target, effective_credentials = resolve_connection(
                self._target,
                self._credentials,
                connection_override,
            )
        except ConfigError as exc:
            raise MySqlExecutionError(
                MySqlErrorClassification.CONFIGURATION,
                str(exc),
            ) from None
        try:
            with self._connection_factory(
                merged_target,
                effective_credentials,
            ) as connection:
                cursor = connection.cursor()
                try:
                    statement_results = self._run_statements(
                        cursor,
                        statements,
                        parameters,
                        limit,
                    )
                finally:
                    close = getattr(cursor, "close", None)
                    if callable(close):
                        close()
        except MySqlExecutionError:
            raise
        except MySqlClientError:
            raise MySqlExecutionError(
                MySqlErrorClassification.CONNECTION,
                "could not establish a MySQL execution connection",
            ) from None
        except Exception:
            raise MySqlExecutionError(
                MySqlErrorClassification.CONNECTION,
                "could not establish a MySQL execution connection",
            ) from None
        return MySqlExecutionResult(statements=statement_results)

    def _run_statements(
        self,
        cursor: object,
        statements: Sequence[str],
        parameters: tuple[object, ...] | None,
        limit: int,
    ) -> tuple[MySqlStatementResult, ...]:
        results: list[MySqlStatementResult] = []
        unknown_classification: MySqlErrorClassification | None = None
        for index, statement in enumerate(statements):
            if unknown_classification is not None:
                results.append(
                    MySqlStatementResult(
                        index=index,
                        status=MySqlStatementStatus.OUTCOME_UNKNOWN,
                        classification=unknown_classification,
                    )
                )
                continue
            step_parameters = parameters if len(statements) == 1 else None
            try:
                cursor.execute(statement, step_parameters)  # type: ignore[attr-defined]
                if cursor.description is not None:  # type: ignore[attr-defined]
                    result_sets = self._collect_result_sets(cursor, limit)
                    results.append(
                        MySqlStatementResult(
                            index=index,
                            status=MySqlStatementStatus.SUCCESS,
                            result_sets=result_sets,
                        )
                    )
                else:
                    results.append(
                        MySqlStatementResult(
                            index=index,
                            status=MySqlStatementStatus.SUCCESS,
                            affected_rows=_affected_rows(cursor),
                        )
                    )
            except Exception as exc:
                classification = _interruption_classification(exc)
                if classification is not None:
                    results.append(
                        MySqlStatementResult(
                            index=index,
                            status=MySqlStatementStatus.OUTCOME_UNKNOWN,
                            classification=classification,
                        )
                    )
                    unknown_classification = classification
                else:
                    results.append(
                        MySqlStatementResult(
                            index=index,
                            status=MySqlStatementStatus.ERROR,
                            classification=_server_error_classification(exc),
                            error=_server_error_text(exc),
                        )
                    )
                    for remaining in range(index + 1, len(statements)):
                        results.append(
                            MySqlStatementResult(
                                index=remaining,
                                status=MySqlStatementStatus.NOT_EXECUTED,
                            )
                        )
                    break
        return tuple(results)

    def _collect_result_sets(
        self,
        cursor: object,
        limit: int,
    ) -> tuple[MySqlResultSet, ...]:
        result_sets: list[MySqlResultSet] = []
        while True:
            rows = cursor.fetchall()  # type: ignore[attr-defined]
            safe_rows = _json_safe_rows(rows)
            kept = safe_rows[:limit]
            result_sets.append(
                MySqlResultSet(
                    columns=_column_names(cursor.description),  # type: ignore[attr-defined]
                    rows=tuple(kept),
                    row_count=len(kept),
                    truncated=len(safe_rows) > limit,
                )
            )
            more = cursor.nextset()  # type: ignore[attr-defined]
            if more is not True:
                return tuple(result_sets)


def has_client_command(statements: Sequence[str]) -> str | None:
    """Return the first mysql client command statement, or None when absent."""

    for statement in statements:
        keyword = _leading_keyword(statement)
        if keyword in _CLIENT_COMMAND_KEYWORDS or keyword.startswith("\\"):
            return statement
    return None


def is_read_only_batch(sql: str) -> bool:
    """Whether every statement is read-only by keyword and parsed AST shape.

    A WITH batch is only read-only when its final statement is a SELECT, and a
    SELECT with an INTO target (for example INTO OUTFILE) is a write.
    """

    statements = _split_statements(sql)
    if not statements:
        return False
    for statement in statements:
        if _leading_keyword(statement) not in _READ_ONLY_KEYWORDS:
            return False
        if not _ast_read_only_statement(statement):
            return False
    return True


def raw_statement_kinds(sql: str) -> tuple[str, ...]:
    """Return each statement's normalized leading keyword."""

    return tuple(_leading_keyword(statement) for statement in _split_statements(sql))


def _ast_read_only_statement(statement: str) -> bool:
    try:
        expressions = sqlglot.parse(statement, read="mysql")
    except SqlglotError:
        return False
    if len(expressions) != 1 or expressions[0] is None:
        return False
    expression = expressions[0]
    if isinstance(expression, exp.Select):
        return not any(isinstance(node, exp.Into) for node in expression.walk())
    return isinstance(expression, (exp.Show, exp.Describe))


def static_policy_operations(
    sql: str,
    target: MySqlTarget,
) -> tuple[MySqlOperation, ...] | None:
    """Map one raw batch to policy operations, or None when not expressible.

    SELECT maps to read, SHOW/DESC to metadata, EXPLAIN to explain, and
    INSERT/REPLACE, UPDATE, DELETE to insert, update, and delete with action,
    schemas, tables, and columns (unqualified tables use ``target.database``;
    DML carries ``estimated_rows=1``; DESC/DESCRIBE and table-scoped SHOW like
    SHOW COLUMNS/INDEX map to metadata even without explicit columns).
    CALL/DDL/transactions/SET, parse failures, and multi-table/JOIN/CTE/SELECT*
    shapes that cannot normalize to one rule scope return None, which callers
    map to policy_not_applicable. Callers without a policy skip this entirely
    and rely on account permissions.
    """

    if not isinstance(target, MySqlTarget) or not target.database:
        return None
    try:
        statements = _split_statements(sql)
        if not statements:
            return None
        operations: list[MySqlOperation] = []
        for statement in statements:
            operation = _static_policy_operation(statement, target)
            if operation is None:
                return None
            operations.append(operation)
        return tuple(operations)
    except Exception:
        return None


def _static_policy_operation(
    statement: str,
    target: MySqlTarget,
) -> MySqlOperation | None:
    keyword = _leading_keyword(statement)
    parse_text = statement
    if keyword == "REPLACE":
        # sqlglot falls back to Command for REPLACE; rewrite to INSERT so the
        # static table/column shape can be extracted.
        parse_text = "INSERT " + statement.lstrip()[len("REPLACE") :].lstrip()
    try:
        expressions = sqlglot.parse(parse_text, read="mysql")
    except SqlglotError:
        return None
    if len(expressions) != 1 or expressions[0] is None:
        return None
    expression = expressions[0]
    action = _static_action(expression, keyword)
    if action is None:
        return None
    if _has_unsupported_shape(expression):
        return None
    table_name, schema = _static_table_scope(expression, target)
    if table_name is None or schema is None:
        return None
    columns = _static_columns(expression)
    if not columns and action not in {"metadata", "read"}:
        return None
    return MySqlOperation(
        target=target.name,
        environment=target.environment,
        action=action,
        schemas=frozenset({schema}),
        tables=frozenset({table_name}),
        columns=frozenset(columns),
        estimated_rows=1 if action in MYSQL_DML_ACTIONS else 0,
        migration_id=None,
        where_columns=_where_columns(expression),
    )


def _static_action(expression: exp.Expression, keyword: str) -> str | None:
    if isinstance(expression, exp.Select):
        return "read"
    if isinstance(expression, exp.Show):
        return "metadata"
    if isinstance(expression, exp.Describe):
        return "explain" if keyword == "EXPLAIN" else "metadata"
    if isinstance(expression, exp.Insert):
        return "insert"
    if isinstance(expression, exp.Update):
        return "update"
    if isinstance(expression, exp.Delete):
        return "delete"
    return None


def _has_unsupported_shape(expression: exp.Expression) -> bool:
    return any(
        isinstance(node, (exp.Join, exp.Subquery, exp.CTE))
        for node in expression.walk()
    )


def _static_table_scope(
    expression: exp.Expression,
    target: MySqlTarget,
) -> tuple[str | None, str | None]:
    table_schemas: dict[str, str | None] = {
        node.name: node.db for node in expression.find_all(exp.Table) if node.name
    }
    if isinstance(expression, exp.Show):
        show_target = expression.args.get("target")
        if isinstance(show_target, exp.Identifier) and show_target.name:
            table_schemas.setdefault(show_target.name, None)
    if len(table_schemas) != 1:
        return None, None
    table_name = next(iter(table_schemas))
    schema = table_schemas[table_name] or target.database
    if not schema:
        return None, None
    return table_name, schema


def _static_columns(expression: exp.Expression) -> frozenset[str]:
    columns = {
        column.name for column in expression.find_all(exp.Column) if column.name
    }
    schema = expression.this if isinstance(expression, exp.Insert) else None
    if isinstance(schema, exp.Schema):
        columns.update(
            identifier.name
            for identifier in schema.expressions
            if isinstance(identifier, exp.Identifier) and identifier.name
        )
    return frozenset(columns)


def _where_columns(expression: exp.Expression) -> frozenset[str]:
    if not isinstance(expression, (exp.Update, exp.Delete)):
        return frozenset()
    where = expression.args.get("where")
    if where is None:
        return frozenset()
    return frozenset(
        column.name for column in where.find_all(exp.Column) if column.name
    )


def _split_statements(sql: str) -> list[str]:
    """Split SQL on semicolons outside quotes, comments, and BEGIN...END blocks.

    CREATE PROCEDURE/FUNCTION/TRIGGER/EVENT compound bodies keep their internal
    semicolons so each definition stays one statement; DELIMITER is rejected by
    the caller and is never needed for parsing here.
    """

    statements: list[str] = []
    current: list[str] = []
    compound_depth = 0
    compound_ddl = False
    first_keyword_seen = False
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character in {"'", '"', "`"}:
            current.append(character)
            index += 1
            while index < length:
                quoted = sql[index]
                current.append(quoted)
                index += 1
                if quoted == "\\" and index < length:
                    current.append(sql[index])
                    index += 1
                elif quoted == character:
                    if index < length and sql[index] == character:
                        current.append(sql[index])
                        index += 1
                    else:
                        break
            continue
        if character == "#":
            while index < length and sql[index] != "\n":
                current.append(sql[index])
                index += 1
            continue
        if (
            character == "-"
            and index + 2 < length
            and sql[index + 1] == "-"
            and sql[index + 2].isspace()
        ):
            while index < length and sql[index] != "\n":
                current.append(sql[index])
                index += 1
            continue
        if character == "/" and index + 1 < length and sql[index + 1] == "*":
            current.append(character)
            index += 1
            while index + 1 < length and not (
                sql[index] == "*" and sql[index + 1] == "/"
            ):
                current.append(sql[index])
                index += 1
            if index + 1 < length:
                current.append(sql[index])
                current.append(sql[index + 1])
                index += 2
            else:
                current.append(sql[index])
                index += 1
            continue
        if character.isalpha():
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            word = sql[index:end].upper()
            if not first_keyword_seen:
                first_keyword_seen = True
                compound_ddl = word == "CREATE"
            elif compound_ddl:
                if word == "BEGIN":
                    compound_depth += 1
                elif (
                    word == "END"
                    and _next_word(sql, end) not in _COMPOUND_END_KEYWORDS
                ):
                    compound_depth = max(0, compound_depth - 1)
            current.append(sql[index:end])
            index = end
            continue
        if character == ";":
            if not (compound_ddl and compound_depth > 0):
                statement = "".join(current).strip()
                if statement:
                    statements.append(statement)
                current = []
                compound_ddl = False
                compound_depth = 0
                first_keyword_seen = False
            else:
                current.append(character)
            index += 1
            continue
        current.append(character)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _next_word(sql: str, index: int) -> str:
    while index < len(sql) and (sql[index].isspace() or sql[index] == ";"):
        index += 1
    end = index
    while end < len(sql) and (sql[end].isalnum() or sql[end] == "_"):
        end += 1
    return sql[index:end].upper()


def _leading_keyword(statement: str) -> str:
    text = statement.strip()
    while text:
        if text.startswith("--"):
            newline = text.find("\n")
            if newline == -1:
                return ""
            text = text[newline:].strip()
            continue
        if text.startswith("#"):
            newline = text.find("\n")
            if newline == -1:
                return ""
            text = text[newline:].strip()
            continue
        if text.startswith("/*"):
            end = text.find("*/")
            if end == -1:
                return ""
            text = text[end + 2 :].strip()
            continue
        break
    if not text:
        return ""
    token = text.split(None, 1)[0]
    if token.startswith("\\"):
        return token.upper()
    return "".join(character for character in token if character.isalpha()).upper()


def _error_code(exc: Exception) -> int | None:
    args = getattr(exc, "args", None)
    if isinstance(args, tuple) and args:
        first = args[0]
        if isinstance(first, bool):
            return None
        if isinstance(first, int):
            return first
        if isinstance(first, str) and first.isdigit():
            return int(first)
    return None


def _server_error_classification(
    exc: Exception,
) -> MySqlErrorClassification | None:
    if _error_code(exc) == _SERVER_LOCK_WAIT_TIMEOUT_CODE:
        return MySqlErrorClassification.TIMEOUT
    return None


def _server_error_text(exc: Exception) -> str:
    code = _error_code(exc)
    if code is None:
        return "error unknown"
    return f"error {code}"


def _interruption_classification(
    exc: Exception,
) -> MySqlErrorClassification | None:
    if isinstance(exc, TimeoutError):
        return MySqlErrorClassification.TIMEOUT
    if isinstance(exc, (ConnectionError, OSError)):
        return MySqlErrorClassification.CONNECTION
    code = _error_code(exc)
    if code in _CONNECTION_LOST_CODES:
        return MySqlErrorClassification.CONNECTION
    if code in _EXECUTION_TIMEOUT_CODES:
        return MySqlErrorClassification.TIMEOUT
    return None


def _json_safe_rows(rows: object) -> tuple[dict[str, object], ...]:
    if not isinstance(rows, (list, tuple)):
        return ()
    return tuple(
        _json_safe(dict(row)) for row in rows if isinstance(row, dict)
    )


def _json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="backslashreplace")
    return value


def _column_names(description: object) -> tuple[str, ...]:
    if description is None:
        return ()
    names: list[str] = []
    for column in description:
        if isinstance(column, (tuple, list)):
            name = column[0] if column else None
        else:
            name = column
        if isinstance(name, bytes):
            name = name.decode("utf-8", errors="replace")
        names.append(str(name))
    return tuple(names)


def _affected_rows(cursor: object) -> int | None:
    rowcount = getattr(cursor, "rowcount", None)
    if isinstance(rowcount, int) and not isinstance(rowcount, bool):
        return rowcount
    return None
