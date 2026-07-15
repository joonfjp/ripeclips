from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


RowId = int | str


class TranscriptRow(BaseModel):
    id: RowId
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    speaker: str | None = None
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_time_order(self) -> "TranscriptRow":
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        return self


class Transcript(BaseModel):
    source_id: str
    language: str | None = None
    rows: list[TranscriptRow]


class SelectionOptions(BaseModel):
    max_clip_duration_seconds: int = Field(default=1200, gt=0)
    min_clip_duration_seconds: int = Field(default=60, gt=0)
    max_clips: int | None = Field(default=None, gt=0)
    allow_overlap_seconds: int = Field(default=0, ge=0)
    target: str = "best"
    style: str | None = "insightful, surprising, emotionally engaging"
    topics: list[str] = Field(default_factory=list)
    topic_policy: Literal["soft", "hard", "only"] = "soft"
    admission_strictness: float = Field(default=0.7, ge=0, le=1)
    min_candidates_per_window: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_duration_bounds(self) -> "SelectionOptions":
        if self.max_clip_duration_seconds < self.min_clip_duration_seconds:
            raise ValueError("max_clip_duration_seconds must be >= min_clip_duration_seconds")
        self.topics = [topic.strip() for topic in self.topics if topic.strip()]
        return self


class ProcessingOptions(BaseModel):
    discovery_window_seconds: int | None = Field(default=None, gt=0)
    stride_seconds: int | None = Field(default=None, gt=0)
    candidates_per_window: int = Field(default=5, gt=0)
    dedupe_overlap_threshold: float = Field(default=0.65, ge=0, le=1)


class IdentifyHighlightsRequest(BaseModel):
    transcript: Transcript
    selection: SelectionOptions = Field(default_factory=SelectionOptions)
    processing: ProcessingOptions = Field(default_factory=ProcessingOptions)


class ScoreBreakdown(BaseModel):
    overall: float
    interestingness: float
    self_contained: float
    boundary_quality: float
    topic_relevance: float
    recovery_confidence: float


class Anchors(BaseModel):
    start: str
    end: str


class ClipSource(BaseModel):
    window_ids: list[str]
    candidate_ids: list[str]


class HighlightClip(BaseModel):
    clip_id: str
    rank: int
    start_seconds: float
    end_seconds: float
    duration_seconds: float
    start_row_id: RowId
    end_row_id: RowId
    title: str
    summary: str
    reason: str
    scores: ScoreBreakdown
    anchors: Anchors
    source: ClipSource


class RejectedStats(BaseModel):
    raw_candidates: int
    deduped_candidates: int
    removed_for_overlap: int
    removed_for_duration: int
    removed_for_low_confidence: int
    removed_for_low_score: int
    removed_for_topic_mismatch: int


class ProcessingSummary(BaseModel):
    window_count: int
    llm_calls: int
    mode: str
    version: str
    discovery_window_seconds: int
    stride_seconds: int


class HighlightResult(BaseModel):
    source_id: str
    clips: list[HighlightClip]
    rejected: RejectedStats
    processing_summary: ProcessingSummary


class JobProgress(BaseModel):
    windows_done: int
    windows_total: int


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "completed", "failed"]
    stage: str
    progress: JobProgress
    error: str | None = None
