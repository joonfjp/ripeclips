from __future__ import annotations

import io
import json
import threading
import time
import urllib.error

import pytest
from pydantic import BaseModel, ConfigDict, Field

from app import llm
from app.config import LLMSettings


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


class ScoredAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = Field(ge=0, le=1, allow_inf_nan=False)


class _Response:
    def __init__(self, body: str, headers: dict[str, str] | None = None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return self.body.encode()


def _completion(content: str, *, usage: bool = False) -> str:
    payload: dict = {
        "choices": [{"message": {"role": "assistant", "content": content}}]
    }
    if usage:
        payload["usage"] = {
            "prompt_tokens": 10,
            "completion_tokens": 3,
            "total_tokens": 13,
        }
    return json.dumps(payload)


def _settings(**overrides) -> LLMSettings:
    values = {
        "base_url": "https://example.test/v1",
        "model": "test-model",
        "api_key": "secret-api-key",
        "max_retries": 0,
        "retry_jitter_seconds": 0,
    }
    values.update(overrides)
    return LLMSettings.model_validate(values)


def test_typed_client_sends_expected_payload_headers_and_usage():
    captured = {}

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response(
            _completion('{"answer":"ok"}', usage=True),
            headers={"x-request-id": "request-123"},
        )

    result = llm.LLMClient(_settings(), urlopen=urlopen).call_structured(
        "the complete prompt",
        Answer,
        schema_name="answer",
    )

    request = captured["request"]
    payload = json.loads(request.data)
    assert result.output.answer == "ok"
    assert result.attempts == 1
    assert result.request_id == "request-123"
    assert result.usage.total_tokens == 13
    assert request.full_url == "https://example.test/v1/chat/completions"
    assert request.get_header("Authorization") == "Bearer secret-api-key"
    assert request.get_header("User-agent") == "highlightr/0.2.0"
    assert payload["model"] == "test-model"
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["name"] == "answer"
    assert captured["timeout"] == 180


def test_typed_client_omits_model_when_not_configured():
    captured = {}

    def urlopen(request, timeout):
        assert timeout == 180
        captured["payload"] = json.loads(request.data)
        return _Response(_completion('{"answer":"ok"}'))

    settings = _settings(model=None)
    result = llm.LLMClient(settings, urlopen=urlopen).call_structured(
        "prompt",
        Answer,
    )

    assert result.output.answer == "ok"
    assert "model" not in captured["payload"]


def test_compatibility_wrapper_keeps_returning_json_text(monkeypatch):
    def urlopen(_request, timeout):
        assert timeout == 180
        return _Response(_completion('{"answer":"ok"}'))

    monkeypatch.setattr(llm.urllib.request, "urlopen", urlopen)
    result = llm.call_llm_structured(
        "prompt",
        Answer.model_json_schema(),
        base_url="https://example.test/v1",
        model="test-model",
    )
    assert result == '{"answer":"ok"}'


def test_retries_http_429_and_respects_retry_after():
    responses = [
        urllib.error.HTTPError(
            "https://example.test/v1/chat/completions",
            429,
            "rate limited",
            {"Retry-After": "7"},
            io.BytesIO(b"busy"),
        ),
        _Response(_completion('{"answer":"ok"}')),
    ]
    sleeps: list[float] = []

    def urlopen(_request, timeout):
        assert timeout == 180
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    client = llm.LLMClient(
        _settings(max_retries=1),
        urlopen=urlopen,
        sleep=sleeps.append,
        random_value=lambda: 0,
    )
    result = client.call_structured("prompt", Answer)
    assert result.attempts == 2
    assert sleeps == [7]


def test_retries_transient_connection_error():
    responses = [
        urllib.error.URLError("temporary DNS failure"),
        _Response(_completion('{"answer":"ok"}')),
    ]
    sleeps: list[float] = []

    def urlopen(_request, timeout):
        assert timeout == 180
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    result = llm.LLMClient(
        _settings(max_retries=1),
        urlopen=urlopen,
        sleep=sleeps.append,
        random_value=lambda: 0,
    ).call_structured("prompt", Answer)
    assert result.attempts == 2
    assert sleeps == [3]


def test_non_retryable_http_error_has_attempt_count():
    def urlopen(_request, timeout):
        assert timeout == 180
        raise urllib.error.HTTPError(
            "https://example.test/v1/chat/completions",
            400,
            "bad request",
            {},
            io.BytesIO(b"invalid schema"),
        )

    with pytest.raises(llm.LLMHTTPError) as raised:
        llm.LLMClient(_settings(max_retries=2), urlopen=urlopen).call_structured(
            "prompt",
            Answer,
        )
    assert raised.value.status_code == 400
    assert raised.value.attempts == 1


def test_rejects_malformed_and_nonfinite_json():
    malformed = llm.LLMClient(
        _settings(),
        urlopen=lambda _request, timeout: _Response("not-json"),
    )
    with pytest.raises(llm.LLMResponseError, match="Invalid JSON") as raised:
        malformed.call_structured("prompt", Answer)
    assert raised.value.attempts == 2

    nonfinite = llm.LLMClient(
        _settings(),
        urlopen=lambda _request, timeout: _Response(
            _completion('{"score":NaN}')
        ),
    )
    with pytest.raises(llm.LLMResponseError, match="non-finite"):
        nonfinite.call_structured("prompt", ScoredAnswer)


def test_reports_provider_output_truncation():
    completion = json.loads(_completion('{"answer":"unfinished'))
    completion["choices"][0]["finish_reason"] = "length"
    client = llm.LLMClient(
        _settings(),
        urlopen=lambda _request, timeout: _Response(json.dumps(completion)),
    )
    with pytest.raises(llm.LLMResponseError, match="truncated.*finish_reason=length") as raised:
        client.call_structured("prompt", Answer)
    assert raised.value.attempts == 2


def test_strict_model_validation_rejects_numeric_strings():
    client = llm.LLMClient(
        _settings(),
        urlopen=lambda _request, timeout: _Response(
            _completion('{"score":"1"}')
        ),
    )
    with pytest.raises(llm.LLMResponseError, match="did not match"):
        client.call_structured("prompt", ScoredAnswer)


def test_prompt_character_limit_is_enforced_before_network_call():
    called = False

    def urlopen(_request, timeout):
        nonlocal called
        called = True
        return _Response(_completion('{"answer":"ok"}'))

    client = llm.LLMClient(
        _settings(max_prompt_chars=4000),
        urlopen=urlopen,
    )
    with pytest.raises(llm.LLMPromptTooLargeError):
        client.call_structured("x" * 4000, Answer)
    assert called is False


def test_chat_completions_url_accepts_base_or_complete_endpoint():
    assert (
        llm.get_chat_completions_url("https://example.test/v1")
        == "https://example.test/v1/chat/completions"
    )
    assert (
        llm.get_chat_completions_url(
            "https://example.test/v1/chat/completions/"
        )
        == "https://example.test/v1/chat/completions"
    )


def test_global_semaphore_caps_concurrent_calls_across_clients(monkeypatch):
    monkeypatch.setattr(llm, "_GLOBAL_LLM_SEMAPHORE", threading.BoundedSemaphore(2))
    lock = threading.Lock()
    active = 0
    peak = 0

    def urlopen(_request, timeout):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _Response(_completion('{"answer":"ok"}'))

    # Separate clients stand in for separate jobs; each would otherwise be
    # free to run its own fan-out.
    clients = [llm.LLMClient(_settings(), urlopen=urlopen) for _ in range(3)]
    errors: list[BaseException] = []

    def call(client):
        try:
            client.call_structured("Question", Answer)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [
        threading.Thread(target=call, args=(client,))
        for client in clients
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert peak == 2


def test_global_semaphore_is_released_on_http_error(monkeypatch):
    semaphore = threading.BoundedSemaphore(1)

    def urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 400, "Bad Request", {}, io.BytesIO(b"bad")
        )

    client = llm.LLMClient(_settings(), urlopen=urlopen, semaphore=semaphore)
    for _ in range(2):
        with pytest.raises(llm.LLMHTTPError):
            client.call_structured("Question", Answer)
    assert semaphore.acquire(blocking=False)
