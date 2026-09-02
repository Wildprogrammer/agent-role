"""Fail-closed SQL AST validation for the fixed MySQL read-query surface."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError


class SqlGuardError(ValueError):
    """A submitted SQL statement is malformed or outside the read-only surface."""


@dataclass(frozen=True)
class QueryAnalysis:
    """Value-free reference facts extracted from one validated read statement."""

    statement_type: str
    schemas: frozenset[str]
    tables: frozenset[str]
    columns: frozenset[str]
    parameter_count: int


_SUPPORTED_DIALECT = "mysql"
_TABLE_SCOPED_SHOW_KINDS = frozenset({"COLUMNS", "CREATE TABLE", "INDEX", "KEYS"})
_FORBIDDEN_NODE_TYPES = (
    exp.DML,
    exp.DDL,
    exp.Command,
    exp.Alter,
    exp.Drop,
    exp.TruncateTable,
    exp.Kill,
    exp.Commit,
    exp.Rollback,
    exp.Set,
    exp.Transaction,
    exp.Use,
    exp.Pragma,
    exp.Lock,
    exp.Into,
    exp.PropertyEQ,
    exp.Parameter,
    exp.SessionParameter,
    exp.Var,
)


def analyze_read_query(
    sql: str,
    *,
    parameters: Sequence[object] = (),
    dialect: str = _SUPPORTED_DIALECT,
) -> QueryAnalysis:
    """Parse one parameterized MySQL read statement without executing it.

    PyMySQL's ``%s`` markers are converted only to sqlglot's value-free ``?`` AST
    markers. Parameter values are never interpolated, logged, or otherwise used.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise SqlGuardError("SQL must be a nonblank string")
    if dialect != _SUPPORTED_DIALECT:
        raise SqlGuardError("only the mysql dialect is supported")
    if not isinstance(parameters, Sequence) or isinstance(
        parameters,
        (str, bytes, bytearray),
    ):
        raise SqlGuardError("parameters must be a sequence")

    normalized_sql, parameter_count, first_keyword = _normalize_placeholders(sql)
    if parameter_count != len(parameters):
        raise SqlGuardError("SQL parameter count does not match the supplied values")
    try:
        expressions = sqlglot.parse(normalized_sql, read=_SUPPORTED_DIALECT)
    except SqlglotError:
        raise SqlGuardError("SQL could not be parsed as MySQL") from None
    if len(expressions) != 1 or expressions[0] is None:
        raise SqlGuardError("exactly one SQL statement is required")

    expression = expressions[0]
    statement_type = _read_statement_type(expression, first_keyword)
    if len(list(expression.find_all(exp.Placeholder))) != parameter_count:
        raise SqlGuardError("SQL contains unsupported parameter markers")
    _reject_side_effects(expression)
    _reject_multiple_relation_sources(expression)
    _reject_ambiguous_column_scope(expression)
    return _collect_references(expression, statement_type, parameter_count)


def _read_statement_type(expression: exp.Expression, first_keyword: str) -> str:
    if isinstance(expression, exp.Select):
        return "select"
    if isinstance(expression, exp.Show):
        if first_keyword != "SHOW":
            raise SqlGuardError("statement is not read-only")
        return "show"
    if isinstance(expression, exp.Describe):
        if first_keyword == "EXPLAIN":
            if not isinstance(expression.this, exp.Select):
                raise SqlGuardError("EXPLAIN only accepts a SELECT target")
            return "explain"
        if first_keyword in {"DESC", "DESCRIBE"}:
            if not isinstance(expression.this, exp.Table):
                raise SqlGuardError("DESCRIBE only accepts a table target")
            return "describe"
    raise SqlGuardError("statement is not read-only")


def _reject_side_effects(expression: exp.Expression) -> None:
    if isinstance(expression, exp.Show) and expression.args.get("into_outfile") is not None:
        raise SqlGuardError("file export is not allowed")
    for node in expression.walk():
        if isinstance(node, _FORBIDDEN_NODE_TYPES):
            raise SqlGuardError("SQL contains a side-effecting operation")
        # sqlglot represents arbitrary MySQL functions, including stored functions
        # and LOAD_FILE, as Anonymous. Their behavior cannot be proven read-only.
        if isinstance(node, exp.Anonymous):
            raise SqlGuardError("SQL contains an unverified function call")


def _reject_ambiguous_column_scope(expression: exp.Expression) -> None:
    for node in expression.walk():
        if isinstance(node, exp.Star):
            raise SqlGuardError("SQL wildcard columns are not allowed")
        if isinstance(node, exp.Join) and (
            node.args.get("using") is not None
            or str(node.args.get("method", "")).upper() == "NATURAL"
        ):
            raise SqlGuardError("SQL join column scope is ambiguous")


def _reject_multiple_relation_sources(expression: exp.Expression) -> None:
    """Allow one query block over one direct table source only."""

    relation_nodes = (exp.Join, exp.Subquery, exp.CTE)
    if any(isinstance(node, relation_nodes) for node in expression.walk()):
        raise SqlGuardError("SQL may only reference one direct table source")
    if sum(1 for _ in expression.find_all(exp.Select)) > 1:
        raise SqlGuardError("SQL may only reference one direct table source")


def _collect_references(
    expression: exp.Expression,
    statement_type: str,
    parameter_count: int,
) -> QueryAnalysis:
    cte_names = {
        cte.alias_or_name
        for cte in expression.find_all(exp.CTE)
        if cte.alias_or_name
    }
    table_references = tuple(
        table
        for table in expression.find_all(exp.Table)
        if table.name
        and not (
            table.name in cte_names and not table.db and not table.catalog
        )
    )
    schemas = {table.db for table in table_references if table.db}
    tables = {table.name for table in table_references}
    if isinstance(expression, exp.Show):
        show_database = expression.args.get("db")
        if isinstance(show_database, exp.Identifier):
            schemas.add(show_database.name)
        if str(expression.this).upper() in _TABLE_SCOPED_SHOW_KINDS:
            show_target = expression.args.get("target")
            if isinstance(show_target, exp.Identifier):
                tables.add(show_target.name)
    return QueryAnalysis(
        statement_type=statement_type,
        schemas=frozenset(schemas),
        tables=frozenset(tables),
        columns=frozenset(column.name for column in expression.find_all(exp.Column)),
        parameter_count=parameter_count,
    )


def _normalize_placeholders(sql: str) -> tuple[str, int, str]:
    """Return parser-safe SQL, the PyMySQL marker count, and the first keyword.

    This small lexer deliberately leaves quoted literals, identifiers, and comments
    untouched so a textual ``%s`` there is never mistaken for a bound value.
    """

    output: list[str] = []
    parameter_count = 0
    first_keyword = ""
    index = 0
    length = len(sql)
    while index < length:
        character = sql[index]
        if character == '"':
            raise SqlGuardError("double-quoted SQL input is not allowed")
        if character in {"'", "`"}:
            index = _copy_quoted(sql, index, output)
            continue
        if character == "#":
            index = _copy_line_comment(sql, index, output)
            continue
        if (
            character == "-"
            and index + 2 < length
            and sql[index + 1] == "-"
            and sql[index + 2].isspace()
        ):
            index = _copy_line_comment(sql, index, output)
            continue
        if character == "/" and index + 1 < length and sql[index + 1] == "*":
            if _is_mysql_active_comment(sql, index):
                raise SqlGuardError(
                    "MySQL executable comments and optimizer hints are not allowed"
                )
            index = _copy_block_comment(sql, index, output)
            continue
        if character == "%" and index + 1 < length and sql[index + 1] == "s":
            output.append("?")
            parameter_count += 1
            index += 2
            continue
        if not first_keyword and character.isalpha():
            end = index + 1
            while end < length and (sql[end].isalnum() or sql[end] == "_"):
                end += 1
            first_keyword = sql[index:end].upper()
        output.append(character)
        index += 1
    if not first_keyword:
        raise SqlGuardError("SQL must contain a statement keyword")
    return "".join(output), parameter_count, first_keyword


def _copy_quoted(sql: str, index: int, output: list[str]) -> int:
    quote = sql[index]
    output.append(quote)
    index += 1
    while index < len(sql):
        character = sql[index]
        if character == "\\":
            raise SqlGuardError("backslash-escaped SQL input is not allowed")
        output.append(character)
        index += 1
        if character == quote:
            if index < len(sql) and sql[index] == quote:
                output.append(sql[index])
                index += 1
            else:
                return index
    raise SqlGuardError("SQL contains an unterminated quoted value")


def _copy_line_comment(sql: str, index: int, output: list[str]) -> int:
    end = sql.find("\n", index)
    if end == -1:
        output.append(sql[index:])
        return len(sql)
    output.append(sql[index : end + 1])
    return end + 1


def _copy_block_comment(sql: str, index: int, output: list[str]) -> int:
    end = sql.find("*/", index + 2)
    if end == -1:
        raise SqlGuardError("SQL contains an unterminated comment")
    output.append(sql[index : end + 2])
    return end + 2


def _is_mysql_active_comment(sql: str, index: int) -> bool:
    return (
        index + 2 < len(sql)
        and sql[index + 2] in {"!", "+"}
        or index + 3 < len(sql)
        and sql[index + 2 : index + 4] == "M!"
    )
