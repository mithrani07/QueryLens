"""
routes/database.py

Exposes POST /api/connect-db - accepts a PostgreSQL connection string,
validates and tests it, extracts full schema metadata via
database_service, and returns it in the same shape as /api/upload so the
frontend can render a unified schema preview regardless of source.

The raw connection string is intentionally never echoed back in the
response; database_service redacts credentials from all logs as well.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from services import database_service

logger = logging.getLogger("querylens.routes.database")

router = APIRouter(prefix="/api", tags=["database"])


# ----------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------
class ConnectDatabaseRequest(BaseModel):
    connection_string: str = Field(
        ...,
        min_length=1,
        description="PostgreSQL connection string, e.g. postgresql://user:pass@host:5432/dbname",
    )
    schema_filter: str = Field(
        default="public",
        min_length=1,
        max_length=63,
        description="PostgreSQL schema to inspect (defaults to 'public')",
    )

    @field_validator("connection_string")
    @classmethod
    def strip_connection_string(cls, v: str) -> str:
        return v.strip()

    @field_validator("schema_filter")
    @classmethod
    def strip_schema_filter(cls, v: str) -> str:
        return v.strip()


# ----------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------
class ColumnSchema(BaseModel):
    name: str
    type: str
    is_nullable: bool
    is_primary_key: bool


class TableSchemaResponse(BaseModel):
    table_name: str
    schema_name: str
    columns: list[ColumnSchema]
    row_count: int | None = Field(default=None, description="Approximate row count, if available")
    sample_rows: list[dict] = Field(default_factory=list)


class ConnectionInfoResponse(BaseModel):
    host: str
    port: int
    database: str
    username: str


class ConnectDatabaseResponse(BaseModel):
    connection: ConnectionInfoResponse
    schema_filter: str
    tables: list[TableSchemaResponse]


class ErrorResponse(BaseModel):
    detail: str


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@router.post(
    "/connect-db",
    response_model=ConnectDatabaseResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse, "description": "Malformed connection string"},
        404: {"model": ErrorResponse, "description": "No tables found in the target schema"},
        502: {"model": ErrorResponse, "description": "Could not connect to the database"},
    },
    summary="Connect to a PostgreSQL database and extract its schema",
)
async def connect_database(payload: ConnectDatabaseRequest) -> ConnectDatabaseResponse:
    try:
        conn_info = database_service.test_connection(payload.connection_string)
    except database_service.InvalidConnectionStringError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except database_service.DatabaseConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except database_service.DatabaseServiceError as exc:
        logger.exception("Unexpected database_service error during connection test")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while connecting to the database.",
        ) from exc

    try:
        tables = database_service.get_schema(
            payload.connection_string, schema_filter=payload.schema_filter
        )
    except database_service.SchemaExtractionError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except database_service.DatabaseConnectionError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except database_service.DatabaseServiceError as exc:
        logger.exception("Unexpected database_service error during schema extraction")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while extracting the schema.",
        ) from exc

    logger.info(
        "Connected to database %s@%s/%s - extracted %d table(s) from schema '%s'",
        conn_info.username, conn_info.host, conn_info.database, len(tables), payload.schema_filter,
    )

    return ConnectDatabaseResponse(
        connection=ConnectionInfoResponse(**conn_info.to_dict()),
        schema_filter=payload.schema_filter,
        tables=[
            TableSchemaResponse(
                table_name=t.table_name,
                schema_name=t.schema_name,
                columns=[
                    ColumnSchema(
                        name=c.name,
                        type=c.type,
                        is_nullable=c.is_nullable,
                        is_primary_key=c.is_primary_key,
                    )
                    for c in t.columns
                ],
                row_count=t.row_count,
                sample_rows=t.sample_rows,
            )
            for t in tables
        ],
    )