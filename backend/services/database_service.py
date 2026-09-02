"""
services/database_service.py

Handles connections to remote PostgreSQL databases:
- Validates and parses connection strings
- Tests connectivity
- Extracts table/column schema and metadata (row counts, sample rows)

This module is intentionally read-only: it never executes anything other
than metadata queries and the final validated SELECT built elsewhere.
psycopg2 connections are always opened in autocommit mode with statement
timeouts to avoid hanging or accidentally holding locks.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras

logger = logging.getLogger("querylens.database_service")

# Statement timeout (ms) applied to every connection to prevent runaway
# metadata or preview queries from hanging the request.
STATEMENT_TIMEOUT_MS = 10_000
CONNECT_TIMEOUT_SECONDS = 8
SAMPLE_ROW_COUNT = 3
MAX_TABLES_INSPECTED = 50


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class DatabaseServiceError(Exception):
    """Base exception for database_service failures."""


class InvalidConnectionStringError(DatabaseServiceError):
    """Raised when a connection string is malformed or uses a disallowed scheme."""


class DatabaseConnectionError(DatabaseServiceError):
    """Raised when a connection attempt to PostgreSQL fails."""


class SchemaExtractionError(DatabaseServiceError):
    """Raised when schema metadata cannot be retrieved."""


# ----------------------------------------------------------------------
# Data models
# ----------------------------------------------------------------------
@dataclass
class ColumnInfo:
    name: str
    type: str
    is_nullable: bool = True
    is_primary_key: bool = False


@dataclass
class TableSchema:
    table_name: str
    schema_name: str
    columns: List[ColumnInfo]
    row_count: Optional[int]
    sample_rows: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table_name": self.table_name,
            "schema_name": self.schema_name,
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "is_nullable": c.is_nullable,
                    "is_primary_key": c.is_primary_key,
                }
                for c in self.columns
            ],
            "row_count": self.row_count,
            "sample_rows": self.sample_rows,
        }


@dataclass
class DatabaseConnectionInfo:
    host: str
    port: int
    database: str
    username: str
    # dsn is kept internally for opening connections but is never returned
    # to the client / included in logs or exceptions.
    dsn: str = field(repr=False)

    def to_dict(self) -> Dict[str, Any]:
        """Safe-to-expose summary that never includes credentials."""
        return {
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username,
        }


ALLOWED_SCHEMES = {"postgres", "postgresql"}
_DSN_CREDENTIAL_PATTERN = re.compile(r"://[^:]+:[^@]+@")


def _redact_dsn(dsn: str) -> str:
    """Redacts the password portion of a DSN for safe logging."""
    return _DSN_CREDENTIAL_PATTERN.sub("://***:***@", dsn)


def parse_connection_string(connection_string: str) -> DatabaseConnectionInfo:
    """
    Validates the shape of a PostgreSQL connection string and extracts
    non-sensitive connection metadata. Does not attempt a network
    connection - use test_connection() for that.
    """
    if not connection_string or not connection_string.strip():
        raise InvalidConnectionStringError("Connection string must not be empty.")

    connection_string = connection_string.strip()

    try:
        parsed = urlparse(connection_string)
    except ValueError as exc:
        raise InvalidConnectionStringError(f"Malformed connection string: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidConnectionStringError(
            "Connection string must use the 'postgresql://' or 'postgres://' scheme."
        )

    if not parsed.hostname:
        raise InvalidConnectionStringError("Connection string is missing a host.")

    database = (parsed.path or "").lstrip("/")
    if not database:
        raise InvalidConnectionStringError("Connection string is missing a database name.")

    if not parsed.username:
        raise InvalidConnectionStringError("Connection string is missing a username.")

    return DatabaseConnectionInfo(
        host=parsed.hostname,
        port=parsed.port or 5432,
        database=database,
        username=parsed.username,
        dsn=connection_string,
    )


@contextmanager
def _open_connection(dsn: str) -> Generator[psycopg2.extensions.connection, None, None]:
    """
    Opens a psycopg2 connection with a connect timeout and a per-session
    statement timeout, always closing the connection on exit. Autocommit
    is enabled since only read-only metadata/SELECT queries run here.
    """
    conn = None
    try:
        conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_SECONDS)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
        yield conn
    except psycopg2.OperationalError as exc:
        raise DatabaseConnectionError(
            f"Could not connect to database: {_clean_pg_error(exc)}"
        ) from exc
    except psycopg2.Error as exc:
        raise DatabaseServiceError(f"Database error: {_clean_pg_error(exc)}") from exc
    finally:
        if conn is not None:
            conn.close()


def _clean_pg_error(exc: Exception) -> str:
    """Strips newlines from psycopg2 error messages for cleaner display/logging."""
    return " ".join(str(exc).split())


def test_connection(connection_string: str) -> DatabaseConnectionInfo:
    """
    Validates the connection string format, then attempts an actual
    connection and runs `SELECT 1` to confirm the database is reachable
    and credentials are valid. Returns connection metadata on success.
    """
    conn_info = parse_connection_string(connection_string)

    logger.info("Testing connection to %s", _redact_dsn(connection_string))
    try:
        with _open_connection(conn_info.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
    except DatabaseServiceError:
        raise
    except Exception as exc:
        raise DatabaseConnectionError(f"Unexpected connection failure: {exc}") from exc

    logger.info("Connection to %s succeeded", _redact_dsn(connection_string))
    return conn_info


def _fetch_tables(cur, schema_filter: str = "public") -> List[str]:
    cur.execute(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = %s
          AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """,
        (schema_filter,),
    )
    return [row[0] for row in cur.fetchall()]


def _fetch_primary_keys(cur, schema_filter: str, table_name: str) -> set:
    cur.execute(
        """
        SELECT kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name
         AND tc.table_schema = kcu.table_schema
        WHERE tc.constraint_type = 'PRIMARY KEY'
          AND tc.table_schema = %s
          AND tc.table_name = %s
        """,
        (schema_filter, table_name),
    )
    return {row[0] for row in cur.fetchall()}


def _fetch_columns(cur, schema_filter: str, table_name: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema_filter, table_name),
    )
    return [
        {"name": row[0], "type": row[1], "is_nullable": row[2] == "YES"}
        for row in cur.fetchall()
    ]


def _quote_ident(identifier: str) -> str:
    """Safely double-quotes a PostgreSQL identifier for use in generated SQL."""
    return '"' + identifier.replace('"', '""') + '"'


def get_schema(
    connection_string: str,
    schema_filter: str = "public",
    include_sample_rows: bool = True,
    include_row_counts: bool = True,
) -> List[TableSchema]:
    """
    Connects to the target PostgreSQL database and extracts full schema
    metadata (columns, types, primary keys) for every base table in the
    given schema, optionally including approximate row counts and a
    handful of sample rows per table for LLM context.
    """
    conn_info = parse_connection_string(connection_string)

    tables_schema: List[TableSchema] = []

    try:
        with _open_connection(conn_info.dsn) as conn:
            with conn.cursor() as cur:
                table_names = _fetch_tables(cur, schema_filter)

            if not table_names:
                raise SchemaExtractionError(
                    f"No base tables found in schema '{schema_filter}'."
                )

            if len(table_names) > MAX_TABLES_INSPECTED:
                logger.warning(
                    "Schema '%s' has %d tables; truncating to first %d",
                    schema_filter, len(table_names), MAX_TABLES_INSPECTED,
                )
                table_names = table_names[:MAX_TABLES_INSPECTED]

            for table_name in table_names:
                with conn.cursor() as cur:
                    columns_raw = _fetch_columns(cur, schema_filter, table_name)
                    pk_columns = _fetch_primary_keys(cur, schema_filter, table_name)

                columns = [
                    ColumnInfo(
                        name=c["name"],
                        type=c["type"],
                        is_nullable=c["is_nullable"],
                        is_primary_key=c["name"] in pk_columns,
                    )
                    for c in columns_raw
                ]

                row_count: Optional[int] = None
                if include_row_counts:
                    try:
                        with conn.cursor() as cur:
                            qualified = f"{_quote_ident(schema_filter)}.{_quote_ident(table_name)}"
                            cur.execute(f"SELECT COUNT(*) FROM {qualified}")
                            row_count = cur.fetchone()[0]
                    except psycopg2.Error as exc:
                        logger.warning(
                            "Row count failed for table %s: %s", table_name, _clean_pg_error(exc)
                        )

                sample_rows: List[Dict[str, Any]] = []
                if include_sample_rows:
                    try:
                        with conn.cursor(
                            cursor_factory=psycopg2.extras.RealDictCursor
                        ) as cur:
                            qualified = f"{_quote_ident(schema_filter)}.{_quote_ident(table_name)}"
                            cur.execute(f"SELECT * FROM {qualified} LIMIT {SAMPLE_ROW_COUNT}")
                            sample_rows = [
                                {k: _json_safe(v) for k, v in row.items()}
                                for row in cur.fetchall()
                            ]
                    except psycopg2.Error as exc:
                        logger.warning(
                            "Sample rows failed for table %s: %s", table_name, _clean_pg_error(exc)
                        )

                tables_schema.append(
                    TableSchema(
                        table_name=table_name,
                        schema_name=schema_filter,
                        columns=columns,
                        row_count=row_count,
                        sample_rows=sample_rows,
                    )
                )

    except DatabaseServiceError:
        raise
    except Exception as exc:
        raise SchemaExtractionError(f"Failed to extract schema: {exc}") from exc

    return tables_schema


def _json_safe(value: Any) -> Any:
    """Converts common non-JSON-serializable PostgreSQL types to safe equivalents."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Decimal, datetime, date, time, UUID, memoryview, etc.
    try:
        import datetime
        import decimal
        import uuid as uuid_module

        if isinstance(value, decimal.Decimal):
            return float(value)
        if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
            return value.isoformat()
        if isinstance(value, uuid_module.UUID):
            return str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return str(bytes(value))
    except Exception:
        pass
    return str(value)