"""Tests for Pydantic model validation in app/models.py."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    ClipCategory,
    JobProgress,
    SelectionOptions,
    Transcript,
    TranscriptRow,
)


def test_transcript_row_valid():
    row = TranscriptRow(id=0, start_seconds=0, end_seconds=5, text="hello")
    assert row.end_seconds == 5
    assert row.speaker is None


def test_transcript_row_rejects_end_before_start():
    with pytest.raises(ValidationError):
        TranscriptRow(id=0, start_seconds=10, end_seconds=5, text="hi")


def test_transcript_row_rejects_empty_text():
    with pytest.raises(ValidationError):
        TranscriptRow(id=0, start_seconds=0, end_seconds=5, text="")


def test_transcript_row_rejects_negative_start():
    with pytest.raises(ValidationError):
        TranscriptRow(id=0, start_seconds=-1, end_seconds=5, text="hi")


def test_transcript_row_allows_equal_start_end():
    row = TranscriptRow(id=0, start_seconds=5, end_seconds=5, text="tick")
    assert row.start_seconds == row.end_seconds


def test_transcript_row_rejects_nonfinite_times():
    with pytest.raises(ValidationError):
        TranscriptRow(
            id=0,
            start_seconds=0,
            end_seconds=float("inf"),
            text="hi",
        )


def test_transcript_rejects_row_ids_that_collide_when_stringified():
    with pytest.raises(ValidationError):
        Transcript(
            source_id="source",
            rows=[
                TranscriptRow(id=1, start_seconds=0, end_seconds=1, text="one"),
                TranscriptRow(id="1", start_seconds=1, end_seconds=2, text="two"),
            ],
        )


def test_selection_options_defaults():
    options = SelectionOptions()
    assert options.max_clip_duration_seconds == 1200
    assert options.min_clip_duration_seconds == 60
    assert options.topic_policy == "soft"
    assert options.topics == []


def test_selection_options_rejects_max_below_min():
    with pytest.raises(ValidationError):
        SelectionOptions(max_clip_duration_seconds=30, min_clip_duration_seconds=60)


def test_selection_options_strips_and_drops_blank_topics():
    options = SelectionOptions(topics=["  ai strategy  ", "", "   ", "pricing"])
    assert options.topics == ["ai strategy", "pricing"]


def test_selection_options_maps_legacy_topic_only_policy_to_hard():
    options = SelectionOptions(topic_policy="only")
    assert options.topic_policy == "hard"


def test_selection_options_rejects_out_of_range_strictness():
    with pytest.raises(ValidationError):
        SelectionOptions(admission_strictness=1.5)


def test_selection_options_rejects_invalid_topic_policy():
    with pytest.raises(ValidationError):
        SelectionOptions(topic_policy="aggressive")


def test_clip_category_accepts_distinct_primary_and_secondary():
    category = ClipCategory(primary="debate", secondary="question")
    assert category.primary == "debate"
    assert category.secondary == "question"


def test_clip_category_allows_no_secondary():
    category = ClipCategory(primary="insight", secondary=None)
    assert category.secondary is None


def test_clip_category_rejects_unknown_or_duplicate_values():
    with pytest.raises(ValidationError):
        ClipCategory(primary="advertisement")
    with pytest.raises(ValidationError):
        ClipCategory(primary="story", secondary="story")


def test_job_progress_rejects_done_above_total():
    with pytest.raises(ValidationError):
        JobProgress(windows_done=2, windows_total=1)
