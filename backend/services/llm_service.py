"""
services/llm_service.py

Calls an OpenAI-compatible chat completion endpoint (Groq or NVIDIA NIM,
selected via config.settings.LLM_PROVIDER) to translate a natural language
question plus schema context into a JSON object containing "sql" and
"explanation". Uses the provider's JSON object response format when
available and falls back to prompt-enforced JSON + defensive parsing
otherwise, since not every OpenAI-compatible provider/model supports
`response_format={"type": "json_object"}` identically.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)

from config import settings
from prompts import build_correction_prompt, build_system_prompt, build_user_prompt

logger = logging.getLogger("querylens.llm_service")

REQUIRED_KEYS = {"sql", "explanation"}
MAX_CORRECTION_ATTEMPTS = 1  # how many times generate_and_correct will retry


# ----------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------
class LLMServiceError(Exception):
    """Base exception for llm_service failures."""


class LLMConfigurationError(LLMServiceError):
    """Raised when the active provider is missing required configuration (e.g. API key)."""


class LLMRequestError(LLMServiceError):
    """Raised when the request to the LLM provider fails (network, timeout, auth, rate limit)."""


class LLMResponseParsingError(LLMServiceError):
    """Raised when the LLM's response cannot be parsed into the expected JSON shape."""


# ----------------------------------------------------------------------
# Result model
# ----------------------------------------------------------------------
@dataclass
class SQLGenerationResult:
    sql: str
    explanation: str
    raw_response: str
    model: str
    provider: str

    def to_dict(self) -> Dict[str, str]:
        return {"sql": self.sql, "explanation": self.explanation}


_client: Optional[OpenAI] = None


def _get_client() -> OpenAI:
    """
    Lazily builds (and caches) an OpenAI SDK client pointed at whichever
    provider is currently active in settings. Lazy construction means a
    missing API key only raises when generation is actually attempted,
    not at import time.
    """
    global _client

    api_key = settings.active_api_key
    if not api_key:
        raise LLMConfigurationError(
            f"No API key configured for LLM_PROVIDER='{settings.LLM_PROVIDER}'. "
            f"Set {'GROQ_API_KEY' if settings.LLM_PROVIDER == 'groq' else 'NVIDIA_API_KEY'} "
            f"in your environment."
        )

    if _client is None:
        _client = OpenAI(
            api_key=api_key,
            base_url=settings.active_base_url,
            timeout=settings.LLM_TIMEOUT_SECONDS,
        )
    return _client


def reset_client() -> None:
    """Forces the next call to rebuild the OpenAI client (e.g. after provider/key change)."""
    global _client
    _client = None


# ----------------------------------------------------------------------
# JSON extraction helpers
# ----------------------------------------------------------------------
_CODE_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    return _CODE_FENCE_PATTERN.sub("", text).strip()


def _extract_json_object(text: str) -> Dict[str, Any]:
    """
    Extracts a JSON object from raw LLM text output. Handles the common
    cases of a clean JSON object, one wrapped in markdown code fences, or
    one embedded alongside stray text the model added despite instructions.
    """
    cleaned = _strip_code_fences(text.strip())

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_PATTERN.search(cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    raise LLMResponseParsingError(
        "Could not extract a valid JSON object from the model's response."
    )


def _validate_response_shape(parsed: Dict[str, Any]) -> Dict[str, str]:
    if not isinstance(parsed, dict):
        raise LLMResponseParsingError("Model response JSON is not an object.")

    missing = REQUIRED_KEYS - parsed.keys()
    if missing:
        raise LLMResponseParsingError(
            f"Model response is missing required key(s): {', '.join(sorted(missing))}"
        )

    sql = parsed.get("sql")
    explanation = parsed.get("explanation")

    if not isinstance(sql, str):
        raise LLMResponseParsingError("'sql' field must be a string.")
    if not isinstance(explanation, str):
        raise LLMResponseParsingError("'explanation' field must be a string.")

    return {"sql": sql.strip(), "explanation": explanation.strip()}


def _call_completion(system_prompt: str, user_prompt: str) -> str:
    """
    Performs a single chat completion call against the active provider
    and returns the raw text content of the response.
    """
    client = _get_client()

    request_kwargs: Dict[str, Any] = dict(
        model=settings.active_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=settings.LLM_TEMPERATURE,
        max_tokens=settings.LLM_MAX_TOKENS,
    )

    try:
        # Prefer strict JSON-object mode where the provider supports it.
        response = client.chat.completions.create(
            response_format={"type": "json_object"}, **request_kwargs
        )
    except APIStatusError as exc:
        # Some OpenAI-compatible providers/models reject response_format
        # with a 400. Retry once without it, relying on prompt-enforced
        # JSON output plus our defensive parser.
        if exc.status_code == 400:
            logger.warning(
                "Provider '%s' rejected response_format=json_object (%s); "
                "retrying without structured output mode.",
                settings.LLM_PROVIDER, exc,
            )
            try:
                response = client.chat.completions.create(**request_kwargs)
            except OpenAIError as retry_exc:
                raise _wrap_openai_error(retry_exc) from retry_exc
        else:
            raise _wrap_openai_error(exc) from exc
    except OpenAIError as exc:
        raise _wrap_openai_error(exc) from exc

    if not response.choices:
        raise LLMRequestError("Model returned no choices in its response.")

    content = response.choices[0].message.content
    if not content or not content.strip():
        raise LLMRequestError("Model returned an empty response.")

    return content


def _wrap_openai_error(exc: OpenAIError) -> LLMRequestError:
    if isinstance(exc, AuthenticationError):
        return LLMRequestError(
            f"Authentication failed with {settings.LLM_PROVIDER} - check your API key."
        )
    if isinstance(exc, RateLimitError):
        return LLMRequestError(
            f"{settings.LLM_PROVIDER} rate limit exceeded. Please retry shortly."
        )
    if isinstance(exc, APITimeoutError):
        return LLMRequestError(
            f"Request to {settings.LLM_PROVIDER} timed out after "
            f"{settings.LLM_TIMEOUT_SECONDS}s."
        )
    if isinstance(exc, APIConnectionError):
        return LLMRequestError(f"Could not reach {settings.LLM_PROVIDER}: {exc}")
    if isinstance(exc, APIStatusError):
        return LLMRequestError(
            f"{settings.LLM_PROVIDER} returned an error "
            f"(status {exc.status_code}): {_safe_error_body(exc)}"
        )
    return LLMRequestError(f"Unexpected error calling {settings.LLM_PROVIDER}: {exc}")


def _safe_error_body(exc: APIStatusError) -> str:
    try:
        body = exc.response.json()
        return json.dumps(body)[:500]
    except Exception:
        return str(exc)[:500]


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def generate_sql(
    question: str,
    schema_tables: List[Dict[str, Any]],
    dialect: str = "DuckDB",
) -> SQLGenerationResult:
    """
    Generates a single SQL query + explanation for the given natural
    language question against the given schema. Raises LLMServiceError
    subclasses on any configuration, request, or parsing failure.
    """
    if not question or not question.strip():
        raise LLMServiceError("Question must not be empty.")
    if not schema_tables:
        raise LLMServiceError("At least one table's schema must be provided.")

    system_prompt = build_system_prompt(dialect=dialect, row_limit=settings.QUERY_ROW_LIMIT)
    user_prompt = build_user_prompt(schema_tables, question)

    logger.info(
        "Requesting SQL generation via %s (%s) for question: %.120s",
        settings.LLM_PROVIDER, settings.active_model, question,
    )

    raw_text = _call_completion(system_prompt, user_prompt)
    parsed = _extract_json_object(raw_text)
    validated = _validate_response_shape(parsed)

    return SQLGenerationResult(
        sql=validated["sql"],
        explanation=validated["explanation"],
        raw_response=raw_text,
        model=settings.active_model,
        provider=settings.LLM_PROVIDER,
    )


def generate_correction(
    question: str,
    previous_sql: str,
    error_message: str,
) -> SQLGenerationResult:
    """
    Asks the model to fix a previously generated SQL query that failed
    validation or execution. Used by sql_service in a single automatic
    retry loop before surfacing an error to the user.
    """
    system_prompt = build_system_prompt(dialect="DuckDB", row_limit=settings.QUERY_ROW_LIMIT)
    correction_prompt = build_correction_prompt(
        previous_sql=previous_sql, error_message=error_message, question=question
    )

    logger.info("Requesting SQL correction after error: %.200s", error_message)

    raw_text = _call_completion(system_prompt, correction_prompt)
    parsed = _extract_json_object(raw_text)
    validated = _validate_response_shape(parsed)

    return SQLGenerationResult(
        sql=validated["sql"],
        explanation=validated["explanation"],
        raw_response=raw_text,
        model=settings.active_model,
        provider=settings.LLM_PROVIDER,
    )