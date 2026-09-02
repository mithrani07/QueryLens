"""
services/file_service.py

Handles everything related to uploaded data files:
- Persisting uploads (CSV, Excel, JSON) to disk under UPLOAD_DIR
- Loading them into pandas / DuckDB for inspection
- Extracting table schema (column names, types, sample rows, row count)
- Cleaning up old uploaded files past a retention window

This module never talks to the LLM. It only produces structured schema
data that schema_service.py later formats into prompt text.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import duckdb
import pandas as pd

from config import settings

logger = logging.getLogger("querylens.file_service")


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class FileServiceError(Exception):
    """Base exception for file_service failures."""


class UnsupportedFileTypeError(FileServiceError):
    """Raised when an uploaded file's extension is not supported."""


class FileTooLargeError(FileServiceError):
    """Raised when an uploaded file exceeds MAX_UPLOAD_SIZE_MB."""


class EmptyFileError(FileServiceError):
    """Raised when an uploaded file contains no parsable rows."""


class FileParsingError(FileServiceError):
    """Raised when pandas / DuckDB fails to parse an uploaded file's content."""


class FileNotFoundInStoreError(FileServiceError):
    """Raised when a referenced file_id cannot be located on disk."""


# ----------------------------------------------------------------------
# Data models (plain dataclasses; kept free of Pydantic here so this
# module has no FastAPI/HTTP concerns - routers wrap these as needed)
# ----------------------------------------------------------------------
@dataclass
class ColumnInfo:
    name: str
    type: str


@dataclass
class TableSchema:
    table_name: str
    columns: List[ColumnInfo]
    row_count: int
    sample_rows: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table_name": self.table_name,
            "columns": [{"name": c.name, "type": c.type} for c in self.columns],
            "row_count": self.row_count,
            "sample_rows": self.sample_rows,
        }


@dataclass
class UploadedFileRecord:
    file_id: str
    original_filename: str
    stored_path: Path
    extension: str
    size_bytes: int
    tables: List[TableSchema] = field(default_factory=list)
    uploaded_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_id": self.file_id,
            "original_filename": self.original_filename,
            "extension": self.extension,
            "size_bytes": self.size_bytes,
            "uploaded_at": self.uploaded_at,
            "tables": [t.to_dict() for t in self.tables],
        }


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
# NOTE: ".xls" (legacy BIFF Excel) is deliberately excluded. pandas is
# invoked below with engine="openpyxl", which only understands the
# modern .xlsx (OOXML) format - it cannot read .xls, and xlrd (which
# could) is not a project dependency. Advertising .xls support without
# the ability to parse it would just produce a guaranteed 422 for users.
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".json"}
SAMPLE_ROW_COUNT = 3
_SAFE_NAME_PATTERN = re.compile(r"[^a-zA-Z0-9_]")

# DuckDB connection hardening: fully disable filesystem/network access
# from within SQL executed against these in-memory contexts. Even though
# sql_service.py's AST + keyword checks block DDL/DML, DuckDB also
# exposes table functions (read_csv, read_parquet, httpfs-backed remote
# reads, etc.) that are syntactically plain SELECTs but can still touch
# the local filesystem or network if left enabled. Locking this down at
# connection-open time closes that gap regardless of what validation
# layers exist above it.
_DUCKDB_LOCKDOWN_CONFIG = {"enable_external_access": False}


def _sanitize_table_name(stem: str) -> str:
    """Converts a filename stem into a safe DuckDB/SQL table identifier."""
    cleaned = _SAFE_NAME_PATTERN.sub("_", stem).strip("_")
    if not cleaned:
        cleaned = "table"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned.lower()


def _validate_extension(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"'{ext or 'unknown'}' is not supported. "
            f"Allowed types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    return ext


def _validate_size(size_bytes: int) -> None:
    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if size_bytes > max_bytes:
        raise FileTooLargeError(
            f"File is {size_bytes / (1024 * 1024):.2f} MB, "
            f"which exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB limit."
        )
    if size_bytes == 0:
        raise EmptyFileError("Uploaded file is empty.")


def _dataframe_to_table_schema(df: pd.DataFrame, table_name: str) -> TableSchema:
    """
    Uses an in-memory, network/filesystem-locked-down DuckDB connection to
    register the dataframe and derive canonical DuckDB column types,
    ensuring the schema description shown to the LLM matches the types
    DuckDB will actually use at query time.
    """
    if df.empty:
        raise EmptyFileError("File was parsed but contains no data rows.")

    # Normalize column names: strip whitespace, replace blanks. This also
    # protects against columns with spaces/special characters breaking
    # generated SQL identifiers downstream (sqlglot/DuckDB quote
    # identifiers automatically, but a clean name is safer and more
    # readable in prompts shown to the LLM).
    df = df.copy()
    seen_names: Dict[str, int] = {}
    normalized_columns: List[str] = []
    for i, c in enumerate(df.columns):
        name = str(c).strip() if str(c).strip() else f"column_{i}"
        # De-duplicate column names that collide after normalization
        # (e.g. "Total" and "total " both stripping to the same value).
        if name in seen_names:
            seen_names[name] += 1
            name = f"{name}_{seen_names[name]}"
        else:
            seen_names[name] = 1
        normalized_columns.append(name)
    df.columns = normalized_columns

    con = duckdb.connect(database=":memory:", config=_DUCKDB_LOCKDOWN_CONFIG)
    try:
        con.register("tmp_df", df)
        describe_rows = con.execute("DESCRIBE SELECT * FROM tmp_df").fetchall()
        columns = [ColumnInfo(name=row[0], type=row[1]) for row in describe_rows]

        row_count = con.execute("SELECT COUNT(*) FROM tmp_df").fetchone()[0]

        sample_df = con.execute(
            f"SELECT * FROM tmp_df LIMIT {SAMPLE_ROW_COUNT}"
        ).fetchdf()
    finally:
        con.close()

    # Convert sample rows to JSON-safe native Python types. pandas'
    # to_json already converts NaN/NaT to null, which is what we want
    # for both the API response and the LLM prompt.
    sample_records = json.loads(sample_df.to_json(orient="records", date_format="iso"))

    return TableSchema(
        table_name=table_name,
        columns=columns,
        row_count=int(row_count),
        sample_rows=sample_records,
    )


def _load_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError as exc:
        raise EmptyFileError("CSV file contains no data.") from exc
    except (pd.errors.ParserError, UnicodeDecodeError) as exc:
        raise FileParsingError(f"Failed to parse CSV file: {exc}") from exc


def _load_excel(path: Path) -> Dict[str, pd.DataFrame]:
    """Returns a dict of sheet_name -> DataFrame for every non-empty sheet."""
    try:
        sheets = pd.read_excel(path, sheet_name=None, engine="openpyxl")
    except ValueError as exc:
        raise FileParsingError(f"Failed to parse Excel file: {exc}") from exc
    except Exception as exc:  # pandas raises various openpyxl-specific errors
        raise FileParsingError(f"Failed to parse Excel file: {exc}") from exc

    non_empty = {name: df for name, df in sheets.items() if not df.empty}
    if not non_empty:
        raise EmptyFileError("Excel file contains no data in any sheet.")
    return non_empty


def _load_json(path: Path) -> pd.DataFrame:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FileParsingError(f"Failed to read JSON file: {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise FileParsingError(f"Invalid JSON: {exc}") from exc

    try:
        if isinstance(data, list):
            df = pd.json_normalize(data)
        elif isinstance(data, dict):
            # Support {"records": [...]} style wrappers, else treat as a
            # single-row record.
            record_key = next(
                (k for k, v in data.items() if isinstance(v, list)), None
            )
            df = pd.json_normalize(data[record_key]) if record_key else pd.json_normalize([data])
        else:
            raise FileParsingError("JSON root must be an object or an array.")
    except FileParsingError:
        raise
    except Exception as exc:
        raise FileParsingError(f"Failed to normalize JSON into a table: {exc}") from exc

    return df


def save_upload(filename: str, content: bytes) -> UploadedFileRecord:
    """
    Persists an uploaded file's raw bytes to UPLOAD_DIR, parses it, and
    extracts schema information for every table/sheet found within it.

    Raises FileServiceError subclasses on any validation/parsing failure.
    The file is still written to disk before parsing is attempted so that
    partial diagnostics are possible, but is removed again if parsing fails
    to avoid accumulating unusable files.

    Path traversal note: the stored filename is built entirely from a
    server-generated UUID plus a stem that has been passed through
    _sanitize_table_name(), which strips every character except
    [a-zA-Z0-9_] (including "/", "\\", and "."). A filename like
    "../../etc/passwd" or "..\\..\\config" therefore can never influence
    where the file lands on disk - the original filename is only ever
    retained as a display string (original_filename), never as a path
    component.
    """
    ext = _validate_extension(filename)
    _validate_size(len(content))

    settings.ensure_upload_dir()
    file_id = uuid.uuid4().hex
    safe_stem = _sanitize_table_name(Path(filename).stem)
    stored_path = settings.UPLOAD_DIR / f"{file_id}_{safe_stem}{ext}"

    try:
        stored_path.write_bytes(content)
    except OSError as exc:
        raise FileServiceError(f"Failed to write uploaded file to disk: {exc}") from exc

    try:
        tables: List[TableSchema] = []

        if ext == ".csv":
            df = _load_csv(stored_path)
            tables.append(_dataframe_to_table_schema(df, safe_stem))

        elif ext == ".xlsx":
            sheets = _load_excel(stored_path)
            for sheet_name, df in sheets.items():
                table_name = _sanitize_table_name(f"{safe_stem}_{sheet_name}")
                tables.append(_dataframe_to_table_schema(df, table_name))

        elif ext == ".json":
            df = _load_json(stored_path)
            tables.append(_dataframe_to_table_schema(df, safe_stem))

        else:
            # Unreachable due to _validate_extension, kept for safety.
            raise UnsupportedFileTypeError(f"Unsupported extension: {ext}")

    except FileServiceError:
        # Parsing failed - remove the orphaned file and re-raise.
        stored_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        stored_path.unlink(missing_ok=True)
        raise FileParsingError(f"Unexpected error while parsing file: {exc}") from exc

    record = UploadedFileRecord(
        file_id=file_id,
        original_filename=filename,
        stored_path=stored_path,
        extension=ext,
        size_bytes=len(content),
        tables=tables,
    )
    _FILE_REGISTRY[file_id] = record
    logger.info(
        "Stored upload %s (%s) as %s with %d table(s)",
        filename, file_id, stored_path.name, len(tables),
    )
    return record


# In-memory registry mapping file_id -> UploadedFileRecord.
# This is process-local; for a single-container Render deployment this is
# sufficient. If horizontal scaling is introduced later, swap this for a
# shared store (e.g. Redis) without changing the public function signatures.
_FILE_REGISTRY: Dict[str, UploadedFileRecord] = {}


def get_upload(file_id: str) -> UploadedFileRecord:
    """Retrieves a previously stored UploadedFileRecord by its file_id."""
    record = _FILE_REGISTRY.get(file_id)
    if record is None:
        raise FileNotFoundInStoreError(f"No uploaded file found for id '{file_id}'.")
    return record


def get_dataframes(file_id: str) -> Dict[str, pd.DataFrame]:
    """
    Re-parses a previously stored upload from disk and returns its data as
    {table_name: DataFrame}, using the exact same table names already
    exposed via get_upload(...).tables. Intended for query-execution time,
    where sql_service needs the actual rows rather than schema metadata.
    """
    record = get_upload(file_id)

    if not record.stored_path.exists():
        raise FileNotFoundInStoreError(
            f"Stored file for id '{file_id}' is missing from disk (it may have "
            f"been cleaned up); please re-upload."
        )

    ext = record.extension
    table_names = [t.table_name for t in record.tables]

    if ext == ".csv":
        df = _load_csv(record.stored_path)
        return {table_names[0]: df}

    if ext == ".xlsx":
        sheets = _load_excel(record.stored_path)
        # Sheets were assigned table names in the same iteration order
        # during save_upload, so zip them back together positionally.
        return dict(zip(table_names, sheets.values()))

    if ext == ".json":
        df = _load_json(record.stored_path)
        return {table_names[0]: df}

    raise UnsupportedFileTypeError(f"Unsupported extension: {ext}")


def list_uploads() -> List[UploadedFileRecord]:
    """Returns all currently registered uploads, most recent first."""
    return sorted(_FILE_REGISTRY.values(), key=lambda r: r.uploaded_at, reverse=True)


def delete_upload(file_id: str) -> None:
    """Removes a file from disk and from the in-memory registry."""
    record = _FILE_REGISTRY.pop(file_id, None)
    if record is None:
        raise FileNotFoundInStoreError(f"No uploaded file found for id '{file_id}'.")
    record.stored_path.unlink(missing_ok=True)
    logger.info("Deleted upload %s (%s)", file_id, record.original_filename)


def cleanup_old_files(max_age_hours: int = 24) -> int:
    """
    Deletes uploaded files (both on disk and in the registry) older than
    max_age_hours. Also sweeps UPLOAD_DIR for orphaned files that exist on
    disk but are no longer tracked in the registry (e.g. after a process
    restart cleared in-memory state). Returns the number of files removed.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    removed = 0

    # 1. Remove stale, tracked records.
    stale_ids = [fid for fid, rec in _FILE_REGISTRY.items() if rec.uploaded_at < cutoff]
    for fid in stale_ids:
        try:
            delete_upload(fid)
            removed += 1
        except FileServiceError as exc:
            logger.warning("Failed to clean up tracked file %s: %s", fid, exc)

    # 2. Sweep orphaned files on disk not present in the registry at all.
    settings.ensure_upload_dir()
    tracked_paths = {rec.stored_path.name for rec in _FILE_REGISTRY.values()}
    try:
        for path in settings.UPLOAD_DIR.iterdir():
            if path.name == ".gitkeep" or path.name in tracked_paths:
                continue
            try:
                file_age = time.time() - path.stat().st_mtime
                if file_age > (max_age_hours * 3600):
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError as exc:
                logger.warning("Failed to remove orphaned file %s: %s", path, exc)
    except OSError as exc:
        logger.warning("Failed to sweep upload directory: %s", exc)

    if removed:
        logger.info("cleanup_old_files removed %d file(s)", removed)
    return removed