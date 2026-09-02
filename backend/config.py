"""
config.py

Centralized application configuration for QueryLens.
Loads environment variables via pydantic-settings and exposes a single
cached `settings` instance used across routers and services.
"""

from functools import lru_cache
from pathlib import Path
from typing import List, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute path to the backend/ directory (parent of this file)
BASE_DIR = Path(__file__).resolve().parent
# Absolute path to the project root (parent of backend/)
PROJECT_ROOT = BASE_DIR.parent


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables / .env file.

    LLM_PROVIDER selects which OpenAI-compatible endpoint is used at runtime.
    Both Groq and NVIDIA NIM expose OpenAI-compatible chat completion APIs,
    so a single OpenAI SDK client is reused with a different base_url and key.
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # General app metadata
    # ------------------------------------------------------------------
    APP_NAME: str = "QueryLens"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    ENVIRONMENT: Literal["development", "production"] = "development"

    # ------------------------------------------------------------------
    # LLM Provider configuration (Groq / NVIDIA NIM - OpenAI compatible)
    # ------------------------------------------------------------------
    LLM_PROVIDER: Literal["groq", "nvidia"] = "groq"

    # Groq
    GROQ_API_KEY: str = Field(default="")
    GROQ_BASE_URL: str = "https://api.groq.com/openai/v1"
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # NVIDIA NIM
    NVIDIA_API_KEY: str = Field(default="")
    NVIDIA_BASE_URL: str = "https://integrate.api.nvidia.com/v1"
    NVIDIA_MODEL: str = "meta/llama-3.1-70b-instruct"

    # LLM generation parameters
    LLM_TEMPERATURE: float = 0.1
    LLM_MAX_TOKENS: int = 1024
    LLM_TIMEOUT_SECONDS: int = 30

    # ------------------------------------------------------------------
    # File upload / storage configuration
    #
    # NOTE: .xls (legacy BIFF Excel) is intentionally NOT in this list.
    # pandas is invoked with engine="openpyxl" in file_service.py, which
    # can only read .xlsx (OOXML). openpyxl cannot parse .xls, and xlrd
    # (the library that could) is not in requirements.txt. Advertising
    # .xls support without the ability to parse it produces a guaranteed
    # 422 for the user, so it's excluded rather than silently broken.
    # ------------------------------------------------------------------
    UPLOAD_DIR: Path = PROJECT_ROOT / "uploads"
    MAX_UPLOAD_SIZE_MB: int = 15
    ALLOWED_UPLOAD_EXTENSIONS: List[str] = [".csv", ".xlsx", ".json"]

    # ------------------------------------------------------------------
    # DuckDB configuration
    # ------------------------------------------------------------------
    DUCKDB_PATH: str = ":memory:"
    MAX_PREVIEW_ROWS: int = 50
    QUERY_ROW_LIMIT: int = 1000

    # ------------------------------------------------------------------
    # CORS configuration
    #
    # SECURITY: CORS_ORIGINS=["*"] must always be paired with
    # CORS_ALLOW_CREDENTIALS=False. This app doesn't use cookies/sessions,
    # so credentialed cross-origin requests should never be allowed - a
    # wildcard origin + credentials=True is a real exposure (and modern
    # browsers reject the combination outright anyway).
    # ------------------------------------------------------------------
    CORS_ORIGINS: List[str] = ["*"]
    CORS_ALLOW_CREDENTIALS: bool = False
    CORS_ALLOW_METHODS: List[str] = ["*"]
    CORS_ALLOW_HEADERS: List[str] = ["*"]

    # ------------------------------------------------------------------
    # Server configuration
    # ------------------------------------------------------------------
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    @field_validator("UPLOAD_DIR", mode="before")
    @classmethod
    def resolve_upload_dir(cls, v: str) -> Path:
        path = Path(v)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return path

    @property
    def active_api_key(self) -> str:
        """Returns the API key for the currently selected LLM_PROVIDER."""
        return self.GROQ_API_KEY if self.LLM_PROVIDER == "groq" else self.NVIDIA_API_KEY

    @property
    def active_base_url(self) -> str:
        """Returns the base_url for the currently selected LLM_PROVIDER."""
        return self.GROQ_BASE_URL if self.LLM_PROVIDER == "groq" else self.NVIDIA_BASE_URL

    @property
    def active_model(self) -> str:
        """Returns the model name for the currently selected LLM_PROVIDER."""
        return self.GROQ_MODEL if self.LLM_PROVIDER == "groq" else self.NVIDIA_MODEL

    def ensure_upload_dir(self) -> None:
        """Creates the upload directory on disk if it does not already exist."""
        self.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached Settings instance. Using lru_cache ensures the .env
    file and environment are parsed only once per process.
    """
    settings = Settings()
    settings.ensure_upload_dir()
    return settings


# Module-level singleton for convenient importing: `from config import settings`
settings = get_settings()