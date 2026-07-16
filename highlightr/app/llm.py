from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any


def _str_from_env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value is not None and value.strip() else default


def _int_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Base URL of an OpenAI-compatible API (the part ending in `/v1`). Ripe Clips
# posts to `{base}/chat/completions`. Point this at any OpenAI-compatible
# backend: a local server, OpenAI, OpenRouter, etc.
DEFAULT_BASE_URL = _str_from_env("LLM_BASE_URL", "http://localhost:8080/v1")

# Model name passed through to the backend.
DEFAULT_MODEL = _str_from_env("LLM_MODEL", "gemma4")

# Optional bearer token. Leave unset for local backends that don't require auth.
DEFAULT_API_KEY = os.environ.get("LLM_API_KEY", "")

# Per-request timeout for chat completion calls. Overridable via env; falls back
# to the built-in default when unset or unparseable.
DEFAULT_TIMEOUT_SECONDS = _int_from_env("LLM_TIMEOUT_SECONDS", 180)

RETRY_DELAYS_SECONDS = (3, 9)
USER_AGENT = "ripe-clips/0.1.0"

_DEFAULT_SYSTEM_PROMPT = (
    "You are a precise assistant. Respond only with JSON that conforms exactly "
    "to the provided JSON schema."
)


def get_chat_completions_url(base_url: str | None = None) -> str:
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


def call_llm_structured(
    prompt: str,
    json_schema: Mapping[str, Any],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    schema_name: str = "response",
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    timeout: int | None = None,
) -> Any:
    """Call an OpenAI-compatible chat/completions endpoint with a JSON schema.

    Returns the parsed JSON object produced by the model (already decoded from
    the assistant message content), so downstream code can consume it directly
    or pass it through ``extract_structured_output``.
    """
    timeout = timeout if timeout is not None else DEFAULT_TIMEOUT_SECONDS
    request_url = get_chat_completions_url(base_url)
    payload = {
        "model": model or DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": json_schema,
            },
        },
    }
    request_body = json.dumps(payload)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    key = api_key if api_key is not None else DEFAULT_API_KEY
    if key:
        headers["Authorization"] = f"Bearer {key}"

    request = urllib.request.Request(
        request_url,
        data=request_body.encode("utf-8"),
        headers=headers,
        method="POST",
    )

    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if attempt < len(RETRY_DELAYS_SECONDS) and _is_retryable_status(exc.code):
                time.sleep(RETRY_DELAYS_SECONDS[attempt])
                continue
            raise RuntimeError(
                f"HTTP {exc.code} calling chat endpoint at {request_url}: {error_body}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"Could not reach chat endpoint at {request_url}: {exc.reason}"
            ) from exc

        parsed = json.loads(response_body)
        return _content_from_completion(parsed)

    raise AssertionError("unreachable")


def _content_from_completion(completion: Any) -> Any:
    if not isinstance(completion, dict):
        raise RuntimeError(f"Unexpected chat completion response: {completion!r}")

    if completion.get("error"):
        error = completion["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise RuntimeError(f"chat endpoint error: {message}")

    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"chat completion had no choices: {completion!r}")

    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise RuntimeError(f"chat completion choice had no message: {choices[0]!r}")

    return message.get("content")


def extract_structured_output(response: Any) -> Any:
    if isinstance(response, dict):
        if response.get("error"):
            request_id = response.get("request_id")
            if request_id:
                raise ValueError(
                    f"structured endpoint error: {response['error']} (request_id={request_id})"
                )
            raise ValueError(f"structured endpoint error: {response['error']}")

        for key in ("result", "output", "data", "response", "structured_output"):
            if key in response:
                return extract_structured_output(response[key])
        return response

    if isinstance(response, list):
        if not response:
            raise ValueError("structured response list was empty")
        if len(response) == 1:
            return extract_structured_output(response[0])
        return response

    if isinstance(response, str):
        stripped = _strip_code_fences(response)
        try:
            return extract_structured_output(json.loads(stripped))
        except json.JSONDecodeError:
            return stripped

    return response


def _strip_code_fences(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _is_retryable_status(code: int) -> bool:
    return code == 429 or 500 <= code < 600
