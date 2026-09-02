"""
services/schema_service.py

Combines schema information coming from either file_service (uploaded
CSV/Excel/JSON) or database_service (remote PostgreSQL) into a single,
normalized structure. That structure is exactly what prompts.py's
format_full_schema() / build_user_prompt() expect:

    [
        {
            "table_name": str,
            "columns": [{"name": str, "type": str}, ...],
            "row_count": int | None,
            "sample_rows": [dict, ...],
        },
        ...
    ]

This module has no knowledge of HTTP, LLMs, or SQL execution - it is a
pure data-shaping layer sitting between the source-specific services and
prompts.py.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Union

from services import database_service, file_service

logger = logging.getLogger("querylens.schema_service")

SchemaSourceType = Literal["file", "database"]


class SchemaServiceError(Exception):
    """Base exception for schema_service failures."""


class EmptySchemaError(SchemaServiceError):
    """Raised when no tables are available to build a schema context from."""


class UnknownSourceError(SchemaServiceError):
    """Raised when an unrecognized schema source type is requested."""


@dataclass
class NormalizedTable:
    """A single table normalized to the shape prompts.py expects."""
    table_name: str
    columns: List[Dict[str, str]]
    row_count: Optional[int]
    sample_rows: List[Dict[str, Any]]
    source_type: SchemaSourceType
    source_label: str  # e.g. filename or "database"

    def to_prompt_dict(self) -> Dict[str, Any]:
        """Returns the exact shape consumed by prompts.format_full_schema()."""
        payload: Dict[str, Any] = {
            "table_name": self.table_name,
            "columns": self.columns,
            "sample_rows": self.sample_rows,
        }
        if self.row_count is not None:
            payload["row_count"] = self.row_count
        return payload


@dataclass
class SchemaContext:
    """
    The full combined schema context for a query session: every table
    available from every active source (one or more uploaded files and/or
    one connected database).
    """
    tables: List[NormalizedTable]

    @property
    def is_empty(self) -> bool:
        return len(self.tables) == 0

    @property
    def table_names(self) -> List[str]:
        return [t.table_name for t in self.tables]

    def to_prompt_list(self) -> List[Dict[str, Any]]:
        """Returns the list-of-dicts shape prompts.py functions expect."""
        return [t.to_prompt_dict() for t in self.tables]

    def to_dict(self) -> Dict[str, Any]:
        """A client-facing summary (e.g. for a schema-preview API response)."""
        return {
            "table_count": len(self.tables),
            "tables": [
                {
                    "table_name": t.table_name,
                    "source_type": t.source_type,
                    "source_label": t.source_label,
                    "row_count": t.row_count,
                    "columns": t.columns,
                }
                for t in self.tables
            ],
        }

    def find_table(self, table_name: str) -> Optional[NormalizedTable]:
        return next((t for t in self.tables if t.table_name == table_name), None)


def _resolve_name_collisions(tables: List[NormalizedTable]) -> List[NormalizedTable]:
    """
    Ensures every table_name in the final context is unique (SQL against
    DuckDB requires distinct table names). Colliding names are suffixed
    with an incrementing counter, e.g. `sales`, `sales_2`, `sales_3`.
    """
    seen: Dict[str, int] = {}
    resolved: List[NormalizedTable] = []
    for table in tables:
        base_name = table.table_name
        if base_name not in seen:
            seen[base_name] = 1
            resolved.append(table)
        else:
            seen[base_name] += 1
            new_name = f"{base_name}_{seen[base_name]}"
            logger.info("Resolved duplicate table name '%s' -> '%s'", base_name, new_name)
            table.table_name = new_name
            resolved.append(table)
    return resolved


def from_file_record(record: "file_service.UploadedFileRecord") -> List[NormalizedTable]:
    """Converts an UploadedFileRecord's tables into NormalizedTable objects."""
    normalized: List[NormalizedTable] = []
    for table in record.tables:
        normalized.append(
            NormalizedTable(
                table_name=table.table_name,
                columns=[{"name": c.name, "type": c.type} for c in table.columns],
                row_count=table.row_count,
                sample_rows=table.sample_rows,
                source_type="file",
                source_label=record.original_filename,
            )
        )
    return normalized


def from_database_schema(
    tables: List["database_service.TableSchema"],
    source_label: str = "database",
) -> List[NormalizedTable]:
    """Converts database_service TableSchema objects into NormalizedTable objects."""
    normalized: List[NormalizedTable] = []
    for table in tables:
        normalized.append(
            NormalizedTable(
                table_name=table.table_name,
                columns=[{"name": c.name, "type": c.type} for c in table.columns],
                row_count=table.row_count,
                sample_rows=table.sample_rows,
                source_type="database",
                source_label=source_label,
            )
        )
    return normalized


def build_schema_context(
    file_ids: Optional[List[str]] = None,
    connection_string: Optional[str] = None,
    db_schema_filter: str = "public",
) -> SchemaContext:
    """
    Builds a unified SchemaContext from any combination of uploaded file
    ids and/or a live database connection string. At least one source
    must be provided and yield at least one table, or EmptySchemaError
    is raised.
    """
    all_tables: List[NormalizedTable] = []

    if file_ids:
        for file_id in file_ids:
            try:
                record = file_service.get_upload(file_id)
            except file_service.FileNotFoundInStoreError as exc:
                raise SchemaServiceError(str(exc)) from exc
            all_tables.extend(from_file_record(record))

    if connection_string:
        try:
            db_tables = database_service.get_schema(
                connection_string, schema_filter=db_schema_filter
            )
        except database_service.DatabaseServiceError as exc:
            raise SchemaServiceError(str(exc)) from exc
        all_tables.extend(from_database_schema(db_tables))

    if not all_tables:
        raise EmptySchemaError(
            "No schema could be built - provide at least one uploaded file "
            "or a valid database connection with at least one table."
        )

    all_tables = _resolve_name_collisions(all_tables)
    return SchemaContext(tables=all_tables)


def build_schema_context_from_source(
    source_type: SchemaSourceType,
    *,
    file_id: Optional[str] = None,
    connection_string: Optional[str] = None,
    db_schema_filter: str = "public",
) -> SchemaContext:
    """
    Convenience wrapper for the common single-source case driven directly
    by a router (e.g. "use this one uploaded file" or "use this one DB").
    """
    if source_type == "file":
        if not file_id:
            raise SchemaServiceError("file_id is required when source_type is 'file'.")
        return build_schema_context(file_ids=[file_id])

    if source_type == "database":
        if not connection_string:
            raise SchemaServiceError(
                "connection_string is required when source_type is 'database'."
            )
        return build_schema_context(
            connection_string=connection_string, db_schema_filter=db_schema_filter
        )

    raise UnknownSourceError(f"Unknown schema source type: '{source_type}'")