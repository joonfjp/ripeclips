from __future__ import annotations

import os
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


def _nonblank_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _environment_values(mapping: dict[str, str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for field_name, environment_name in mapping.items():
        value = _nonblank_env(environment_name)
        if value is not None:
            values[field_name] = value
    return values


class LLMSettings(BaseModel):
    """Validated configuration for the OpenAI-compatible LLM client."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = "http://localhost:7777/v1"
    model: str | None = None
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""))
    require_api_key: bool = False
    timeout_seconds: float = Field(default=180.0, gt=0, le=3600)
    max_retries: int = Field(default=2, ge=0, le=8)
    retry_initial_seconds: float = Field(default=3.0, ge=0, le=60)
    retry_multiplier: float = Field(default=3.0, ge=1, le=10)
    retry_max_seconds: float = Field(default=60.0, ge=0, le=600)
    retry_jitter_seconds: float = Field(default=0.5, ge=0, le=30)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    max_prompt_chars: int = Field(default=120_000, ge=4_000, le=2_000_000)
    ranking_batch_size: int = Field(default=40, ge=2, le=500)
    metadata_batch_size: int = Field(default=8, ge=1, le=100)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("LLM base_url must be an absolute http(s) URL")
        return normalized

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("LLM model must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_required_api_key(self) -> LLMSettings:
        if self.require_api_key and not self.api_key.get_secret_value():
            raise ValueError(
                "LLM_API_KEY is required when LLM_REQUIRE_API_KEY is enabled"
            )
        return self

    @classmethod
    def from_env(cls) -> LLMSettings:
        values: dict[str, Any] = _environment_values(
            {
                "base_url": "LLM_BASE_URL",
                "model": "LLM_MODEL",
                "api_key": "LLM_API_KEY",
                "require_api_key": "LLM_REQUIRE_API_KEY",
                "timeout_seconds": "LLM_TIMEOUT_SECONDS",
                "max_retries": "LLM_MAX_RETRIES",
                "retry_initial_seconds": "LLM_RETRY_INITIAL_SECONDS",
                "retry_multiplier": "LLM_RETRY_MULTIPLIER",
                "retry_max_seconds": "LLM_RETRY_MAX_SECONDS",
                "retry_jitter_seconds": "LLM_RETRY_JITTER_SECONDS",
                "max_concurrency": "LLM_MAX_CONCURRENCY",
                "max_prompt_chars": "LLM_MAX_PROMPT_CHARS",
                "ranking_batch_size": "LLM_RANKING_BATCH_SIZE",
                "metadata_batch_size": "LLM_METADATA_BATCH_SIZE",
            }
        )
        return cls.model_validate(values)


class AppSettings(BaseModel):
    """Process-level service configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    llm: LLMSettings = Field(default_factory=LLMSettings)
    job_ttl_seconds: float = Field(default=86_400.0, gt=0)
    max_jobs: int = Field(default=500, ge=1, le=100_000)
    max_srt_upload_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    cors_origins: tuple[str, ...] = (
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    )

    @classmethod
    def from_env(cls) -> AppSettings:
        values: dict[str, Any] = {
            "llm": LLMSettings.from_env(),
            **_environment_values(
                {
                    "job_ttl_seconds": "JOB_TTL_SECONDS",
                    "max_jobs": "MAX_JOBS",
                    "max_srt_upload_bytes": "MAX_SRT_UPLOAD_BYTES",
                }
            ),
        }
        cors_origins = _nonblank_env("CORS_ORIGINS")
        if cors_origins is not None:
            values["cors_origins"] = tuple(
                origin.strip() for origin in cors_origins.split(",") if origin.strip()
            )
        return cls.model_validate(values)


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings.from_env()


def reset_settings_cache() -> None:
    """Clear cached environment settings for tests or explicit reconfiguration."""

    get_settings.cache_clear()
