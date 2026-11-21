"""Tests for the pure logic in app/highlights.py.

These cover deterministic helpers (windowing, scoring, dedupe, selection,
formatting) without touching the LLM client.
"""

from __future__ import annotations

import re
import threading

import pytest

from app import highlights
from app.config import LLMSettings
from app.highlight_contracts import (
    ClipMetadataResponse,
    DiscoveryResponse,
    GlobalRankResponse,
    TopicVerificationResponse,
)
from app.highlights import (
    Candidate,
    _anchor_confidence,
    _apply_clip_metadata,
    _apply_topic_verification,
    _build_topic_verification_prompt,
    _build_windows,
    _calibrated_final_score,
    _candidate_id,
    _category_from_llm_item,
    _dedupe_candidates,
    _format_description,
    _is_topic_mismatch,
    _normalize_rows,
    _overlap_ratio,
    _overlap_seconds,
    _select_non_overlapping,
    _to_clip,
    format_seconds,
    identify_highlights,
    resolve_window_and_stride,
)
from app.llm import LLMConnectionError, LLMResponseError, LLMUsage
from app.models import IdentifyHighlightsRequest, Transcript, TranscriptRow
from tests.fakes import FakeLLMClient


def make_row(row_id, start, end, text="some words here"):
    return TranscriptRow(id=row_id, start_seconds=start, end_seconds=end, text=text)


def make_candidate(
    cid,
    start,
    end,
    score=0.9,
    topic_relevance=0.9,
    *,
    topic_match="unverified",
    topic_confidence=0.0,
    topic_evidence="",
):
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
        shareability=0.5,
        self_contained=0.5,
        boundary_quality=0.5,
        topic_relevance=topic_relevance,
        recovery_confidence=1.0,
        topic_match=topic_match,
        topic_verification_confidence=topic_confidence,
        topic_evidence=topic_evidence,
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


def test_category_from_llm_item_validates_the_global_taxonomy():
    category = _category_from_llm_item(
        {"primary": "debate", "secondary": "question"}
    )
    assert category is not None
    assert category.primary == "debate"
    assert category.secondary == "question"
    assert _category_from_llm_item(
        {"primary": "debate", "secondary": "debate"}
    ) is None
    assert _category_from_llm_item(
        {"primary": "advertisement", "secondary": None}
    ) is None


def test_discovery_schema_requires_shareability_score():
    candidate_reference = highlights.DISCOVERY_SCHEMA["properties"]["candidates"][
        "items"
    ]["$ref"]
    candidate_schema = highlights.DISCOVERY_SCHEMA["$defs"][
        candidate_reference.rsplit("/", 1)[-1]
    ]
    assert candidate_schema["properties"]["shareability"] == {
        "maximum": 1,
        "minimum": 0,
        "title": "Shareability",
        "type": "number",
    }
    assert "shareability" in candidate_schema["required"]


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


def test_topic_focused_dedupe_keeps_verified_direct_match():
    direct = make_candidate(
        "direct",
        0,
        100,
        score=0.75,
        topic_match="direct",
        topic_confidence=0.9,
        topic_evidence="some words",
    )
    related = make_candidate(
        "related",
        5,
        100,
        score=0.98,
        topic_match="related",
        topic_confidence=0.95,
        topic_evidence="some words",
    )

    result = _dedupe_candidates(
        [related, direct],
        threshold=0.65,
        topic_focused=True,
    )

    assert len(result) == 1
    assert result[0].id == "direct"


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


def test_topic_mismatch_requires_verified_direct_or_related_match():
    direct = make_candidate(
        "direct",
        0,
        60,
        topic_match="direct",
        topic_confidence=0.9,
        topic_evidence="some words",
    )
    related = make_candidate(
        "related",
        0,
        60,
        topic_match="related",
        topic_confidence=0.8,
        topic_evidence="some words",
    )
    unrelated = make_candidate(
        "unrelated",
        0,
        60,
        topic_match="unrelated",
        topic_confidence=0.99,
    )

    assert _is_topic_mismatch(
        direct, topics=["ai"], topic_policy="hard", admission_strictness=0.7
    ) is False
    assert _is_topic_mismatch(
        related, topics=["ai"], topic_policy="hard", admission_strictness=0.7
    ) is False
    assert _is_topic_mismatch(
        unrelated, topics=["ai"], topic_policy="hard", admission_strictness=0.7
    ) is True


def test_topic_mismatch_rejects_low_confidence_or_missing_evidence():
    low_confidence = make_candidate(
        "low",
        0,
        60,
        topic_match="direct",
        topic_confidence=0.69,
        topic_evidence="some words",
    )
    missing_evidence = make_candidate(
        "missing",
        0,
        60,
        topic_match="related",
        topic_confidence=0.9,
    )

    assert _is_topic_mismatch(
        low_confidence,
        topics=["ai"],
        topic_policy="hard",
        admission_strictness=0.7,
    ) is True
    assert _is_topic_mismatch(
        missing_evidence,
        topics=["ai"],
        topic_policy="hard",
        admission_strictness=0.7,
    ) is True


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


def test_topic_focused_final_score_rewards_verified_topic_relevance():
    relevant = _calibrated_final_score(
        global_score=0.8,
        local_score=0.8,
        recovery_confidence=1.0,
        rank=1,
        total=2,
        topic_relevance=0.95,
        topic_focused=True,
    )
    weaker_match = _calibrated_final_score(
        global_score=0.8,
        local_score=0.8,
        recovery_confidence=1.0,
        rank=1,
        total=2,
        topic_relevance=0.7,
        topic_focused=True,
    )
    assert relevant > weaker_match


# --- topic verification ---------------------------------------------------


def test_topic_verification_prompt_uses_exact_topic_and_requires_evidence():
    candidate = make_candidate("a", 0, 60)
    candidate.rows[0].text = "Artificial intelligence changes software development."

    prompt = _build_topic_verification_prompt(
        [candidate],
        ["AI"],
    )

    assert "Focus topics exactly as entered by the user: AI" in prompt
    assert "Do not invent or add focus topics" in prompt
    assert "verbatim quote" in prompt
    assert "Artificial intelligence changes software development." in prompt


def test_topic_verification_applies_valid_evidence_and_rejects_invented_quote():
    direct = make_candidate("direct", 0, 60)
    direct.rows[0].text = "Artificial intelligence changes software development."
    invalid = make_candidate("invalid", 70, 130)
    invalid.rows[0].text = "This clip is about gardening."

    _apply_topic_verification(
        [direct, invalid],
        TopicVerificationResponse.model_validate(
            {
                "candidates": [
                    {
                        "candidate_id": "direct",
                        "match": "direct",
                        "confidence": 0.92,
                        "evidence_quote": "Artificial intelligence changes software",
                        "reason": "AI is the central subject.",
                    },
                    {
                        "candidate_id": "invalid",
                        "match": "related",
                        "confidence": 0.95,
                        "evidence_quote": "AI transforms everything",
                        "reason": "Claims an AI connection.",
                    },
                ]
            },
            strict=True,
        ),
    )

    assert direct.topic_match == "direct"
    assert direct.topic_evidence == "Artificial intelligence changes software"
    assert direct.topic_relevance > 0.9
    assert invalid.topic_match == "unrelated"
    assert invalid.topic_evidence == ""
    assert invalid.topic_relevance == 0.0
# --- publishing metadata -------------------------------------------------


def test_clip_metadata_is_validated_and_normalized():
    candidate = make_candidate("a", 0, 60)
    candidate.rows[0].text = (
        "you will see me on security footage and you combine it with other evidence"
    )
    _apply_clip_metadata(
        candidate,
        {
            "title_string": "Tyler Robinson Evidence",
            "best_headlines": ["Headline One", "Headline Two", "Headline Three"],
            "summary": "Well, THAT Escalated Quickly.",
            "best_headline": "Headline Two",
            "best_questions": [
                "What did the messages establish?",
                "Which evidence changed the case?",
                "The messages changed everything",
            ],
            "best_question": "What did the messages establish?",
            "best_description_sentences": [
                "The clip discusses an identification.",
                "Security footage provides one piece of evidence.",
                "Messages provide another piece.",
            ],
            "best_quotes": [
                "you will see me",
                "security footage",
                "combine it with other evidence",
            ],
            "hashtags": "#Debate, evidence, #debate",
            "keywords": "Tyler Robinson, security footage, Tyler Robinson",
        },
    )
    assert candidate.best_headline == "Headline Two"
    assert candidate.best_questions == [
        "What did the messages establish?",
        "Which evidence changed the case?",
    ]
    assert candidate.best_question == "What did the messages establish?"
    assert candidate.summary == "well, that escalated quickly."
    assert candidate.best_quotes == [
        "you will see me",
        "security footage",
        "combine it with other evidence",
    ]
    assert candidate.hashtags == "debate, evidence"
    assert candidate.keywords == "Tyler Robinson, security footage"
    assert candidate.best_description == (
        "The clip discusses an identification. Security footage provides one piece of evidence."
        "\n\nMessages provide another piece."
    )


def test_enrichment_matches_llm_metadata_by_candidate_id():
    first = make_candidate("first", 0, 60)
    second = make_candidate("second", 70, 130)

    client = FakeLLMClient(
        [
            {
            "clips": [
                {
                    "candidate_id": "second",
                    "title_string": "Second subject",
                    "best_headlines": ["Second A", "Second B", "Second C"],
                    "summary": "The Sequel Nobody Saw Coming.",
                    "best_headline": "Second A",
                    "best_questions": [
                        "What happened second?",
                        "Why did events change?",
                        "The second act changed everything",
                    ],
                    "best_question": "What happened second?",
                    "best_description_sentences": ["The second clip description."],
                    "best_quotes": ["some", "words", "here"],
                    "hashtags": "second, clip",
                    "keywords": "second subject, clip",
                }
            ]
            }
        ]
    )
    calls = highlights._enrich_selected_clips_llm(
        [first, second],
        episode_title="Episode",
        language="en",
        llm_client=client,
    )
    assert calls == 1
    assert first.title_string == ""
    assert second.title_string == "Second subject"
    assert second.best_headline == "Second A"
    assert second.best_question == "What happened second?"


def test_clip_metadata_rejects_unknown_selections_and_non_verbatim_quotes():
    candidate = make_candidate("a", 0, 60)
    candidate.rows[0].text = "the exact words in this transcript"
    _apply_clip_metadata(
        candidate,
        {
            "best_headlines": ["One", "Two", "Three"],
            "best_headline": "Not in the list",
            "best_questions": ["Why one?", "Why two?", "Why three?"],
            "best_question": "Why four?",
            "best_description_sentences": [],
            "best_quotes": ["exact words", "invented quote", "this transcript"],
        },
    )
    assert candidate.best_headlines == ["One", "Two", "Three"]
    assert candidate.best_headline == ""
    assert candidate.best_questions == ["Why one?", "Why two?", "Why three?"]
    assert candidate.best_question == ""
    assert candidate.best_quotes == []


def test_clip_metadata_keeps_complete_headlines_and_questions_over_seven_words():
    candidate = make_candidate("a", 0, 60)
    _apply_clip_metadata(
        candidate,
        {
            "best_headlines": [
                "One two three four five six seven eight",
                "A short second headline",
                "A short third headline",
            ],
            "best_headline": "A short second headline",
            "best_questions": [
                "One two three four five six seven eight?",
                "What is the shorter hook?",
                "This hook stays brief",
            ],
            "best_question": "One two three four five six seven eight?",
        },
    )
    assert candidate.best_headlines == [
        "One two three four five six seven eight",
        "A short second headline",
        "A short third headline",
    ]
    assert candidate.best_headline == "A short second headline"
    assert candidate.best_questions[0] == (
        "One two three four five six seven eight?"
    )
    assert candidate.best_question == "One two three four five six seven eight?"


def test_enrichment_retries_only_overlong_metadata_once():
    candidate = make_candidate("retry", 0, 60)
    responses = [
        {
            "clips": [
                {
                    "candidate_id": "retry",
                    "title_string": "Evidence",
                    "best_headlines": [
                        "One two three four five six seven eight",
                        "A second short headline",
                        "A third short headline",
                    ],
                    "summary": "a cheeky summary.",
                    "best_headline": "One two three four five six seven eight",
                    "best_questions": [
                        "One two three four five six seven eight?",
                        "What does the evidence show?",
                        "The evidence changes everything",
                    ],
                    "best_question": "One two three four five six seven eight?",
                    "best_description_sentences": ["A description."],
                    "best_quotes": ["some", "words", "here"],
                    "hashtags": "evidence",
                    "keywords": "evidence",
                }
            ]
        },
        {
            "clips": [
                {
                    "candidate_id": "retry",
                    "title_string": "Evidence",
                    "best_headlines": ["Seven Words Win", "Short Second", "Short Third"],
                    "summary": "a cheeky summary.",
                    "best_headline": "Seven Words Win",
                    "best_questions": [
                        "What does the evidence prove?",
                        "Which fact changed everything?",
                        "Does the evidence prove the case?",
                    ],
                    "best_question": "What does the evidence prove?",
                    "best_description_sentences": ["A description."],
                    "best_quotes": ["some", "words", "here"],
                    "hashtags": "evidence",
                    "keywords": "evidence",
                }
            ]
        },
    ]

    client = FakeLLMClient(responses)
    calls = highlights._enrich_selected_clips_llm(
        [candidate],
        episode_title="Episode",
        language="en",
        llm_client=client,
    )
    assert calls == 2
    assert candidate.best_headline == "Seven Words Win"
    assert candidate.best_questions == [
        "What does the evidence prove?",
        "Which fact changed everything?",
        "Does the evidence prove the case?",
    ]
    assert candidate.best_question == "What does the evidence prove?"
    assert client.responses == []


def test_to_clip_keeps_partial_llm_headlines_and_fills_missing_slots():
    candidate = make_candidate("a", 0, 60)
    candidate.title = "Fallback title"
    candidate.best_headlines = ["A strong returned headline"]
    clip = _to_clip(candidate, rank=1)
    assert clip.best_headlines[0] == "A strong returned headline"
    assert len(clip.best_headlines) == 3


@pytest.mark.parametrize("title", [
    "Claude Hacked Its Own Pet And Escaped",
    "Claude Hacked Its Own Pet And Escaped Yesterday",
    "Why This Matters",
    "A Closer Look",
])
def test_to_clip_fallback_headlines_remain_distinct(title):
    candidate = make_candidate("a", 0, 60)
    candidate.title = title
    candidate.best_headlines = [title, title]
    candidate.best_headline = "A stale selection"
    clip = _to_clip(candidate, rank=1)
    assert len(clip.best_headlines) == 3
    assert len({highlights._norm(value) for value in clip.best_headlines}) == 3
    assert clip.best_headline in clip.best_headlines


def test_to_clip_keeps_partial_llm_questions_and_fills_missing_slots():
    candidate = make_candidate("a", 0, 60)
    candidate.title = "A useful debate"
    candidate.best_questions = ["Could this change the debate?"]
    candidate.best_question = "Could this change the debate?"
    clip = _to_clip(candidate, rank=1)
    assert clip.best_questions[0] == "Could this change the debate?"
    assert len(clip.best_questions) == 3
    assert clip.best_question == "Could this change the debate?"


def test_to_clip_fallback_questions_respect_character_limit():
    candidate = make_candidate("a", 0, 60)
    candidate.title = "x" * (highlights.MAX_QUESTION_CHARS + 1)
    clip = _to_clip(candidate, rank=1)
    assert all(
        len(question) <= highlights.MAX_QUESTION_CHARS
        for question in clip.best_questions
    )


def test_question_candidates_over_character_limit_trigger_metadata_retry():
    item = {
        "best_headlines": ["First", "Second", "Third"],
        "best_headline": "First",
        "best_questions": [
            "x" * (highlights.MAX_QUESTION_CHARS + 1),
            "What changed?",
            "Why now?",
        ],
        "best_question": "What changed?",
    }
    assert highlights._metadata_exceeds_word_limit(item)


def test_clip_metadata_prompt_generates_and_selects_opening_hooks():
    prompt = highlights._build_clip_metadata_prompt(
        [make_candidate("a", 0, 60)],
        episode_title="Episode",
        language="en",
    )
    assert "best_questions: write exactly three unique" in prompt
    assert "stop a cold viewer and make them keep watching" in prompt
    assert "single-line question ending in a question mark" in prompt
    assert "must not exaggerate or add an unsupported premise" in prompt
    assert "Copy the final question exactly from best_questions" in prompt
    metadata_reference = highlights.CLIP_METADATA_SCHEMA["properties"]["clips"][
        "items"
    ]["$ref"]
    metadata_schema = highlights.CLIP_METADATA_SCHEMA["$defs"][
        metadata_reference.rsplit("/", 1)[-1]
    ]
    assert "best_questions" in metadata_schema["required"]


def test_hashtag_normalization_accepts_hash_separated_model_output():
    assert highlights._normalize_csv(
        "#Evidence #CriticalThinking #DecisionMaking",
        lowercase=True,
        strip_hash=True,
    ) == "evidence, criticalthinking, decisionmaking"


def test_to_clip_populates_metadata_fallbacks():
    candidate = make_candidate("a", 0, 60)
    candidate.title = "A useful debate"
    candidate.summary = "Two speakers disagree about the available evidence."
    clip = _to_clip(candidate, rank=1)
    assert clip.title_string == "A useful debate"
    assert len(clip.best_headlines) == 3
    assert clip.best_headline in clip.best_headlines
    assert len(clip.best_questions) == 3
    assert clip.best_question in clip.best_questions
    assert len(clip.best_quotes) == 3
    assert clip.best_description == candidate.summary
    assert all(len(headline.split()) <= 7 for headline in clip.best_headlines)
    assert len(clip.best_headline.split()) <= 7
    assert all(
        len(question.split()) <= highlights.MAX_QUESTION_WORDS
        for question in clip.best_questions
    )
    assert all(question.endswith("?") for question in clip.best_questions)
    assert len(clip.best_question.split()) <= highlights.MAX_QUESTION_WORDS


def test_format_description_alternates_two_and_three_sentence_paragraphs():
    sentences = [f"Sentence {index}." for index in range(1, 11)]
    assert _format_description(sentences) == (
        "Sentence 1. Sentence 2.\n\n"
        "Sentence 3. Sentence 4. Sentence 5.\n\n"
        "Sentence 6. Sentence 7.\n\n"
        "Sentence 8. Sentence 9. Sentence 10."
    )


def test_empty_result_preserves_source_title_without_llm_call():
    result = identify_highlights(
        IdentifyHighlightsRequest(
            transcript=Transcript(
                source_id="episode_123",
                title="Candace Owens vs Andrew Wilson: The Great Debate.",
                rows=[],
            )
        )
    )
    assert result.title == "Candace Owens vs Andrew Wilson: The Great Debate."
    assert result.clips == []


@pytest.mark.parametrize("metadata_fails", [False, True])
def test_full_pipeline_uses_typed_discovery_and_metadata_contracts(metadata_fails):
    client = FakeLLMClient(
        [
            {
                "candidates": [
                    {
                        "title": "Claude Hacked Its Own Pet And Escaped",
                        "summary": "The key point lands clearly.",
                        "reason": "It stands alone.",
                        "category": {"primary": "insight", "secondary": None},
                        "start_row_id": "0",
                        "end_row_id": "0",
                        "start_anchor": "some words",
                        "end_anchor": "useful insight",
                        "interestingness": 0.9,
                        "shareability": 0.85,
                        "self_contained": 0.95,
                        "boundary_quality": 0.9,
                        "topic_relevance": 0.8,
                        "overall": 0.9,
                    }
                ]
            },
            {
                "clips": [
                    {
                        "candidate_id": "cand_b2892359",
                        "title_string": "Useful Insight",
                        "best_headlines": ["First", "Second", "Third"],
                        "summary": "the key point lands.",
                        "best_headline": "First",
                        "best_questions": [
                            "What changed?",
                            "Why now?",
                            "The insight lands",
                        ],
                        "best_question": "What changed?",
                        "best_description_sentences": ["A description."],
                        "best_quotes": [
                            "some words here",
                            "words here form",
                            "a useful insight",
                        ],
                        "hashtags": "insight, ideas, learning",
                        "keywords": "insight, ideas, learning, context, analysis",
                    }
                ]
            },
        ]
    )
    request = IdentifyHighlightsRequest.model_validate(
        {
            "transcript": {
                "source_id": "typed",
                "rows": [
                    {
                        "id": 0,
                        "start_seconds": 0,
                        "end_seconds": 60,
                        "text": "some words here form a useful insight",
                    }
                ],
            },
            "selection": {
                "min_clip_duration_seconds": 10,
                "max_clip_duration_seconds": 120,
            },
        }
    )

    if metadata_fails:
        client.responses[1] = LLMResponseError(
            "Invalid JSON in assistant content: Unterminated string", attempts=1,
        )
    result = identify_highlights(request, llm_client=client)
    assert len(result.clips) == 1
    assert result.clips[0].title_string == (
        "Claude Hacked Its Own Pet And Escaped" if metadata_fails else "Useful Insight"
    )
    assert len(result.clips[0].best_headlines) == 3
    assert result.processing_summary.llm_calls == 2


def test_topic_focused_pipeline_verifies_and_removes_unrelated_candidates():
    ai_candidate_id = _candidate_id("window_0000", 0, 0)
    garden_candidate_id = _candidate_id("window_0000", 1, 1)
    client = FakeLLMClient(
        [
            {
                "candidates": [
                    {
                        "title": "How AI models learn",
                        "summary": "Training data shapes an AI model.",
                        "reason": "It directly explains artificial intelligence.",
                        "category": {"primary": "insight", "secondary": None},
                        "start_row_id": "0",
                        "end_row_id": "0",
                        "start_anchor": "trained an AI model",
                        "end_anchor": "patterns in its training data",
                        "interestingness": 0.8,
                        "shareability": 0.8,
                        "self_contained": 0.9,
                        "boundary_quality": 0.9,
                        "topic_relevance": 0.95,
                        "overall": 0.8,
                    },
                    {
                        "title": "Growing tomatoes",
                        "summary": "A gardening tip.",
                        "reason": "It is a clear standalone story.",
                        "category": {"primary": "advice", "secondary": None},
                        "start_row_id": "1",
                        "end_row_id": "1",
                        "start_anchor": "planted tomatoes",
                        "end_anchor": "watered them every morning",
                        "interestingness": 0.95,
                        "shareability": 0.95,
                        "self_contained": 0.95,
                        "boundary_quality": 0.95,
                        "topic_relevance": 0.8,
                        "overall": 0.95,
                    },
                ]
            },
            {
                "candidates": [
                    {
                        "candidate_id": ai_candidate_id,
                        "match": "direct",
                        "confidence": 0.96,
                        "evidence_quote": "trained an AI model",
                        "reason": "The clip directly explains AI model training.",
                    },
                    {
                        "candidate_id": garden_candidate_id,
                        "match": "unrelated",
                        "confidence": 0.99,
                        "evidence_quote": "",
                        "reason": "The clip only discusses gardening.",
                    },
                ]
            },
            {
                "clips": [
                    {
                        "candidate_id": ai_candidate_id,
                        "title_string": "AI Model Training",
                        "best_headlines": [
                            "How AI Models Learn",
                            "Training Data Shapes AI",
                            "Inside an AI Model",
                        ],
                        "summary": "training data leaves fingerprints.",
                        "best_headline": "How AI Models Learn",
                        "best_questions": [
                            "How do AI models learn?",
                            "What shapes an AI model?",
                            "Can training data leave fingerprints?",
                        ],
                        "best_question": "How do AI models learn?",
                        "best_description_sentences": [
                            "Training data determines the patterns an artificial intelligence model can learn."
                        ],
                        "best_quotes": [
                            "trained an AI model",
                            "patterns in its training",
                            "its training data",
                        ],
                        "hashtags": "ai, models, training",
                        "keywords": "artificial intelligence, AI model, training data, patterns, software",
                    }
                ]
            },
        ]
    )
    request = IdentifyHighlightsRequest.model_validate(
        {
            "transcript": {
                "source_id": "topic-focused",
                "rows": [
                    {
                        "id": 0,
                        "start_seconds": 0,
                        "end_seconds": 30,
                        "text": (
                            "We trained an AI model and found patterns in its "
                            "training data."
                        ),
                    },
                    {
                        "id": 1,
                        "start_seconds": 40,
                        "end_seconds": 70,
                        "text": (
                            "I planted tomatoes and watered them every morning."
                        ),
                    },
                ],
            },
            "selection": {
                "min_clip_duration_seconds": 10,
                "max_clip_duration_seconds": 60,
                "topics": ["AI"],
                "topic_policy": "hard",
                "admission_strictness": 0.65,
            },
        }
    )

    result = identify_highlights(request, llm_client=client)

    assert len(result.clips) == 1
    assert result.clips[0].title_string == "AI Model Training"
    assert result.clips[0].topic_verification.match == "direct"
    assert result.clips[0].topic_verification.evidence_quote == "trained an AI model"
    assert result.rejected.removed_for_topic_mismatch == 1
    assert result.processing_summary.llm_calls == 3
    assert [call[1] for call in client.calls] == [
        DiscoveryResponse,
        TopicVerificationResponse,
        ClipMetadataResponse,
    ]


def test_discovery_windows_use_configured_bounded_concurrency():
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = 0
    max_active = 0

    def response_factory(_prompt, response_model, _schema_name):
        nonlocal active, max_active
        assert response_model is DiscoveryResponse
        with lock:
            active += 1
            max_active = max(max_active, active)
        barrier.wait(timeout=2)
        with lock:
            active -= 1
        return {"candidates": []}

    client = FakeLLMClient(
        response_factory=response_factory,
        settings=LLMSettings(max_concurrency=2, max_retries=0),
    )
    request = IdentifyHighlightsRequest.model_validate(
        {
            "transcript": {
                "source_id": "parallel",
                "rows": [
                    {
                        "id": 0,
                        "start_seconds": 0,
                        "end_seconds": 30,
                        "text": "first window",
                    },
                    {
                        "id": 1,
                        "start_seconds": 60,
                        "end_seconds": 90,
                        "text": "second window",
                    },
                ],
            },
            "selection": {
                "min_clip_duration_seconds": 10,
                "max_clip_duration_seconds": 60,
            },
            "processing": {
                "discovery_window_seconds": 60,
                "stride_seconds": 60,
            },
        }
    )

    result = identify_highlights(request, llm_client=client)
    assert result.processing_summary.window_count == 2
    assert result.processing_summary.llm_calls == 2
    assert max_active == 2


def test_processing_summary_sums_token_usage_across_calls():
    client = FakeLLMClient(
        response_factory=lambda _prompt, _model, _schema: {"candidates": []},
        settings=LLMSettings(max_concurrency=4, max_retries=0),
        usage=LLMUsage(prompt_tokens=100, completion_tokens=7, total_tokens=107),
    )
    rows = [
        {
            "id": index,
            "start_seconds": index * 60,
            "end_seconds": index * 60 + 30,
            "text": f"window {index}",
        }
        for index in range(5)
    ]
    request = IdentifyHighlightsRequest.model_validate(
        {
            "transcript": {"source_id": "usage", "rows": rows},
            "selection": {
                "min_clip_duration_seconds": 10,
                "max_clip_duration_seconds": 60,
            },
            "processing": {
                "discovery_window_seconds": 60,
                "stride_seconds": 60,
            },
        }
    )

    summary = identify_highlights(request, llm_client=client).processing_summary
    assert summary.llm_calls == len(client.calls) == 5
    assert summary.prompt_tokens == 100 * len(client.calls)
    assert summary.completion_tokens == 7 * len(client.calls)


def test_processing_summary_tokens_default_to_zero_without_usage():
    client = FakeLLMClient(
        response_factory=lambda _prompt, _model, _schema: {"candidates": []},
    )
    request = IdentifyHighlightsRequest.model_validate(
        {
            "transcript": {
                "source_id": "no-usage",
                "rows": [
                    {"id": 0, "start_seconds": 0, "end_seconds": 30, "text": "hi"}
                ],
            },
        }
    )
    summary = identify_highlights(request, llm_client=client).processing_summary
    assert summary.prompt_tokens == 0
    assert summary.completion_tokens == 0


def test_global_ranking_uses_tournament_batches():
    def response_factory(prompt, response_model, _schema_name):
        assert response_model is GlobalRankResponse
        candidate_ids = re.findall(r"^candidate_id: (\S+)$", prompt, flags=re.MULTILINE)
        return {
            "ranked_candidates": [
                {
                    "candidate_id": candidate_id,
                    "global_score": 0.9 - index * 0.05,
                    "rank_reason": "ranked in batch",
                }
                for index, candidate_id in enumerate(candidate_ids)
            ]
        }

    client = FakeLLMClient(
        response_factory=response_factory,
        settings=LLMSettings(ranking_batch_size=2, max_retries=0),
    )
    candidates = [
        make_candidate(str(index), index * 100, index * 100 + 60)
        for index in range(5)
    ]

    ranked, calls = highlights._global_rank_candidates_llm(
        candidates,
        "",
        [],
        "soft",
        llm_client=client,
    )
    assert calls == 4
    assert {candidate.id for candidate in ranked} == {
        candidate.id for candidate in candidates
    }
    assert all(
        len(re.findall(r"^candidate_id:", prompt, flags=re.MULTILINE)) <= 2
        for prompt, response_model, _schema_name in client.calls
        if response_model is GlobalRankResponse
    )


def test_metadata_enrichment_batches_selected_candidates():
    def response_factory(prompt, response_model, _schema_name):
        assert response_model is ClipMetadataResponse
        candidate_ids = re.findall(r"^candidate_id: (\S+)$", prompt, flags=re.MULTILINE)
        return {
            "clips": [
                {
                    "candidate_id": candidate_id,
                    "title_string": f"Subject {candidate_id}",
                    "best_headlines": ["First", "Second", "Third"],
                    "summary": "a short summary.",
                    "best_headline": "First",
                    "best_questions": [
                        "What changed?",
                        "Why now?",
                        "The truth emerges",
                    ],
                    "best_question": "What changed?",
                    "best_description_sentences": ["A description."],
                    "best_quotes": ["some", "words", "here"],
                    "hashtags": "topic, insight, podcast",
                    "keywords": "topic, insight, evidence, context, analysis",
                }
                for candidate_id in candidate_ids
            ]
        }

    client = FakeLLMClient(
        response_factory=response_factory,
        settings=LLMSettings(metadata_batch_size=2, max_retries=0),
    )
    candidates = [
        make_candidate(str(index), index * 100, index * 100 + 60)
        for index in range(5)
    ]
    calls = highlights._enrich_selected_clips_llm(
        candidates,
        episode_title="Episode",
        language="en",
        llm_client=client,
    )

    assert calls == 3
    assert all(candidate.title_string.startswith("Subject") for candidate in candidates)
    assert all(
        len(re.findall(r"^candidate_id:", prompt, flags=re.MULTILINE)) <= 2
        for prompt, response_model, _schema_name in client.calls
        if response_model is ClipMetadataResponse
    )


@pytest.mark.parametrize("failure_mode, expected_calls", [
    ("large_batches", 7),
    ("one_bad_clip", 7),
    ("all_bad", 7),
    ("connection", 1),
])
def test_metadata_failures_split_batches_and_preserve_valid_clips(
    failure_mode, expected_calls,
):
    def response_factory(prompt, response_model, _schema_name):
        assert response_model is ClipMetadataResponse
        ids = re.findall(r"^candidate_id: (\S+)$", prompt, flags=re.MULTILINE)
        if failure_mode == "connection":
            return LLMConnectionError("offline", attempts=1)
        if len(ids) > 1 or failure_mode == "all_bad" or (
            failure_mode == "one_bad_clip" and ids == ["0"]
        ):
            return LLMResponseError("Invalid JSON in assistant content", attempts=1)
        return {"clips": [{
            "candidate_id": ids[0],
            "title_string": f"Enriched {ids[0]}",
            "summary": "a recovered summary.",
            "best_headlines": [],
            "best_headline": "",
            "best_questions": [],
            "best_question": "",
            "best_description_sentences": [],
            "best_quotes": [],
            "hashtags": "",
            "keywords": "",
        }]}

    candidates = [make_candidate(str(i), i * 100, i * 100 + 60) for i in range(4)]
    for candidate in candidates:
        candidate.title = "Claude Hacked Its Own Pet And Escaped"
    client = FakeLLMClient(response_factory=response_factory)
    calls = highlights._enrich_selected_clips_llm(
        candidates, episode_title="Episode", language="en", llm_client=client,
    )
    assert calls == expected_calls == len(client.calls)
    for index, candidate in enumerate(candidates):
        clip = _to_clip(candidate, rank=index + 1)
        assert len(clip.best_headlines) == 3
        assert len(clip.best_questions) == 3
        recovered = failure_mode == "large_batches" or (
            failure_mode == "one_bad_clip" and index != 0
        )
        assert clip.title_string == (f"Enriched {index}" if recovered else candidate.title)
