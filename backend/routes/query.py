"""
routes/query.py

Exposes POST /api/generate-sql - the core QueryLens endpoint. Accepts a
natural language question plus a reference to a previously established
schema source (an uploaded file_id, and/or a database connection_string),
then:

  1. Builds a unified schema context via schema_service.
  2. Sends the question + schema to llm_service to generate SQL + explanation.
  3. Validates the generated SQL with sql_service (sqlglot parse + safety checks).
  4. Executes the validated SQL against an in-memory DuckDB context built
     from the actual data (re-loaded via file_service, or materialized
     from PostgreSQL via database_service) so the response can include a
     real result preview alongside the SQL and explanation.
  5. If validation or execution fails, makes one automatic correction
     attempt by feeding the error back to the LLM before giving up.

Per the product spec, the primary response contract is just two things -
the SQL and the explanation - but a bounded result preview is included as
well since it is generated for free during validation and materially
helps users trust the query before copying it elsewhere.
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from services import database_service, file_service, llm_service, schema_service, sql_service

logger = logging.getLogger("querylens.routes.query")

router = APIRouter(prefix="/api", tags=["query"])

MAX_QUESTION_LENGTH = 1000
RESULT_PREVIEW_ROW_LIMIT = 50


# ----------------------------------------------------------------------
# Request models
# ----------------------------------------------------------------------
class GenerateSQLRequest(BaseModel):
    question: str = Field(
        ..., min_length=1, max_length=MAX_QUESTION_LENGTH,
        description="Natural language question to translate into SQL",
    )
    file_id: Optional[str] = Field(
        default=None, description="file_id returned from a prior /api/upload call"
    )
    connection_string: Optional[str] = Field(
        default=None, description="PostgreSQL connection string for a live database source"
    )
    schema_filter: str = Field(
        default="public", description="PostgreSQL schema to query against (database sources only)"
    )
    execute: bool = Field(
        default=True,
        description="Whether to also execute the validated SQL and return a result preview",
    )

    @field_validator("question")
    @classmethod
    def strip_question(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("question must not be blank")
        return stripped

    @model_validator(mode="after")
    def require_one_source(self) -> "GenerateSQLRequest":
        if not self.file_id and not self.connection_string:
            raise ValueError("Either file_id or connection_string must be provided.")
        if self.file_id and self.connection_string:
            raise ValueError(
                "Provide only one schema source per request: file_id OR connection_string."
            )
        return self


# ----------------------------------------------------------------------
# Response models
# ----------------------------------------------------------------------
class ResultPreview(BaseModel):
    columns: list[str]
    rows: list[dict]
    row_count: int
    truncated: bool = Field(
        default=False, description="True if the full result set exceeded the preview row limit"
    )


class GenerateSQLResponse(BaseModel):
    sql: str = Field(..., description="Validated, executable SQL query")
    explanation: str = Field(..., description="2-3 line plain English explanation of the query")
    dialect: str = Field(default="duckdb")
    warnings: list[str] = Field(default_factory=list)
    result_preview: Optional[ResultPreview] = Field(
        default=None, description="Present only when execute=true and execution succeeded"
    )


class ErrorResponse(BaseModel):
    detail: str


# ----------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------
def _load_execution_tables(request: GenerateSQLRequest) -> dict[str, pd.DataFrame]:
    """
    Materializes the actual data referenced by the schema context into
    {table_name: DataFrame} for sql_service to execute against. For file
    sources this re-reads the stored upload; for database sources this
    pulls each table's full contents via pandas over the same connection
    string (bounded by QUERY_ROW_LIMIT further downstream in sql_service).
    """
    if request.file_id:
        try:
            return file_service.get_dataframes(request.file_id)
        except file_service.FileNotFoundInStoreError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except file_service.FileServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

    # connection_string branch: materialize each table via pandas.read_sql
    assert request.connection_string is not None  # enforced by request validator
    try:
        conn_info = database_service.parse_connection_string(request.connection_string)
        tables_schema = database_service.get_schema(
            request.connection_string,
            schema_filter=request.schema_filter,
            include_sample_rows=False,
            include_row_counts=False,
        )
    except database_service.DatabaseServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    import psycopg2

    dataframes: dict[str, pd.DataFrame] = {}
    try:
        conn = psycopg2.connect(conn_info.dsn, connect_timeout=8)
        try:
            for table in tables_schema:
                qualified = f'"{table.schema_name}"."{table.table_name}"'
                dataframes[table.table_name] = pd.read_sql(f"SELECT * FROM {qualified}", conn)
        finally:
            conn.close()
    except psycopg2.Error as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to load table data from database: {exc}",
        ) from exc

    return dataframes


def _build_schema_context(request: GenerateSQLRequest) -> schema_service.SchemaContext:
    try:
        if request.file_id:
            return schema_service.build_schema_context(file_ids=[request.file_id])
        return schema_service.build_schema_context(
            connection_string=request.connection_string,
            db_schema_filter=request.schema_filter,
        )
    except schema_service.EmptySchemaError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except schema_service.SchemaServiceError as exc:
        # A wrapped FileNotFoundInStoreError means the referenced file_id
        # simply doesn't exist (e.g. expired/cleaned up) - that is a 404,
        # not a client input error.
        if "No uploaded file found" in str(exc):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _generate_with_llm(question: str, schema_prompt_list: list[dict]) -> llm_service.SQLGenerationResult:
    try:
        return llm_service.generate_sql(question=question, schema_tables=schema_prompt_list)
    except llm_service.LLMConfigurationError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except llm_service.LLMRequestError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except llm_service.LLMResponseParsingError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    except llm_service.LLMServiceError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _correct_with_llm(
    question: str, previous_sql: str, error_message: str
) -> llm_service.SQLGenerationResult:
    try:
        return llm_service.generate_correction(
            question=question, previous_sql=previous_sql, error_message=error_message
        )
    except llm_service.LLMServiceError as exc:
        # Correction attempt failing is not itself the primary error to
        # surface - the caller will raise using the original failure.
        logger.warning("LLM correction attempt failed: %s", exc)
        raise


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@router.post(
    "/generate-sql",
    response_model=GenerateSQLResponse,
    status_code=status.HTTP_200_OK,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid request or unsafe SQL"},
        404: {"model": ErrorResponse, "description": "Schema source not found or empty"},
        422: {"model": ErrorResponse, "description": "Generated SQL failed validation"},
        502: {"model": ErrorResponse, "description": "Upstream LLM or database failure"},
        503: {"model": ErrorResponse, "description": "LLM provider not configured"},
    },
    summary="Generate SQL and an explanation from a natural language question",
)
async def generate_sql(payload: GenerateSQLRequest) -> GenerateSQLResponse:
    schema_context = _build_schema_context(payload)
    schema_prompt_list = schema_context.to_prompt_list()

    generation = _generate_with_llm(payload.question, schema_prompt_list)

    if not generation.sql:
        # LLM explicitly declined (e.g. unanswerable question) - surface
        # its explanation without attempting validation/execution.
        return GenerateSQLResponse(
            sql="",
            explanation=generation.explanation,
            dialect="duckdb",
            warnings=["The model could not produce a query for this question."],
            result_preview=None,
        )

    validation_error: Optional[str] = None
    validation: Optional[sql_service.ValidationResult] = None
    current_sql = generation.sql
    current_explanation = generation.explanation

    try:
        validation = sql_service.validate_sql(current_sql)
    except sql_service.SQLServiceError as exc:
        validation_error = str(exc)

    if validation_error is not None:
        logger.info("Initial SQL failed validation, attempting one correction: %s", validation_error)
        try:
            correction = _correct_with_llm(payload.question, current_sql, validation_error)
            current_sql = correction.sql
            current_explanation = correction.explanation
            validation = sql_service.validate_sql(current_sql)
            validation_error = None
        except (llm_service.LLMServiceError, sql_service.SQLServiceError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Generated SQL failed validation and could not be automatically "
                       f"corrected: {validation_error}",
            ) from exc

    assert validation is not None  # validation_error is None here, so validation succeeded

    response = GenerateSQLResponse(
        sql=validation.normalized_sql,
        explanation=current_explanation,
        dialect=validation.dialect,
        warnings=list(validation.warnings),
        result_preview=None,
    )

    if not payload.execute:
        return response

    try:
        tables = _load_execution_tables(payload)
        execution = sql_service.execute_query(
            validation.normalized_sql, tables, row_limit=RESULT_PREVIEW_ROW_LIMIT
        )
        response.result_preview = ResultPreview(
            columns=execution.columns,
            rows=execution.rows,
            row_count=execution.row_count,
            truncated=execution.truncated,
        )
    except sql_service.SQLExecutionError as exc:
        # Attempt one correction using the execution error before giving up.
        logger.info("Execution failed, attempting one correction: %s", exc)
        try:
            correction = _correct_with_llm(payload.question, current_sql, str(exc))
            corrected_validation = sql_service.validate_sql(correction.sql)
            tables = _load_execution_tables(payload)
            execution = sql_service.execute_query(
                corrected_validation.normalized_sql, tables, row_limit=RESULT_PREVIEW_ROW_LIMIT
            )
            response.sql = corrected_validation.normalized_sql
            response.explanation = correction.explanation
            response.warnings = list(corrected_validation.warnings)
            response.result_preview = ResultPreview(
                columns=execution.columns,
                rows=execution.rows,
                row_count=execution.row_count,
                truncated=execution.truncated,
            )
        except (llm_service.LLMServiceError, sql_service.SQLServiceError) as retry_exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Query passed validation but failed to execute, and could not be "
                       f"automatically corrected: {exc}",
            ) from retry_exc

    return response