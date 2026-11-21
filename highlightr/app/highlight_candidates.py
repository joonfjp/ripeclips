from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from typing import Any

from .highlight_contracts import DiscoveryCandidate
from .models import (
    ClipCategory,
    ClipCategoryName,
    IdentifyHighlightsRequest,
    TranscriptRow,
)


@dataclass(slots=True)
class Window:
    id: str
    start: float
    end: float
    rows: list[TranscriptRow]


@dataclass(slots=True)
class Candidate:
    id: str
    start: float
    end: float
    start_row: TranscriptRow
    end_row: TranscriptRow
    rows: list[TranscriptRow]
    score: float
    interestingness: float
    shareability: float
    self_contained: float
    boundary_quality: float
    topic_relevance: float
    recovery_confidence: float
    topic_match: str = "unverified"
    topic_verification_confidence: float = 0.0
    topic_evidence: str = ""
    topic_verification_reason: str = ""
    title: str = ""
    summary: str = ""
    reason: str = ""
    title_string: str = ""
    best_headlines: list[str] = field(default_factory=list)
    best_headline: str = ""
    best_questions: list[str] = field(default_factory=list)
    best_question: str = ""
    best_description: str = ""
    best_quotes: list[str] = field(default_factory=list)
    hashtags: str = ""
    keywords: str = ""
    start_anchor: str = ""
    end_anchor: str = ""
    category_primary: ClipCategoryName = "insight"
    category_secondary: ClipCategoryName | None = None
    window_ids: set[str] = field(default_factory=set)
    candidate_ids: set[str] = field(default_factory=set)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def resolve_window_and_stride(
    max_clip_duration_seconds: int,
    discovery_window_seconds: int | None,
    stride_seconds: int | None,
) -> tuple[int, int]:
    window = discovery_window_seconds or _smart_discovery_window(
        max_clip_duration_seconds
    )
    stride = stride_seconds or _smart_stride(window)
    return window, min(stride, window)


def _smart_discovery_window(max_clip_duration_seconds: int) -> int:
    return max(600, min(int(max_clip_duration_seconds * 1.35), 1800))


def _smart_stride(discovery_window_seconds: int) -> int:
    return max(60, min(600, discovery_window_seconds // 2))


def _normalize_rows(rows: list[TranscriptRow]) -> list[TranscriptRow]:
    cleaned = [row for row in rows if row.text.strip()]
    return sorted(cleaned, key=lambda row: (row.start_seconds, row.end_seconds))


def _build_windows(
    rows: list[TranscriptRow],
    *,
    window_seconds: int,
    stride_seconds: int,
) -> list[Window]:
    if not rows:
        return []

    transcript_start = rows[0].start_seconds
    transcript_end = max(row.end_seconds for row in rows)
    windows: list[Window] = []
    start = transcript_start
    index = 0
    right = 0

    while start < transcript_end:
        end = min(start + window_seconds, transcript_end)
        while right < len(rows) and rows[right].start_seconds < end:
            right += 1
        window_rows = [row for row in rows[:right] if row.end_seconds > start]
        if window_rows:
            windows.append(
                Window(
                    id=f"window_{index:04d}",
                    start=start,
                    end=end,
                    rows=window_rows,
                )
            )
            index += 1
        if end >= transcript_end:
            break
        start += stride_seconds

    return windows


def plan_highlight_windows(request: IdentifyHighlightsRequest) -> list[Window]:
    rows = _normalize_rows(request.transcript.rows)
    window_seconds, stride_seconds = resolve_window_and_stride(
        request.selection.max_clip_duration_seconds,
        request.processing.discovery_window_seconds,
        request.processing.stride_seconds,
    )
    return _build_windows(
        rows,
        window_seconds=window_seconds,
        stride_seconds=stride_seconds,
    )


def _candidate_from_llm_item(
    item: DiscoveryCandidate,
    window: Window,
    row_by_id: dict[str, TranscriptRow],
) -> Candidate | None:
    start_row = row_by_id.get(item.start_row_id)
    end_row = row_by_id.get(item.end_row_id)
    if start_row is None or end_row is None:
        return None
    if end_row.end_seconds <= start_row.start_seconds:
        return None

    start_index = window.rows.index(start_row)
    end_index = window.rows.index(end_row)
    if end_index < start_index:
        return None

    rows = window.rows[start_index : end_index + 1]
    if not rows:
        return None

    start_anchor = item.start_anchor.strip()
    end_anchor = item.end_anchor.strip()
    recovery_confidence = _anchor_confidence(rows, start_anchor, end_anchor)
    score = _clamp(item.overall * 0.7 + recovery_confidence * 0.3)
    candidate_id = _candidate_id(window.id, start_row.id, end_row.id)

    return Candidate(
        id=candidate_id,
        start=start_row.start_seconds,
        end=end_row.end_seconds,
        start_row=start_row,
        end_row=end_row,
        rows=rows,
        score=score,
        interestingness=item.interestingness,
        shareability=item.shareability,
        self_contained=item.self_contained,
        boundary_quality=item.boundary_quality,
        topic_relevance=item.topic_relevance,
        recovery_confidence=recovery_confidence,
        title=item.title.strip(),
        summary=item.summary.strip(),
        reason=item.reason.strip(),
        start_anchor=start_anchor,
        end_anchor=end_anchor,
        category_primary=item.category.primary,
        category_secondary=item.category.secondary,
        window_ids={window.id},
        candidate_ids={candidate_id},
    )


def _category_from_llm_item(item: Any) -> ClipCategory | None:
    if not isinstance(item, dict):
        return None
    try:
        return ClipCategory.model_validate(item)
    except ValueError:
        return None


def _title_from_rows(rows: list[TranscriptRow]) -> str:
    words = _words(" ".join(row.text for row in rows))
    if not words:
        return "Untitled Highlight"
    return " ".join(words[:10])[:80]


def _clip_excerpt(rows: list[TranscriptRow], max_chars: int = 900) -> str:
    text = " ".join(row.text.strip() for row in rows)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _anchor(text: str) -> str:
    words = _words(text)
    return " ".join(words[:16])


def _words(text: str) -> list[str]:
    return re.findall(r"[\w']+", text, flags=re.UNICODE)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", text.lower())).strip()


def _float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    if not math.isfinite(value):
        return lower
    return max(lower, min(upper, value))


def _anchor_confidence(
    rows: list[TranscriptRow],
    start_anchor: str,
    end_anchor: str,
) -> float:
    text = _norm(" ".join(row.text for row in rows))
    start_ok = bool(start_anchor.strip()) and _norm(start_anchor) in text
    end_ok = bool(end_anchor.strip()) and _norm(end_anchor) in text
    if start_ok and end_ok:
        return 1.0
    if start_ok or end_ok:
        return 0.75
    return 0.55


def _candidate_id(window_id: str, start_id: object, end_id: object) -> str:
    raw = f"{window_id}:{start_id}:{end_id}".encode()
    digest = hashlib.sha1(raw).hexdigest()[:8]
    return f"cand_{digest}"


def format_seconds(value: float) -> str:
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
