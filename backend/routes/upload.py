"""
routes/upload.py

Exposes POST /api/upload - accepts a single CSV, Excel, or JSON file,
persists and parses it via file_service, and returns a file_id token
(used by /api/generate-sql later) alongside the extracted schema so the
frontend can render the "Tables: ..." preview badge immediately.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from services import file_service

logger = logging.getLogger("querylens.routes.upload")

router = APIRouter(prefix="/api", tags=["upload"])


# ----------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------
class ColumnSchema(BaseModel):
    name: str = Field(..., description="Column name")
    type: str = Field(..., description="DuckDB-inferred column type, e.g. BIGINT, VARCHAR")


class TableSchemaResponse(BaseModel):
    table_name: str = Field(..., description="Table name usable in generated SQL")
    columns: list[ColumnSchema]
    row_count: int = Field(..., description="Total number of rows in the table")
    sample_rows: list[dict] = Field(
        default_factory=list, description="Up to 3 sample rows for LLM/user context"
    )


class UploadResponse(BaseModel):
    file_id: str = Field(..., description="Opaque token identifying this upload for later requests")
    original_filename: str
    extension: str
    size_bytes: int
    tables: list[TableSchemaResponse]


class ErrorResponse(BaseModel):
    detail: str


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid, empty, or oversized file"},
        422: {"model": ErrorResponse, "description": "File could not be parsed"},
    },
    summary="Upload a CSV, Excel, or JSON file and extract its schema",
)
async def upload_file(file: UploadFile = File(..., description="CSV, XLSX, XLS, or JSON file")) -> UploadResponse:
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No filename provided.")

    content = await file.read()

    try:
        record = file_service.save_upload(file.filename, content)
    except (
        file_service.UnsupportedFileTypeError,
        file_service.FileTooLargeError,
        file_service.EmptyFileError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except file_service.FileParsingError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except file_service.FileServiceError as exc:
        logger.exception("Unexpected file_service error during upload")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred while processing the file.",
        ) from exc
    finally:
        await file.close()

    logger.info(
        "Upload processed: file_id=%s filename=%s tables=%d",
        record.file_id, record.original_filename, len(record.tables),
    )

    return UploadResponse(
        file_id=record.file_id,
        original_filename=record.original_filename,
        extension=record.extension,
        size_bytes=record.size_bytes,
        tables=[
            TableSchemaResponse(
                table_name=t.table_name,
                columns=[ColumnSchema(name=c.name, type=c.type) for c in t.columns],
                row_count=t.row_count,
                sample_rows=t.sample_rows,
            )
            for t in record.tables
        ],
    )


@router.delete(
    "/upload/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
    responses={404: {"model": ErrorResponse, "description": "File not found"}},
    summary="Delete a previously uploaded file",
)
async def delete_file(file_id: str) -> None:
    try:
        file_service.delete_upload(file_id)
    except file_service.FileNotFoundInStoreError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc