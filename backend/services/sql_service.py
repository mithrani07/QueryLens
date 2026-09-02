"""
services/sql_service.py

Validates LLM-generated SQL before it ever touches a real DuckDB
connection:
- Parses the statement with sqlglot to confirm it is syntactically valid.
- Blocks any destructive or non-read-only statement (DROP, DELETE, UPDATE,
  INSERT, ALTER, CREATE, TRUNCATE, GRANT, REVOKE, ATTACH, COPY, etc).
- Enforces single-statement execution (no stacked queries via `;`).
- Executes the validated query against a DuckDB in-memory context that
  has the relevant source tables registered (from an uploaded file or a
  materialized snapshot of PostgreSQL tables).

DuckDB itself is also opened with enable_external_access disabled: this
service never issues CREATE/INSERT/UPDATE/DELETE against the query
context, only SELECT, and the connection is additionally locked down so
that even DuckDB's built-in table functions (read_csv, read_parquet,
httpfs-backed remote reads, etc.) cannot reach the local filesystem or
network from within a query - a syntactically valid SELECT could
otherwise still exfiltrate data or read arbitrary files without ever
touching a forbidden AST node type.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from config import settings

logger = logging.getLogger("querylens.sql_service")

# Statement types that must never reach execution. Expressed both as
# sqlglot expression classes (structural check) and as a keyword blocklist
# (defense in depth against dialect quirks sqlglot might not classify the
# same way).
_FORBIDDEN_EXPRESSION_TYPES = (
    exp.Drop,
    exp.Delete,
    exp.Update,
    exp.Insert,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Grant,
    exp.AttachOption,
    exp.Command,  # catches ATTACH, DETACH, PRAGMA, COPY, EXPORT, etc.
    exp.Merge,
)

_FORBIDDEN_KEYWORDS = {
    "drop", "delete", "update", "insert", "alter", "create", "truncate",
    "grant", "revoke", "attach", "detach", "copy", "export", "import",
    "pragma", "vacuum", "checkpoint", "install", "load", "call", "merge",
    "replace", "set",
}

_ALLOWED_ROOT_TYPES = (exp.Select, exp.Union, exp.With, exp.Subquery)

_STATEMENT_SPLIT_PATTERN = re.compile(r";\s*\S")  # semicolon followed by more non-whitespace

# DuckDB connection hardening: disable all filesystem/network access from
# within any query run against this context. This is set at connect()
# time (not via a later SET) because enable_external_access is a
# lockable option in DuckDB - safest to fix it from the moment the
# connection is opened.
_DUCKDB_LOCKDOWN_CONFIG = {"enable_external_access": False}


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class SQLServiceError(Exception):
    """Base exception for sql_service failures."""


class SQLSyntaxError(SQLServiceError):
    """Raised when sqlglot cannot parse the SQL as valid."""


class ForbiddenStatementError(SQLServiceError):
    """Raised when the SQL contains a destructive or otherwise disallowed statement."""


class MultipleStatementsError(SQLServiceError):
    """Raised when more than one SQL statement is present (stacked queries)."""


class SQLExecutionError(SQLServiceError):
    """Raised when a syntactically valid, safe query fails to execute against DuckDB."""


# ----------------------------------------------------------------------
# Result models
# ----------------------------------------------------------------------
@dataclass
class ValidationResult:
    is_valid: bool
    normalized_sql: str
    dialect: str
    warnings: List[str] = field(default_factory=list)


@dataclass
class ExecutionResult:
    columns: List[str]
    rows: List[Dict[str, Any]]
    row_count: int
    truncated: bool
    sql_executed: str


def _strip_trailing_semicolon(sql: str) -> str:
    return sql.strip().rstrip(";").strip()


def check_single_statement(sql: str) -> None:
    """
    Rejects stacked queries (e.g. "SELECT 1; DROP TABLE x;"). A single
    trailing semicolon is fine; a semicolon followed by further non-
    whitespace content is treated as multiple statements.
    """
    if _STATEMENT_SPLIT_PATTERN.search(sql):
        raise MultipleStatementsError(
            "Only a single SQL statement is permitted per request."
        )


def check_forbidden_keywords(sql: str) -> None:
    """
    Defense-in-depth keyword scan run in addition to the AST-based check.
    Looks for forbidden keywords as whole words, case-insensitively,
    ignoring content inside single/double-quoted string literals so that
    a legitimate value like `WHERE notes = 'please delete later'` is not
    falsely flagged.
    """
    # Remove string literals before scanning for keywords.
    without_literals = re.sub(r"'(?:[^'\\]|\\.)*'", "''", sql)
    without_literals = re.sub(r'"(?:[^"\\]|\\.)*"', '""', without_literals)

    tokens = re.findall(r"[a-zA-Z_]+", without_literals.lower())
    hit = _FORBIDDEN_KEYWORDS.intersection(tokens)
    if hit:
        raise ForbiddenStatementError(
            f"Query contains disallowed keyword(s): {', '.join(sorted(hit))}. "
            f"Only read-only SELECT queries are permitted."
        )


def check_ast_is_read_only(parsed: exp.Expression) -> None:
    """
    Walks the parsed AST to confirm the root expression is a read-only
    query type, and that no forbidden expression type appears anywhere
    within it (e.g. a CTE that sneaks in a nested statement).
    """
    if not isinstance(parsed, _ALLOWED_ROOT_TYPES):
        raise ForbiddenStatementError(
            f"Only SELECT/WITH/UNION queries are permitted; "
            f"got statement of type '{type(parsed).__name__}'."
        )

    for node in parsed.walk():
        node_obj = node[0] if isinstance(node, tuple) else node
        if isinstance(node_obj, _FORBIDDEN_EXPRESSION_TYPES):
            raise ForbiddenStatementError(
                f"Query contains a disallowed operation: {type(node_obj).__name__}."
            )


def validate_sql(sql: str, dialect: str = "duckdb") -> ValidationResult:
    """
    Full validation pipeline for a candidate SQL string:
      1. Reject empty input.
      2. Reject multiple stacked statements.
      3. Parse with sqlglot (raises SQLSyntaxError on failure).
      4. Confirm the AST root is a read-only statement type.
      5. Run a keyword-level blocklist scan as defense in depth.

    Returns a ValidationResult with the sqlglot-normalized SQL string
    (consistently formatted, dialect-qualified) ready for execution.
    """
    if not sql or not sql.strip():
        raise SQLSyntaxError("SQL query is empty.")

    check_single_statement(sql)
    cleaned = _strip_trailing_semicolon(sql)

    try:
        parsed = sqlglot.parse_one(cleaned, read=dialect)
    except ParseError as exc:
        raise SQLSyntaxError(f"SQL failed to parse: {exc}") from exc
    except Exception as exc:
        raise SQLSyntaxError(f"Unexpected parsing error: {exc}") from exc

    if parsed is None:
        raise SQLSyntaxError("SQL parsed to an empty statement.")

    check_ast_is_read_only(parsed)
    check_forbidden_keywords(cleaned)

    warnings: List[str] = []
    if "select *" in cleaned.lower() or "select\n*" in cleaned.lower():
        warnings.append("Query selects all columns with '*'.")

    try:
        normalized_sql = parsed.sql(dialect=dialect, pretty=True)
    except Exception as exc:
        # Normalization is a convenience step; if it fails for any reason,
        # fall back to the cleaned original rather than blocking the query.
        logger.warning("SQL normalization via sqlglot failed, using raw SQL: %s", exc)
        normalized_sql = cleaned

    return ValidationResult(
        is_valid=True,
        normalized_sql=normalized_sql,
        dialect=dialect,
        warnings=warnings,
    )


def _build_duckdb_context(
    tables: Dict[str, pd.DataFrame],
) -> duckdb.DuckDBPyConnection:
    """
    Creates a fresh in-memory DuckDB connection with each provided
    dataframe registered under its table name. The connection is created
    fresh per request rather than reused globally, keeping requests fully
    isolated from one another, and is opened with enable_external_access
    disabled so that no query executed against it can read local files or
    reach the network via DuckDB's built-in table functions.
    """
    con = duckdb.connect(database=":memory:", config=_DUCKDB_LOCKDOWN_CONFIG)
    for table_name, df in tables.items():
        con.register(table_name, df)
    return con


def execute_query(
    sql: str,
    tables: Dict[str, pd.DataFrame],
    dialect: str = "duckdb",
    row_limit: Optional[int] = None,
) -> ExecutionResult:
    """
    Validates and then executes a SQL query against an in-memory DuckDB
    context built from the provided {table_name: DataFrame} mapping.

    `tables` should contain every table referenced in the schema shown to
    the LLM - for file uploads this is produced directly from parsed
    dataframes; for PostgreSQL sources, callers should materialize the
    relevant tables into dataframes first (e.g. via pandas.read_sql) since
    DuckDB here operates purely in-memory and does not proxy live
    PostgreSQL connections.

    Raises SQLServiceError subclasses on validation or execution failure.
    """
    if not tables:
        raise SQLExecutionError("No tables are available to execute the query against.")

    validation = validate_sql(sql, dialect=dialect)
    effective_limit = row_limit or settings.QUERY_ROW_LIMIT

    con = _build_duckdb_context(tables)
    try:
        try:
            result_df = con.execute(validation.normalized_sql).fetchdf()
        except duckdb.CatalogException as exc:
            raise SQLExecutionError(
                f"Query references a table or column that does not exist: {exc}"
            ) from exc
        except duckdb.BinderException as exc:
            raise SQLExecutionError(f"Query has a type or reference error: {exc}") from exc
        except duckdb.ParserException as exc:
            raise SQLExecutionError(f"DuckDB could not parse the query: {exc}") from exc
        except duckdb.Error as exc:
            raise SQLExecutionError(f"Query execution failed: {exc}") from exc
    finally:
        con.close()

    total_rows = len(result_df)
    truncated = total_rows > effective_limit
    if truncated:
        result_df = result_df.head(effective_limit)

    import json as _json
    records = _json.loads(result_df.to_json(orient="records", date_format="iso"))

    return ExecutionResult(
        columns=list(result_df.columns),
        rows=records,
        row_count=len(records),
        truncated=truncated,
        sql_executed=validation.normalized_sql,
    )


def validate_and_execute(
    sql: str,
    tables: Dict[str, pd.DataFrame],
    dialect: str = "duckdb",
    row_limit: Optional[int] = None,
) -> ExecutionResult:
    """
    Convenience wrapper combining validate_sql() + execute_query() in one
    call, which is the primary entry point routers should use after
    receiving generated SQL from llm_service.
    """
    return execute_query(sql, tables, dialect=dialect, row_limit=row_limit)