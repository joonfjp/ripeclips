from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import ClipCategoryName


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiscoveryCategory(StrictContract):
    primary: ClipCategoryName
    secondary: ClipCategoryName | None

    @model_validator(mode="after")
    def validate_distinct_categories(self) -> DiscoveryCategory:
        if self.secondary == self.primary:
            raise ValueError("secondary category must differ from primary category")
        return self


class DiscoveryCandidate(StrictContract):
    title: str
    summary: str
    reason: str
    category: DiscoveryCategory
    start_row_id: str
    end_row_id: str
    start_anchor: str
    end_anchor: str
    interestingness: float = Field(ge=0, le=1, allow_inf_nan=False)
    shareability: float = Field(ge=0, le=1, allow_inf_nan=False)
    self_contained: float = Field(ge=0, le=1, allow_inf_nan=False)
    boundary_quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    topic_relevance: float = Field(ge=0, le=1, allow_inf_nan=False)
    overall: float = Field(ge=0, le=1, allow_inf_nan=False)


class DiscoveryResponse(StrictContract):
    candidates: list[DiscoveryCandidate]

    @model_validator(mode="wrap")
    @classmethod
    def preserve_valid_candidates(cls, value: Any, handler: Any) -> Any:
        try:
            return handler(value)
        except ValidationError as error:
            # Keep valid siblings only when the failure is isolated to records.
            # All-invalid output still triggers the client's bounded retry.
            if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
                raise
            errors = error.errors()
            if any(
                len(item["loc"]) < 2 or item["loc"][0] != "candidates"
                or not isinstance(item["loc"][1], int)
                or item["type"] == "extra_forbidden"
                for item in errors
            ):
                raise
            invalid = {item["loc"][1] for item in errors}
            valid = [item for index, item in enumerate(value["candidates"]) if index not in invalid]
            if not valid:
                raise
            result = handler({**value, "candidates": valid})
            logging.getLogger(__name__).warning("Skipped %d invalid discovery candidates", len(invalid))
            return result



class TopicVerification(StrictContract):
    candidate_id: str
    match: Literal["direct", "related", "unrelated"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_quote: str
    reason: str


class TopicVerificationResponse(StrictContract):
    candidates: list[TopicVerification]


class RankedCandidate(StrictContract):
    candidate_id: str
    global_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    rank_reason: str


class GlobalRankResponse(StrictContract):
    ranked_candidates: list[RankedCandidate]


class ClipMetadata(StrictContract):
    candidate_id: str
    title_string: str
    best_headlines: list[str]
    summary: str
    best_headline: str
    best_questions: list[str]
    best_question: str
    best_description_sentences: list[str]
    best_quotes: list[str]
    hashtags: str
    keywords: str


class ClipMetadataResponse(StrictContract):
    clips: list[ClipMetadata]
