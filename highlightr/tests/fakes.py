from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.config import LLMSettings
from app.llm import LLMResult, LLMUsage

ResponseFactory = Callable[[str, type[BaseModel], str | None], Any]


class FakeLLMClient:
    def __init__(
        self,
        responses: list[Any] | None = None,
        *,
        response_factory: ResponseFactory | None = None,
        settings: LLMSettings | None = None,
        usage: LLMUsage | None = None,
    ):
        self.settings = settings or LLMSettings(
            max_retries=0,
            retry_jitter_seconds=0,
        )
        self.responses = list(responses or [])
        self.response_factory = response_factory
        self.usage = usage or LLMUsage()
        self.calls: list[tuple[str, type[BaseModel], str | None]] = []

    def call_structured(
        self,
        prompt: str,
        response_model: type[BaseModel],
        *,
        schema_name: str | None = None,
        system_prompt: str | None = None,
    ) -> LLMResult:
        del system_prompt
        self.calls.append((prompt, response_model, schema_name))
        if self.response_factory is not None:
            response = self.response_factory(prompt, response_model, schema_name)
        else:
            if not self.responses:
                raise AssertionError(
                    f"No fake response configured for {response_model.__name__}"
                )
            response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        output = (
            response
            if isinstance(response, response_model)
            else response_model.model_validate(response, strict=True)
        )
        return LLMResult(
            output=output,
            attempts=1,
            elapsed_seconds=0.01,
            usage=self.usage,
        )
