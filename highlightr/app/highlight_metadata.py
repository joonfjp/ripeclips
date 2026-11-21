from __future__ import annotations

import re
from typing import Any

from .highlight_candidates import (
    Candidate,
    _anchor,
    _norm,
    _title_from_rows,
)
from .models import (
    Anchors,
    ClipCategory,
    ClipSource,
    HighlightClip,
    ScoreBreakdown,
    TopicVerificationDetails,
    TranscriptRow,
)

MAX_QUESTION_WORDS = 6
MAX_QUESTION_CHARS = 100


def _apply_clip_metadata(candidate: Candidate, item: dict[str, Any]) -> None:
    candidate.best_headlines = _unique_strings(item.get("best_headlines"))[:3]
    best_headline = str(item.get("best_headline", "")).strip()
    if best_headline in candidate.best_headlines:
        candidate.best_headline = best_headline
    candidate.best_questions = [
        question
        for question in _unique_strings(item.get("best_questions"))
        if question.endswith("?")
    ][:3]
    best_question = str(item.get("best_question", "")).strip()
    if best_question in candidate.best_questions:
        candidate.best_question = best_question

    transcript = " ".join(
        row.text.strip() for row in candidate.rows if row.text.strip()
    )
    quotes = [
        quote
        for quote in _unique_strings(item.get("best_quotes"))
        if _norm(quote) and _norm(quote) in _norm(transcript)
    ]
    if len(quotes) == 3:
        candidate.best_quotes = quotes

    candidate.title_string = str(item.get("title_string", "")).strip()
    summary = " ".join(str(item.get("summary", "")).split()).lower()
    if summary:
        candidate.summary = summary
    candidate.best_description = _format_description(
        _unique_strings(item.get("best_description_sentences"))
    )
    candidate.hashtags = _normalize_csv(
        item.get("hashtags"),
        lowercase=True,
        strip_hash=True,
    )
    candidate.keywords = _normalize_csv(item.get("keywords"))


def _unique_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item).strip()
        key = _norm(text)
        if text and key not in seen:
            result.append(text)
            seen.add(key)
    return result


def _normalize_csv(
    value: Any,
    *,
    lowercase: bool = False,
    strip_hash: bool = False,
) -> str:
    parts: list[str] = []
    for chunk in str(value or "").split(","):
        hash_tags = re.findall(r"#\s*([^#,\s]+)", chunk) if strip_hash else []
        parts.extend(hash_tags or [chunk])
    cleaned: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = part.strip()
        if strip_hash:
            text = text.lstrip("#").strip()
        if lowercase:
            text = text.lower()
        key = _norm(text)
        if text and key not in seen:
            cleaned.append(text)
            seen.add(key)
    return ", ".join(cleaned)


def _limit_words(value: str, maximum: int) -> str:
    return " ".join(value.split()[:maximum]).strip()


def _metadata_exceeds_word_limit(
    item: dict[str, Any],
    maximum: int = 7,
) -> bool:
    headline_values = _unique_strings(item.get("best_headlines"))
    headline_values.append(str(item.get("best_headline", "")).strip())
    if any(len(value.split()) > maximum for value in headline_values if value):
        return True

    question_values = _unique_strings(item.get("best_questions"))
    question_values.append(str(item.get("best_question", "")).strip())
    return any(_question_exceeds_limits(value) for value in question_values if value)


def _question_exceeds_limits(value: str) -> bool:
    return (
        len(value.split()) > MAX_QUESTION_WORDS
        or len(value) > MAX_QUESTION_CHARS
        or "\n" in value
        or "\r" in value
    )


def _format_description(sentences: list[str]) -> str:
    paragraphs: list[str] = []
    index = 0
    group_index = 0
    group_sizes = (2, 3)
    while index < len(sentences):
        size = group_sizes[group_index % len(group_sizes)]
        paragraph = " ".join(sentences[index : index + size]).strip()
        if paragraph:
            paragraphs.append(paragraph)
        index += size
        group_index += 1
    return "\n\n".join(paragraphs)


def _to_clip(candidate: Candidate, *, rank: int) -> HighlightClip:
    fallback_title = candidate.title or _title_from_rows(candidate.rows)
    short_title = _limit_words(fallback_title, 7)
    fallback_headlines = [
        short_title,
        f"{_limit_words(short_title, 5)}: What Happened",
        f"Why {_limit_words(short_title, 5)} Matters",
        f"A Closer Look: {_limit_words(short_title, 4)}",
    ]
    headlines = _unique_strings([*candidate.best_headlines, *fallback_headlines])[:3]
    question_topic = _limit_words(short_title, 3) or "this"
    if len(f"What makes {question_topic} important?") > MAX_QUESTION_CHARS:
        question_topic = "this"
    fallback_questions = [
        f"Why does {question_topic} matter?",
        f"What makes {question_topic} important?",
        f"Could {question_topic} change everything?",
    ]
    valid_questions = [
        question
        for question in candidate.best_questions
        if question.endswith("?") and not _question_exceeds_limits(question)
    ]
    questions = _unique_strings([*valid_questions, *fallback_questions])[:3]
    best_question = (
        candidate.best_question
        if candidate.best_question in questions
        else questions[0]
    )
    quotes = candidate.best_quotes or _fallback_quotes(candidate.rows)
    return HighlightClip(
        clip_id=f"clip_{rank:03d}",
        rank=rank,
        start_seconds=round(candidate.start, 3),
        end_seconds=round(candidate.end, 3),
        duration_seconds=round(candidate.duration, 3),
        start_row_id=candidate.start_row.id,
        end_row_id=candidate.end_row.id,
        title=fallback_title,
        summary=candidate.summary
        or (
            f"Candidate span from transcript rows {candidate.start_row.id} "
            f"through {candidate.end_row.id}."
        ),
        reason=candidate.reason
        or (
            "Selected by structured LLM discovery, recovery validation, "
            "and non-overlap constraints."
        ),
        title_string=candidate.title_string or fallback_title,
        best_headlines=headlines,
        best_headline=(
            candidate.best_headline
            if candidate.best_headline in headlines
            else headlines[0]
        ),
        best_questions=questions,
        best_question=best_question,
        best_description=candidate.best_description
        or candidate.summary
        or f"This clip discusses {fallback_title}.",
        best_quotes=quotes,
        hashtags=candidate.hashtags or _fallback_hashtags(candidate),
        keywords=candidate.keywords or fallback_title,
        category=ClipCategory(
            primary=candidate.category_primary,
            secondary=candidate.category_secondary,
        ),
        scores=ScoreBreakdown(
            overall=round(candidate.score, 4),
            interestingness=round(candidate.interestingness, 4),
            shareability=round(candidate.shareability, 4),
            self_contained=round(candidate.self_contained, 4),
            boundary_quality=round(candidate.boundary_quality, 4),
            topic_relevance=round(candidate.topic_relevance, 4),
            recovery_confidence=round(candidate.recovery_confidence, 4),
        ),
        topic_verification=TopicVerificationDetails(
            match=candidate.topic_match,
            confidence=round(candidate.topic_verification_confidence, 4),
            evidence_quote=candidate.topic_evidence,
            reason=candidate.topic_verification_reason,
        ),
        anchors=Anchors(
            start=candidate.start_anchor or _anchor(candidate.start_row.text),
            end=candidate.end_anchor or _anchor(candidate.end_row.text),
        ),
        source=ClipSource(
            window_ids=sorted(candidate.window_ids),
            candidate_ids=sorted(candidate.candidate_ids),
        ),
    )


def _fallback_quotes(rows: list[TranscriptRow]) -> list[str]:
    text = " ".join(row.text.strip() for row in rows if row.text.strip())
    words = text.split()
    if not words:
        return ["", "", ""]
    width = min(12, max(1, len(words) // 3))
    starts = (
        0,
        max(0, (len(words) - width) // 2),
        max(0, len(words) - width),
    )
    return [" ".join(words[start : start + width]) for start in starts]


def _fallback_hashtags(candidate: Candidate) -> str:
    categories = [candidate.category_primary, candidate.category_secondary]
    return ", ".join(category for category in categories if category)
