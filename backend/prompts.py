"""
prompts.py

Prompt templates used to instruct the LLM to translate natural language
questions into validated SQL. The model is strictly constrained to return
a single JSON object with exactly two keys: "sql" and "explanation".
"""

from typing import Dict, List, Optional


# ----------------------------------------------------------------------
# Core system prompt
# ----------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """You are QueryLens, an expert SQL engine that converts natural language \
questions into a single, correct, executable SQL query for {dialect}.

You will be given:
1. A database schema (table names, column names, column types, and sample rows).
2. A natural language question from the user.

STRICT OUTPUT RULES:
- You MUST respond with ONLY a single valid JSON object. No markdown, no code fences, \
no commentary, no text before or after the JSON.
- The JSON object MUST contain EXACTLY two keys: "sql" and "explanation".
- "sql" must be a single valid {dialect} SELECT statement (or other read-only query) \
that directly answers the question, using ONLY tables and columns that exist in the \
provided schema.
- "explanation" must be plain English, 2 to 3 sentences maximum, describing what the \
query does and why, without repeating raw SQL syntax verbatim.
- Never invent table or column names that are not present in the schema.
- Never use DDL or DML statements (no INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, \
TRUNCATE). Only read-only SELECT queries are permitted.
- Do not use database-specific functions unsupported by {dialect}.
- If the question is ambiguous, make the most reasonable interpretation based on the \
schema and state that assumption briefly inside "explanation".
- If the question cannot be answered with the given schema, return a "sql" value of \
an empty string and explain why in "explanation".
- Quote identifiers only when necessary (e.g. names with spaces or reserved words).
- Always terminate the SQL statement with a semicolon.
- Limit result sets to a reasonable size ({row_limit} rows) using LIMIT when the \
question does not imply aggregation to a single row or small summary.

RESPONSE FORMAT (return exactly this shape, with real values, nothing else):
{{"sql": "SELECT ...;", "explanation": "..."}}
"""


# ----------------------------------------------------------------------
# Follow-up / correction prompt (used when a previous SQL attempt failed
# validation via sqlglot or execution against DuckDB)
# ----------------------------------------------------------------------
SQL_CORRECTION_TEMPLATE = """The previous SQL query you generated failed with the \
following error:

ERROR: {error_message}

PREVIOUS SQL:
{previous_sql}

Using the same schema and the original question below, generate a corrected query.
Follow the exact same STRICT OUTPUT RULES and JSON response format as before.

ORIGINAL QUESTION: {question}
"""


# ----------------------------------------------------------------------
# User-turn template combining schema + question
# ----------------------------------------------------------------------
USER_PROMPT_TEMPLATE = """DATABASE SCHEMA:
{schema}

USER QUESTION:
{question}
"""


def format_column(column: Dict[str, str]) -> str:
    """Formats a single column definition as 'name (TYPE)'."""
    name = column.get("name", "")
    col_type = column.get("type", "UNKNOWN")
    return f"{name} ({col_type})"


def format_table_schema(
    table_name: str,
    columns: List[Dict[str, str]],
    sample_rows: Optional[List[Dict]] = None,
    row_count: Optional[int] = None,
) -> str:
    """
    Formats a single table's schema (and optional sample rows) into a
    readable block for inclusion in the LLM prompt.

    Example output:
        TABLE: employees (1200 rows)
        COLUMNS: id (BIGINT), name (VARCHAR), salary (DOUBLE), department (VARCHAR)
        SAMPLE ROWS:
          {"id": 1, "name": "Asha Rao", "salary": 1200000, "department": "Engineering"}
          {"id": 2, "name": "Vikram Shah", "salary": 950000, "department": "Sales"}
    """
    lines: List[str] = []
    header = f"TABLE: {table_name}"
    if row_count is not None:
        header += f" ({row_count} rows)"
    lines.append(header)

    column_str = ", ".join(format_column(c) for c in columns)
    lines.append(f"COLUMNS: {column_str}")

    if sample_rows:
        lines.append("SAMPLE ROWS:")
        for row in sample_rows[:3]:
            lines.append(f"  {row}")

    return "\n".join(lines)


def format_full_schema(tables: List[Dict]) -> str:
    """
    Formats the complete multi-table schema description used in the prompt.

    `tables` is expected to be a list of dicts shaped like:
        {
            "table_name": str,
            "columns": [{"name": str, "type": str}, ...],
            "sample_rows": [dict, ...]  # optional
            "row_count": int             # optional
        }
    """
    if not tables:
        return "No tables available."

    blocks = [
        format_table_schema(
            table_name=t["table_name"],
            columns=t.get("columns", []),
            sample_rows=t.get("sample_rows"),
            row_count=t.get("row_count"),
        )
        for t in tables
    ]
    return "\n\n".join(blocks)


def build_system_prompt(dialect: str = "DuckDB", row_limit: int = 1000) -> str:
    """Builds the fully rendered system prompt for a given SQL dialect."""
    return SYSTEM_PROMPT_TEMPLATE.format(dialect=dialect, row_limit=row_limit)


def build_user_prompt(schema_tables: List[Dict], question: str) -> str:
    """Builds the fully rendered user prompt with schema context and question."""
    schema_text = format_full_schema(schema_tables)
    return USER_PROMPT_TEMPLATE.format(schema=schema_text, question=question.strip())


def build_correction_prompt(previous_sql: str, error_message: str, question: str) -> str:
    """Builds a follow-up prompt asking the LLM to correct a failed SQL query."""
    return SQL_CORRECTION_TEMPLATE.format(
        previous_sql=previous_sql,
        error_message=error_message,
        question=question.strip(),
    )