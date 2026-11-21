from __future__ import annotations

import copy
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Any, Generic, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from .config import LLMSettings, get_settings
from .version import __version__

logger = logging.getLogger(__name__)

_CANONICAL_DEFAULTS = LLMSettings()
DEFAULT_BASE_URL = _CANONICAL_DEFAULTS.base_url
DEFAULT_MODEL = _CANONICAL_DEFAULTS.model
DEFAULT_API_KEY = _CANONICAL_DEFAULTS.api_key.get_secret_value()
DEFAULT_TIMEOUT_SECONDS = _CANONICAL_DEFAULTS.timeout_seconds
USER_AGENT = f"highlightr/{__version__}"

DEFAULT_GLOBAL_CONCURRENCY = 4


def _global_concurrency_from_env() -> int:
    raw = (os.environ.get("LLM_GLOBAL_CONCURRENCY") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_GLOBAL_CONCURRENCY
    except ValueError:
        logger.warning(
            "Ignoring invalid LLM_GLOBAL_CONCURRENCY=%r; using %d",
            raw,
            DEFAULT_GLOBAL_CONCURRENCY,
        )
        return DEFAULT_GLOBAL_CONCURRENCY


# Process-wide cap on in-flight LLM requests. LLMSettings.max_concurrency only
# bounds a single job's discovery fan-out; this keeps many concurrent jobs (and
# ad-hoc clients) from flooding the provider.
_GLOBAL_LLM_SEMAPHORE = threading.BoundedSemaphore(_global_concurrency_from_env())

_DEFAULT_SYSTEM_PROMPT = (
    "You are a precise assistant. Respond only with JSON that conforms exactly "
    "to the provided JSON schema."
)

T = TypeVar("T", bound=BaseModel)


class ResponseLike(Protocol):
    headers: Any

    def __enter__(self) -> ResponseLike: ...

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> bool: ...

    def read(self) -> bytes: ...


UrlOpen = Callable[..., ResponseLike]


class LLMError(RuntimeError):
    """Base class for expected provider failures."""

    def __init__(self, message: str, *, attempts: int = 0):
        super().__init__(message)
        self.attempts = attempts


class LLMConfigurationError(LLMError):
    pass


class LLMConnectionError(LLMError):
    pass


class LLMHTTPError(LLMError):
    def __init__(self, status_code: int, message: str, *, attempts: int = 0):
        super().__init__(message, attempts=attempts)
        self.status_code = status_code


class LLMResponseError(LLMError):
    pass


class LLMPromptTooLargeError(LLMError):
    pass


@dataclass(frozen=True, slots=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class RawLLMResult:
    output: Any
    attempts: int
    elapsed_seconds: float
    request_id: str | None = None
    usage: LLMUsage = LLMUsage()


@dataclass(frozen=True, slots=True)
class LLMResult(Generic[T]):
    output: T
    attempts: int
    elapsed_seconds: float
    request_id: str | None = None
    usage: LLMUsage = LLMUsage()


def get_chat_completions_url(base_url: str | None = None) -> str:
    base = (base_url or get_settings().llm.base_url).rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


class LLMClient:
    """Synchronous, injectable client for OpenAI-compatible structured completions."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        urlopen: UrlOpen | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        monotonic: Callable[[], float] = time.monotonic,
        semaphore: threading.Semaphore | None = None,
    ):
        self.settings = settings
        self._urlopen = urlopen or urllib.request.urlopen
        self._semaphore = semaphore or _GLOBAL_LLM_SEMAPHORE
        self._sleep = sleep
        self._random_value = random_value
        self._monotonic = monotonic

    def call_structured(
        self,
        prompt: str,
        response_model: type[T],
        *,
        schema_name: str | None = None,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    ) -> LLMResult[T]:
        # Retry malformed model output once, independently of transport retries.
        # Never weaken required fields, types, finite scores or range checks.
        started = self._monotonic()
        attempts = 0
        for repair_attempt in range(2):
            try:
                raw = self.call_structured_schema(
                    prompt,
                    response_model.model_json_schema(),
                    schema_name=schema_name or response_model.__name__,
                    system_prompt=system_prompt,
                )
                attempts += raw.attempts
                payload = raw.output
                try:
                    output = response_model.model_validate(payload, strict=True)
                except ValidationError as exc:
                    # Extra keys are harmless model chatter; remove only paths
                    # identified by Pydantic, including nested candidate fields.
                    payload = copy.deepcopy(payload)
                    extras = [e for e in exc.errors() if e["type"] == "extra_forbidden"]
                    for error in extras:
                        parent = payload
                        for key in error["loc"][:-1]:
                            parent = parent[key]
                        del parent[error["loc"][-1]]
                    if extras:
                        logger.warning("Discarded %d unknown fields from %s", len(extras), response_model.__name__)
                    try:
                        output = response_model.model_validate(payload, strict=True)
                    except ValidationError as invalid:
                        raise LLMResponseError(
                            f"LLM output did not match {response_model.__name__}: {invalid}"
                        ) from invalid
                return LLMResult(
                    output=output,
                    attempts=attempts,
                    elapsed_seconds=self._monotonic() - started,
                    request_id=raw.request_id,
                    usage=raw.usage,
                )
            except LLMResponseError as error:
                attempts += error.attempts
                if repair_attempt == 1:
                    error.attempts = attempts
                    raise
                logger.warning("Retrying malformed %s output: %s", response_model.__name__, error)
                system_prompt += " Return valid JSON matching the schema exactly; include all required fields with the specified types and ranges."
            except LLMError as error:
                error.attempts += attempts
                raise
        raise AssertionError("unreachable")

    def call_structured_schema(
        self,
        prompt: str,
        json_schema: Mapping[str, Any],
        *,
        schema_name: str = "response",
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    ) -> RawLLMResult:
        prompt_chars = len(prompt) + len(system_prompt)
        if prompt_chars > self.settings.max_prompt_chars:
            raise LLMPromptTooLargeError(
                f"Prompt has {prompt_chars:,} characters; configured maximum is "
                f"{self.settings.max_prompt_chars:,}"
            )

        request_url = get_chat_completions_url(self.settings.base_url)
        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema_name,
                    "strict": True,
                    "schema": dict(json_schema),
                },
            },
        }
        if self.settings.model is not None:
            payload["model"] = self.settings.model
        request_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        api_key = self.settings.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        started = self._monotonic()
        response_body = ""
        response_headers: Any = {}
        attempts = 0

        for attempt_index in range(self.settings.max_retries + 1):
            attempts += 1
            request = urllib.request.Request(
                request_url,
                data=request_body,
                headers=headers,
                method="POST",
            )
            try:
                # Hold the global slot only for the request itself, not for
                # retry back-off sleeps.
                with (
                    self._semaphore,
                    self._urlopen(
                        request,
                        timeout=self.settings.timeout_seconds,
                    ) as response,
                ):
                    response_body = response.read().decode("utf-8")
                    response_headers = getattr(response, "headers", {})
                break
            except urllib.error.HTTPError as exc:
                error_body = exc.read().decode("utf-8", errors="replace")[:2_000]
                if self._should_retry_status(exc.code) and self._can_retry(attempt_index):
                    self._sleep_for_retry(attempt_index, getattr(exc, "headers", None))
                    continue
                raise LLMHTTPError(
                    exc.code,
                    f"HTTP {exc.code} calling chat endpoint at {request_url}: {error_body}",
                    attempts=attempts,
                ) from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if self._can_retry(attempt_index):
                    self._sleep_for_retry(attempt_index, None)
                    continue
                reason = getattr(exc, "reason", exc)
                raise LLMConnectionError(
                    f"Could not reach chat endpoint at {request_url}: {reason}",
                    attempts=attempts,
                ) from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise AssertionError("unreachable")

        elapsed = self._monotonic() - started
        try:
            completion = _strict_json_loads(
                response_body,
                context="chat completion response",
            )
            output = _content_from_completion(completion)
        except LLMError as error:
            error.attempts = attempts
            raise
        request_id = _request_id(completion, response_headers)
        usage = _usage_from_completion(completion)
        logger.info(
            "LLM completion model=%s schema=%s attempts=%d elapsed_seconds=%.3f "
            "prompt_chars=%d request_id=%s total_tokens=%s",
            self.settings.model,
            schema_name,
            attempts,
            elapsed,
            prompt_chars,
            request_id or "-",
            usage.total_tokens if usage.total_tokens is not None else "-",
        )
        return RawLLMResult(
            output=output,
            attempts=attempts,
            elapsed_seconds=elapsed,
            request_id=request_id,
            usage=usage,
        )

    def _can_retry(self, attempt_index: int) -> bool:
        return attempt_index < self.settings.max_retries

    @staticmethod
    def _should_retry_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code < 600

    def _sleep_for_retry(self, attempt_index: int, headers: Any) -> None:
        retry_after = _retry_after_seconds(headers)
        if retry_after is None:
            retry_after = (
                self.settings.retry_initial_seconds
                * self.settings.retry_multiplier**attempt_index
            )
        delay = min(retry_after, self.settings.retry_max_seconds)
        delay += self._random_value() * self.settings.retry_jitter_seconds
        if delay > 0:
            self._sleep(delay)


@lru_cache(maxsize=1)
def get_default_llm_client() -> LLMClient:
    return LLMClient(get_settings().llm)


def reset_default_llm_client() -> None:
    get_default_llm_client.cache_clear()


def call_llm_structured(
    prompt: str,
    json_schema: Mapping[str, Any],
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    schema_name: str = "response",
    system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
    timeout: int | float | None = None,
) -> Any:
    """Compatibility wrapper for callers that still provide a raw JSON schema."""

    settings = get_settings().llm
    overrides: dict[str, Any] = {}
    if base_url is not None:
        overrides["base_url"] = base_url
    if model is not None:
        overrides["model"] = model
    if api_key is not None:
        overrides["api_key"] = api_key
    if timeout is not None:
        overrides["timeout_seconds"] = timeout
    if overrides:
        settings = LLMSettings.model_validate(
            settings.model_copy(update=overrides).model_dump()
        )
    output = LLMClient(settings).call_structured_schema(
        prompt,
        json_schema,
        schema_name=schema_name,
        system_prompt=system_prompt,
    ).output
    return json.dumps(output, separators=(",", ":"))


def extract_structured_output(response: Any) -> Any:
    """Unwrap common proxy envelopes and strictly decode a JSON string."""

    if isinstance(response, dict):
        if response.get("error"):
            request_id = response.get("request_id")
            suffix = f" (request_id={request_id})" if request_id else ""
            raise LLMResponseError(f"Structured endpoint error: {response['error']}{suffix}")
        for key in ("result", "output", "data", "response", "structured_output"):
            if key in response:
                return extract_structured_output(response[key])
        return response
    if isinstance(response, list):
        if not response:
            raise LLMResponseError("Structured response list was empty")
        if len(response) == 1:
            return extract_structured_output(response[0])
        return response
    if isinstance(response, str):
        return extract_structured_output(
            _strict_json_loads(_strip_code_fences(response), context="assistant content")
        )
    return response


def _content_from_completion(completion: Any) -> Any:
    if not isinstance(completion, dict):
        raise LLMResponseError(f"Unexpected chat completion response: {completion!r}")
    if completion.get("error"):
        error = completion["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise LLMResponseError(f"Chat endpoint error: {message}")

    choices = completion.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMResponseError("Chat completion had no choices")
    choice = choices[0]
    if isinstance(choice, dict) and choice.get("finish_reason") == "length":
        raise LLMResponseError(
            "Chat completion was truncated (finish_reason=length); "
            "the provider reached its output or context token limit"
        )
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        raise LLMResponseError("Chat completion choice had no message")
    if message.get("refusal"):
        raise LLMResponseError(f"Model refused the structured request: {message['refusal']}")

    content = message.get("content")
    if isinstance(content, dict):
        return extract_structured_output(content)
    if isinstance(content, list):
        text_parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        ]
        content = "".join(text_parts)
    if not isinstance(content, str) or not content.strip():
        raise LLMResponseError("Chat completion message had no structured content")
    return extract_structured_output(content)


def _strict_json_loads(value: str, *, context: str) -> Any:
    def reject_constant(constant: str) -> None:
        raise ValueError(f"non-finite JSON number {constant}")

    try:
        return json.loads(value, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        raise LLMResponseError(f"Invalid JSON in {context}: {exc}") from exc


def _strip_code_fences(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _retry_after_seconds(headers: Any) -> float | None:
    if headers is None:
        return None
    try:
        raw = headers.get("Retry-After")
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(str(raw))
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _request_id(completion: Any, headers: Any) -> str | None:
    if headers is not None:
        for name in ("x-request-id", "request-id"):
            try:
                value = headers.get(name)
            except AttributeError:
                value = None
            if value:
                return str(value)
    if isinstance(completion, dict):
        value = completion.get("request_id")
        if value:
            return str(value)
    return None


def _usage_from_completion(completion: Any) -> LLMUsage:
    usage = completion.get("usage") if isinstance(completion, dict) else None
    if not isinstance(usage, dict):
        return LLMUsage()
    return LLMUsage(
        prompt_tokens=_optional_int(usage.get("prompt_tokens")),
        completion_tokens=_optional_int(usage.get("completion_tokens")),
        total_tokens=_optional_int(usage.get("total_tokens")),
    )


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None
