from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RowId = int | str
ClipCategoryName = Literal[
    "question",
    "insight",
    "story",
    "advice",
    "opinion",
    "debate",
    "prediction",
    "humor",
]


class TranscriptRow(BaseModel):
    id: RowId
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    speaker: str | None = None
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_order(self) -> TranscriptRow:
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        return self


class Transcript(BaseModel):
    source_id: str = Field(min_length=1)
    title: str | None = Field(
        default=None,
        description="Authoritative title of the source episode or video.",
    )
    language: str | None = None
    rows: list[TranscriptRow]

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("source_id must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_unique_row_ids(self) -> Transcript:
        normalized_ids = [str(row.id) for row in self.rows]
        if len(normalized_ids) != len(set(normalized_ids)):
            raise ValueError("transcript row IDs must be unique after string conversion")
        return self


class SelectionOptions(BaseModel):
    max_clip_duration_seconds: int = Field(default=1200, gt=0)
    min_clip_duration_seconds: int = Field(default=60, gt=0)
    max_clips: int | None = Field(default=None, gt=0)
    allow_overlap_seconds: int = Field(default=0, ge=0)
    target: str = "best"
    style: str | None = "insightful, surprising, emotionally engaging"
    topics: list[str] = Field(default_factory=list)
    topic_policy: Literal["soft", "hard", "only"] = "soft"
    admission_strictness: float = Field(
        default=0.7,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    min_candidates_per_window: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_duration_bounds(self) -> SelectionOptions:
        if self.max_clip_duration_seconds < self.min_clip_duration_seconds:
            raise ValueError("max_clip_duration_seconds must be >= min_clip_duration_seconds")
        self.topics = [topic.strip() for topic in self.topics if topic.strip()]
        # "only" was the former stricter sibling of "hard". Both now share one
        # independently verified topic-focused behavior, while the legacy value
        # remains accepted so saved jobs and API clients continue to work.
        if self.topic_policy == "only":
            self.topic_policy = "hard"
        return self


class ProcessingOptions(BaseModel):
    discovery_window_seconds: int | None = Field(default=None, gt=0)
    stride_seconds: int | None = Field(default=None, gt=0)
    candidates_per_window: int = Field(default=5, gt=0)
    dedupe_overlap_threshold: float = Field(
        default=0.65,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )


class IdentifyHighlightsRequest(BaseModel):
    transcript: Transcript
    selection: SelectionOptions = Field(default_factory=SelectionOptions)
    processing: ProcessingOptions = Field(default_factory=ProcessingOptions)


class ScoreBreakdown(BaseModel):
    overall: float = Field(ge=0, le=1, allow_inf_nan=False)
    interestingness: float = Field(ge=0, le=1, allow_inf_nan=False)
    shareability: float = Field(ge=0, le=1, allow_inf_nan=False)
    self_contained: float = Field(ge=0, le=1, allow_inf_nan=False)
    boundary_quality: float = Field(ge=0, le=1, allow_inf_nan=False)
    topic_relevance: float = Field(ge=0, le=1, allow_inf_nan=False)
    recovery_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)


class Anchors(BaseModel):
    start: str
    end: str


class ClipSource(BaseModel):
    window_ids: list[str]
    candidate_ids: list[str]


class ClipCategory(BaseModel):
    primary: ClipCategoryName
    secondary: ClipCategoryName | None = None

    @model_validator(mode="after")
    def validate_distinct_categories(self) -> ClipCategory:
        if self.secondary == self.primary:
            raise ValueError("secondary category must differ from primary category")
        return self


class TopicVerificationDetails(BaseModel):
    match: Literal["direct", "related", "unrelated", "unverified"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    evidence_quote: str
    reason: str


class HighlightClip(BaseModel):
    clip_id: str
    rank: int
    start_seconds: float = Field(ge=0, allow_inf_nan=False)
    end_seconds: float = Field(ge=0, allow_inf_nan=False)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    start_row_id: RowId
    end_row_id: RowId
    title: str
    summary: str
    reason: str
    title_string: str = Field(
        description="Concise label for the principal people, organizations, or subject."
    )
    best_headlines: list[str] = Field(
        min_length=3,
        max_length=3,
        description="Three distinct, accurate social-video headlines of at most seven words each.",
    )
    best_headline: str = Field(
        description="Strongest headline, selected exactly from best_headlines, at most seven words."
    )
    best_questions: list[str] = Field(
        min_length=3,
        max_length=3,
        description=(
            "Three distinct, transcript-supported opening questions ending in question marks, "
            "of at most six words each."
        ),
    )
    best_question: str = Field(
        description=(
            "Strongest opening question ending in a question mark, selected exactly from "
            "best_questions, at most six words."
        )
    )
    best_description: str = Field(
        description=(
            "Standalone transcript-supported context and significance, with paragraphs "
            "separated by two newline characters in alternating groups of two and three sentences."
        )
    )
    best_quotes: list[str] = Field(
        min_length=3,
        max_length=3,
        description="Three short verbatim excerpts from the selected transcript span.",
    )
    hashtags: str = Field(
        description="Comma-separated lowercase discovery tags without hash characters."
    )
    keywords: str = Field(
        description="Comma-separated proper nouns, subjects, entities, and search phrases."
    )
    category: ClipCategory
    scores: ScoreBreakdown
    topic_verification: TopicVerificationDetails
    anchors: Anchors
    source: ClipSource


class RejectedStats(BaseModel):
    raw_candidates: int = Field(ge=0)
    deduped_candidates: int = Field(ge=0)
    removed_for_overlap: int = Field(ge=0)
    removed_for_duration: int = Field(ge=0)
    removed_for_low_confidence: int = Field(ge=0)
    removed_for_low_score: int = Field(ge=0)
    removed_for_topic_mismatch: int = Field(ge=0)
    removed_as_duplicate: int = Field(default=0, ge=0)
    removed_for_limit: int = Field(default=0, ge=0)


class ProcessingSummary(BaseModel):
    warnings: list[str] = Field(default_factory=list)
    window_count: int = Field(ge=0)
    llm_calls: int = Field(ge=0)
    mode: str
    version: str
    discovery_window_seconds: int = Field(gt=0)
    stride_seconds: int = Field(gt=0)
    # Summed provider-reported usage; calls whose response omits usage add 0.
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)


class HighlightResult(BaseModel):
    source_id: str
    title: str = Field(description="Authoritative title of the source episode or video.")
    clips: list[HighlightClip]
    rejected: RejectedStats
    processing_summary: ProcessingSummary


class ClipMetadataResult(BaseModel):
    source_id: str
    title: str = Field(description="Title supplied for the clip, or its source_id.")
    clip: HighlightClip
    llm_calls: int = Field(ge=0)


class JobProgress(BaseModel):
    windows_done: int = Field(ge=0)
    windows_total: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_progress(self) -> JobProgress:
        if self.windows_done > self.windows_total:
            raise ValueError("windows_done must not exceed windows_total")
        return self


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    stage: str
    progress: JobProgress
    error: str | None = None
