"""Tests for the pure logic in app/highlights.py.

These cover deterministic helpers (windowing, scoring, dedupe, selection,
formatting) without touching the LLM client.
"""

from __future__ import annotations

from app import highlights
from app.highlights import (
    Candidate,
    _anchor_confidence,
    _build_windows,
    _calibrated_final_score,
    _dedupe_candidates,
    _is_topic_mismatch,
    _normalize_rows,
    _overlap_ratio,
    _overlap_seconds,
    _select_non_overlapping,
    format_seconds,
    resolve_window_and_stride,
)
from app.models import TranscriptRow


def make_row(row_id, start, end, text="some words here"):
    return TranscriptRow(id=row_id, start_seconds=start, end_seconds=end, text=text)


def make_candidate(cid, start, end, score=0.9, topic_relevance=0.9):
    row = make_row(0, start, end)
    return Candidate(
        id=cid,
        start=start,
        end=end,
        start_row=row,
        end_row=row,
        rows=[row],
        score=score,
        interestingness=0.5,
        self_contained=0.5,
        boundary_quality=0.5,
        topic_relevance=topic_relevance,
        recovery_confidence=1.0,
        window_ids={f"w_{cid}"},
        candidate_ids={cid},
    )


# --- format_seconds -------------------------------------------------------


def test_format_seconds_hms():
    assert format_seconds(0) == "00:00:00"
    assert format_seconds(65) == "00:01:05"
    assert format_seconds(3661) == "01:01:01"


def test_format_seconds_truncates_fractional():
    assert format_seconds(59.9) == "00:00:59"


# --- resolve_window_and_stride -------------------------------------------


def test_resolve_window_and_stride_uses_explicit_values():
    window, stride = resolve_window_and_stride(1200, 900, 300)
    assert window == 900
    assert stride == 300


def test_resolve_window_and_stride_auto_scales_and_floors():
    # Short max clip -> window floored at 600, stride floored at 60.
    window, stride = resolve_window_and_stride(60, None, None)
    assert window == 600
    assert stride == 300  # window // 2


def test_resolve_window_and_stride_caps_window():
    window, _ = resolve_window_and_stride(100000, None, None)
    assert window == 1800  # ceiling


def test_resolve_window_and_stride_stride_never_exceeds_window():
    window, stride = resolve_window_and_stride(1200, 300, 900)
    assert stride <= window
    assert stride == 300


# --- _normalize_rows ------------------------------------------------------


def test_normalize_rows_drops_blank_text_and_sorts():
    rows = [
        make_row(0, 30, 40, "later"),
        make_row(1, 0, 10, "   "),  # blank -> dropped
        make_row(2, 5, 15, "earlier"),
    ]
    result = _normalize_rows(rows)
    assert [r.text for r in result] == ["earlier", "later"]
    assert result[0].start_seconds == 5


# --- _build_windows -------------------------------------------------------


def test_build_windows_single_window_when_short():
    rows = [make_row(i, i * 10, i * 10 + 10) for i in range(3)]  # 0..30s
    windows = _build_windows(rows, window_seconds=600, stride_seconds=300)
    assert len(windows) == 1
    assert windows[0].id == "window_0000"
    assert windows[0].start == 0
    assert windows[0].end == 30


def test_build_windows_overlapping_coverage():
    # 100s of transcript, window 40s, stride 20s.
    rows = [make_row(i, i * 10, i * 10 + 10) for i in range(10)]  # 0..100s
    windows = _build_windows(rows, window_seconds=40, stride_seconds=20)
    assert len(windows) > 1
    # Each window carries only rows that intersect its time span.
    for window in windows:
        for row in window.rows:
            assert row.start_seconds < window.end
            assert row.end_seconds > window.start


def test_build_windows_empty_rows():
    assert _build_windows([], window_seconds=600, stride_seconds=300) == []


# --- overlap helpers ------------------------------------------------------


def test_overlap_seconds_and_ratio():
    a = make_candidate("a", 0, 100)
    b = make_candidate("b", 50, 150)
    assert _overlap_seconds(a, b) == 50.0
    # shorter duration is 100, overlap 50 -> ratio 0.5
    assert _overlap_ratio(a, b) == 0.5


def test_overlap_seconds_no_overlap():
    a = make_candidate("a", 0, 40)
    b = make_candidate("b", 60, 100)
    assert _overlap_seconds(a, b) == 0.0
    assert _overlap_ratio(a, b) == 0.0


# --- _dedupe_candidates ---------------------------------------------------


def test_dedupe_merges_high_overlap_keeps_higher_score():
    high = make_candidate("high", 0, 100, score=0.95)
    dup = make_candidate("dup", 5, 100, score=0.80)  # ~95% overlap
    result = _dedupe_candidates([dup, high], threshold=0.65)
    assert len(result) == 1
    kept = result[0]
    assert kept.id == "high"
    # Merged candidate absorbs the duplicate's provenance.
    assert "dup" in kept.candidate_ids


def test_dedupe_keeps_distinct_candidates():
    a = make_candidate("a", 0, 40)
    b = make_candidate("b", 200, 260)
    result = _dedupe_candidates([a, b], threshold=0.65)
    assert len(result) == 2


# --- _select_non_overlapping ---------------------------------------------


def test_select_non_overlapping_prunes_overlaps():
    a = make_candidate("a", 0, 100)
    b = make_candidate("b", 50, 150)  # overlaps a
    c = make_candidate("c", 200, 260)
    selected, removed = _select_non_overlapping(
        [a, b, c], max_clips=None, allow_overlap_seconds=0
    )
    ids = {cand.id for cand in selected}
    assert ids == {"a", "c"}
    assert removed == 1


def test_select_non_overlapping_respects_max_clips():
    cands = [make_candidate(str(i), i * 100, i * 100 + 50) for i in range(5)]
    selected, _ = _select_non_overlapping(cands, max_clips=2, allow_overlap_seconds=0)
    assert len(selected) == 2


def test_select_non_overlapping_allows_small_overlap():
    a = make_candidate("a", 0, 100)
    b = make_candidate("b", 98, 200)  # 2s overlap, allowed up to 5
    selected, removed = _select_non_overlapping(
        [a, b], max_clips=None, allow_overlap_seconds=5
    )
    assert len(selected) == 2
    assert removed == 0


# --- _anchor_confidence ---------------------------------------------------


def test_anchor_confidence_both_anchors_match():
    rows = [make_row(0, 0, 10, "the quick brown fox jumps over the lazy dog")]
    conf = _anchor_confidence(rows, "the quick brown", "lazy dog")
    assert conf == 1.0


def test_anchor_confidence_one_anchor_match():
    rows = [make_row(0, 0, 10, "the quick brown fox")]
    conf = _anchor_confidence(rows, "the quick brown", "not present here")
    assert conf == 0.75


def test_anchor_confidence_no_match():
    rows = [make_row(0, 0, 10, "the quick brown fox")]
    conf = _anchor_confidence(rows, "totally absent", "also missing")
    assert conf == 0.55


# --- _is_topic_mismatch ---------------------------------------------------


def test_topic_mismatch_soft_policy_never_mismatches():
    cand = make_candidate("a", 0, 60, topic_relevance=0.0)
    assert _is_topic_mismatch(
        cand, topics=["ai"], topic_policy="soft", admission_strictness=0.7
    ) is False


def test_topic_mismatch_no_topics_never_mismatches():
    cand = make_candidate("a", 0, 60, topic_relevance=0.0)
    assert _is_topic_mismatch(
        cand, topics=[], topic_policy="only", admission_strictness=0.7
    ) is False


def test_topic_mismatch_only_policy_rejects_low_relevance():
    low = make_candidate("a", 0, 60, topic_relevance=0.3)
    high = make_candidate("b", 0, 60, topic_relevance=0.95)
    assert _is_topic_mismatch(
        low, topics=["ai"], topic_policy="only", admission_strictness=0.7
    ) is True
    assert _is_topic_mismatch(
        high, topics=["ai"], topic_policy="only", admission_strictness=0.7
    ) is False


# --- _calibrated_final_score ---------------------------------------------


def test_calibrated_final_score_single_candidate_in_range():
    score = _calibrated_final_score(
        global_score=0.9,
        local_score=0.8,
        recovery_confidence=1.0,
        rank=1,
        total=1,
    )
    assert 0.0 <= score <= 1.0


def test_calibrated_final_score_rank_prior_favors_top_rank():
    top = _calibrated_final_score(
        global_score=0.8, local_score=0.8, recovery_confidence=1.0, rank=1, total=5
    )
    bottom = _calibrated_final_score(
        global_score=0.8, local_score=0.8, recovery_confidence=1.0, rank=5, total=5
    )
    assert top > bottom


def test_calibrated_final_score_is_clamped():
    score = _calibrated_final_score(
        global_score=1.0, local_score=1.0, recovery_confidence=1.0, rank=1, total=1
    )
    assert score <= 1.0
