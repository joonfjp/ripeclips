from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AppSettings, LLMSettings, get_settings, reset_settings_cache


def test_llm_settings_load_and_validate_environment(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example/api/v1/")
    monkeypatch.setenv("LLM_MODEL", " model-name ")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("LLM_MAX_CONCURRENCY", "4")

    settings = LLMSettings.from_env()
    assert settings.base_url == "https://provider.example/api/v1"
    assert settings.model == "model-name"
    assert settings.api_key.get_secret_value() == "secret"
    assert settings.timeout_seconds == 42
    assert settings.max_concurrency == 4


def test_blank_environment_values_use_canonical_defaults(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "   ")
    settings = LLMSettings.from_env()
    assert settings.base_url == "http://localhost:7777/v1"
    assert settings.model is None


def test_required_api_key_fails_fast(monkeypatch):
    monkeypatch.setenv("LLM_REQUIRE_API_KEY", "true")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="LLM_API_KEY is required"):
        LLMSettings.from_env()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("base_url", "localhost:7777/v1"),
        ("base_url", "ftp://example.test/v1"),
        ("model", "   "),
    ],
)
def test_invalid_llm_configuration_fails_fast(field, value):
    with pytest.raises(ValidationError):
        LLMSettings.model_validate({field: value})


def test_cached_app_settings_can_be_explicitly_reset(monkeypatch):
    reset_settings_cache()
    monkeypatch.setenv("LLM_MODEL", "first")
    assert get_settings().llm.model == "first"
    monkeypatch.setenv("LLM_MODEL", "second")
    assert get_settings().llm.model == "first"
    reset_settings_cache()
    assert get_settings().llm.model == "second"
    reset_settings_cache()


def test_app_settings_parse_service_limits_and_cors(monkeypatch):
    monkeypatch.setenv("JOB_TTL_SECONDS", "60")
    monkeypatch.setenv("MAX_JOBS", "25")
    monkeypatch.setenv("MAX_SRT_UPLOAD_BYTES", "2048")
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://one.example, https://two.example",
    )
    settings = AppSettings.from_env()
    assert settings.job_ttl_seconds == 60
    assert settings.max_jobs == 25
    assert settings.max_srt_upload_bytes == 2048
    assert settings.cors_origins == (
        "https://one.example",
        "https://two.example",
    )
