from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any

from .llm import call_llm_structured, extract_structured_output
from .models import (
    Anchors,
    ClipSource,
    HighlightClip,
    HighlightResult,
    IdentifyHighlightsRequest,
    ProcessingSummary,
    RejectedStats,
    ScoreBreakdown,
    TranscriptRow,
)


DISCOVERY_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "reason": {"type": "string"},
                    "start_row_id": {"type": "string"},
                    "end_row_id": {"type": "string"},
                    "start_anchor": {"type": "string"},
                    "end_anchor": {"type": "string"},
                    "interestingness": {"type": "number"},
                    "self_contained": {"type": "number"},
                    "boundary_quality": {"type": "number"},
                    "topic_relevance": {"type": "number"},
                    "overall": {"type": "number"},
                },
                "required": [
                    "title",
                    "summary",
                    "reason",
                    "start_row_id",
                    "end_row_id",
                    "start_anchor",
                    "end_anchor",
                    "interestingness",
                    "self_contained",
                    "boundary_quality",
                    "topic_relevance",
                    "overall",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

GLOBAL_RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "global_score": {"type": "number"},
                    "rank_reason": {"type": "string"},
                },
                "required": ["candidate_id", "global_score", "rank_reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranked_candidates"],
    "additionalProperties": False,
}


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
    self_contained: float
    boundary_quality: float
    topic_relevance: float
    recovery_confidence: float
    title: str = ""
    summary: str = ""
    reason: str = ""
    start_anchor: str = ""
    end_anchor: str = ""
    window_ids: set[str] = field(default_factory=set)
    candidate_ids: set[str] = field(default_factory=set)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


ProgressCallback = Callable[[int, int, str], None]


def identify_highlights(
    request: IdentifyHighlightsRequest,
    *,
    progress_callback: ProgressCallback | None = None,
) -> HighlightResult:
    rows = _normalize_rows(request.transcript.rows)
    selection = request.selection
    processing = request.processing
    discovery_window_seconds, stride_seconds = resolve_window_and_stride(
        selection.max_clip_duration_seconds,
        processing.discovery_window_seconds,
        processing.stride_seconds,
    )

    if not rows:
        return HighlightResult(
            source_id=request.transcript.source_id,
            clips=[],
            rejected=RejectedStats(
                raw_candidates=0,
                deduped_candidates=0,
                removed_for_overlap=0,
                removed_for_duration=0,
                removed_for_low_confidence=0,
                removed_for_low_score=0,
                removed_for_topic_mismatch=0,
            ),
            processing_summary=ProcessingSummary(
                window_count=0,
                llm_calls=0,
                mode="llm_structured",
                version="ripe-clips",
                discovery_window_seconds=discovery_window_seconds,
                stride_seconds=stride_seconds,
            ),
        )

    windows = _build_windows(
        rows,
        window_seconds=discovery_window_seconds,
        stride_seconds=stride_seconds,
    )
    raw_candidates: list[Candidate] = []
    removed_for_duration = 0
    removed_for_low_confidence = 0
    removed_for_low_score = 0
    removed_for_topic_mismatch = 0
    llm_calls = 0

    for index, window in enumerate(windows, 1):
        candidates, calls = _discover_window_candidates_llm(
            window,
            candidates_per_window=processing.candidates_per_window,
            min_duration=selection.min_clip_duration_seconds,
            max_duration=selection.max_clip_duration_seconds,
            style=selection.style or "",
            target=selection.target,
            topics=selection.topics,
            topic_policy=selection.topic_policy,
            admission_strictness=selection.admission_strictness,
            min_candidates_per_window=selection.min_candidates_per_window,
            language=request.transcript.language,
        )
        llm_calls += calls
        valid_candidates: list[Candidate] = []
        for candidate in candidates:
            if candidate.duration < selection.min_clip_duration_seconds:
                removed_for_duration += 1
                continue
            if candidate.duration > selection.max_clip_duration_seconds:
                removed_for_duration += 1
                continue
            if candidate.recovery_confidence < 0.5:
                removed_for_low_confidence += 1
                continue
            if _is_topic_mismatch(
                candidate,
                topics=selection.topics,
                topic_policy=selection.topic_policy,
                admission_strictness=selection.admission_strictness,
            ):
                removed_for_topic_mismatch += 1
                continue
            valid_candidates.append(candidate)

        admitted_from_window = 0
        for candidate in valid_candidates:
            if (
                candidate.score < selection.admission_strictness
                and admitted_from_window >= selection.min_candidates_per_window
            ):
                removed_for_low_score += 1
                continue
            raw_candidates.append(candidate)
            admitted_from_window += 1

        if progress_callback:
            progress_callback(index, len(windows), "identifying")

    deduped = _dedupe_candidates(raw_candidates, processing.dedupe_overlap_threshold)
    if len(deduped) > 1:
        if progress_callback:
            progress_callback(len(windows), len(windows), "global_ranking")
        deduped, calls = _global_rank_candidates_llm(
            deduped,
            selection.style or "",
            selection.topics,
            selection.topic_policy,
        )
        llm_calls += calls

    selected, removed_for_overlap = _select_non_overlapping(
        deduped,
        max_clips=selection.max_clips,
        allow_overlap_seconds=selection.allow_overlap_seconds,
    )

    clips = [_to_clip(candidate, rank=i + 1) for i, candidate in enumerate(selected)]
    return HighlightResult(
        source_id=request.transcript.source_id,
        clips=clips,
        rejected=RejectedStats(
            raw_candidates=len(raw_candidates),
            deduped_candidates=len(deduped),
            removed_for_overlap=removed_for_overlap,
            removed_for_duration=removed_for_duration,
            removed_for_low_confidence=removed_for_low_confidence,
            removed_for_low_score=removed_for_low_score,
            removed_for_topic_mismatch=removed_for_topic_mismatch,
        ),
        processing_summary=ProcessingSummary(
            window_count=len(windows),
            llm_calls=llm_calls,
            mode="llm_structured",
            version="ripe-clips",
            discovery_window_seconds=discovery_window_seconds,
            stride_seconds=stride_seconds,
        ),
    )


def resolve_window_and_stride(
    max_clip_duration_seconds: int,
    discovery_window_seconds: int | None,
    stride_seconds: int | None,
) -> tuple[int, int]:
    window = discovery_window_seconds or _smart_discovery_window(max_clip_duration_seconds)
    stride = stride_seconds or _smart_stride(window)
    return window, min(stride, window)


def _smart_discovery_window(max_clip_duration_seconds: int) -> int:
    # Scale the window to the requested clip length so short clips don't pay for
    # oversized prompts. Floor (10 min) keeps enough narrative context for the
    # LLM to judge standalone clips; ceiling (30 min) caps prompt size/cost.
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

    while start < transcript_end:
        end = min(start + window_seconds, transcript_end)
        window_rows = [
            row for row in rows if row.start_seconds < end and row.end_seconds > start
        ]
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


def _discover_window_candidates_llm(
    window: Window,
    *,
    candidates_per_window: int,
    min_duration: int,
    max_duration: int,
    style: str,
    target: str,
    topics: list[str],
    topic_policy: str,
    admission_strictness: float,
    min_candidates_per_window: int,
    language: str | None,
) -> tuple[list[Candidate], int]:
    if not window.rows:
        return [], 0

    prompt = _build_discovery_prompt(
        window,
        candidates_per_window=candidates_per_window,
        min_duration=min_duration,
        max_duration=max_duration,
        style=style,
        target=target,
        topics=topics,
        topic_policy=topic_policy,
        admission_strictness=admission_strictness,
        min_candidates_per_window=min_candidates_per_window,
        language=language,
    )
    response = call_llm_structured(prompt, DISCOVERY_SCHEMA)
    data = extract_structured_output(response)
    row_by_id = {str(row.id): row for row in window.rows}
    candidates: list[Candidate] = []

    if isinstance(data, dict):
        for item in data.get("candidates", []):
            candidate = _candidate_from_llm_item(item, window, row_by_id)
            if candidate is not None:
                candidates.append(candidate)

    return sorted(candidates, key=lambda candidate: candidate.score, reverse=True), 1


def _candidate_from_llm_item(
    item: Any,
    window: Window,
    row_by_id: dict[str, TranscriptRow],
) -> Candidate | None:
    if not isinstance(item, dict):
        return None

    start_row = row_by_id.get(str(item.get("start_row_id", "")))
    end_row = row_by_id.get(str(item.get("end_row_id", "")))
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

    interestingness = _clamp(_float(item.get("interestingness")))
    self_contained = _clamp(_float(item.get("self_contained")))
    boundary_quality = _clamp(_float(item.get("boundary_quality")))
    topic_relevance = _clamp(_float(item.get("topic_relevance")))
    overall = _clamp(_float(item.get("overall")))
    start_anchor = str(item.get("start_anchor", "")).strip()
    end_anchor = str(item.get("end_anchor", "")).strip()
    recovery_confidence = _anchor_confidence(rows, start_anchor, end_anchor)
    score = _clamp(overall * 0.7 + recovery_confidence * 0.3)
    candidate_id = _candidate_id(window.id, start_row.id, end_row.id)

    return Candidate(
        id=candidate_id,
        start=start_row.start_seconds,
        end=end_row.end_seconds,
        start_row=start_row,
        end_row=end_row,
        rows=rows,
        score=score,
        interestingness=interestingness,
        self_contained=self_contained,
        boundary_quality=boundary_quality,
        topic_relevance=topic_relevance,
        recovery_confidence=recovery_confidence,
        title=str(item.get("title", "")).strip(),
        summary=str(item.get("summary", "")).strip(),
        reason=str(item.get("reason", "")).strip(),
        start_anchor=start_anchor,
        end_anchor=end_anchor,
        window_ids={window.id},
        candidate_ids={candidate_id},
    )


def _global_rank_candidates_llm(
    candidates: list[Candidate],
    style: str,
    topics: list[str],
    topic_policy: str,
) -> tuple[list[Candidate], int]:
    by_id = {candidate.id: candidate for candidate in candidates}
    response = call_llm_structured(
        _build_global_rank_prompt(candidates, style, topics, topic_policy),
        GLOBAL_RANK_SCHEMA,
    )
    data = extract_structured_output(response)
    ranked_items = data.get("ranked_candidates", []) if isinstance(data, dict) else []

    ranked: list[Candidate] = []
    seen: set[str] = set()
    for item in ranked_items:
        if not isinstance(item, dict):
            continue
        candidate_id = str(item.get("candidate_id", ""))
        candidate = by_id.get(str(candidate_id))
        if candidate is None or candidate.id in seen:
            continue
        global_score = _clamp(_float(item.get("global_score")))
        rank_reason = str(item.get("rank_reason", "")).strip()
        candidate.score = _calibrated_final_score(
            global_score=global_score,
            local_score=candidate.score,
            recovery_confidence=candidate.recovery_confidence,
            rank=len(ranked) + 1,
            total=len(candidates),
        )
        if rank_reason:
            candidate.reason = f"{candidate.reason} Global ranking: {rank_reason}".strip()
        ranked.append(candidate)
        seen.add(candidate.id)

    for candidate in candidates:
        if candidate.id not in seen:
            ranked.append(candidate)

    return ranked, 1


def _build_discovery_prompt(
    window: Window,
    *,
    candidates_per_window: int,
    min_duration: int,
    max_duration: int,
    style: str,
    target: str,
    topics: list[str],
    topic_policy: str,
    admission_strictness: float,
    min_candidates_per_window: int,
    language: str | None,
) -> str:
    transcript = "\n".join(
        f"[{row.id}] {format_seconds(row.start_seconds)} - "
        f"{format_seconds(row.end_seconds)}"
        f"{f' {row.speaker}:' if row.speaker else ''} {row.text}"
        for row in window.rows
    )
    topic_text = ", ".join(topics) if topics else "none"
    topic_rule = _topic_policy_rule(topics, topic_policy)
    coverage_rule = (
        f"- Try to return at least {min_candidates_per_window} usable candidate(s) from this window. "
        "If none meet the admission score, return the best valid fallback(s) and score them honestly."
        if min_candidates_per_window > 0
        else "- If this window has no candidate that satisfies the topic policy and admission score, return an empty candidates array."
    )

    return f"""You are selecting highlight clips from a podcast transcript.

Return up to {candidates_per_window} strong candidate clips from this window.
You may return fewer than {candidates_per_window}, including zero.

Selection target: {target}
Preferred style: {style or "interesting, self-contained, compelling"}
Focus topics: {topic_text}
Topic policy: {topic_policy}
Minimum admission score: {admission_strictness:.2f}
Transcript language: {language or "unknown"}

Rules:
- Candidate duration must be between {min_duration} and {max_duration} seconds.
- Choose self-contained spans with clear setup and payoff.
- Prefer surprising, emotionally engaging, insightful, funny, useful, or high-stakes moments.
{topic_rule}
- topic_relevance is 0.00 for unrelated clips and 1.00 for clips directly centered on the focus topics.
- Except for fallback coverage, only return candidates whose overall score is at least {admission_strictness:.2f}.
{coverage_rule}
- Use only row IDs that appear in this transcript window.
- start_row_id and end_row_id are inclusive row IDs.
- Do not invent timestamps or text.
- Anchors must be short verbatim quotes from near the selected start/end rows.
- Scores must be numbers from 0 to 1 and calibrated strictly:
  - 0.95-1.00: rare, exceptional even compared with a full episode
  - 0.90-0.94: excellent and highly shareable
  - 0.80-0.89: strong candidate
  - 0.70-0.79: usable but imperfect
  - below 0.70: weak or context-dependent
- Do not give every good clip a score above 0.90. Reserve high scores.
- Return JSON matching the provided schema exactly.

Transcript window {window.id}, {format_seconds(window.start)}-{format_seconds(window.end)}:
{transcript}
"""


def _build_global_rank_prompt(
    candidates: list[Candidate],
    style: str,
    topics: list[str],
    topic_policy: str,
) -> str:
    blocks = []
    for candidate in candidates:
        excerpt = _clip_excerpt(candidate.rows)
        blocks.append(
            f"candidate_id: {candidate.id}\n"
            f"time: {format_seconds(candidate.start)}-{format_seconds(candidate.end)}\n"
            f"duration_seconds: {candidate.duration:.1f}\n"
            f"title: {candidate.title}\n"
            f"summary: {candidate.summary}\n"
            f"reason: {candidate.reason}\n"
            f"local_score: {candidate.score:.3f}\n"
            f"topic_relevance: {candidate.topic_relevance:.3f}\n"
            f"excerpt: {excerpt}"
        )

    topic_text = ", ".join(topics) if topics else "none"

    return f"""Rank these recovered podcast highlight candidates from best to worst.

Preferred style: {style or "interesting, self-contained, compelling"}
Focus topics: {topic_text}
Topic policy: {topic_policy}

Rules:
- Include every candidate_id exactly once.
- Prefer clips with strong standalone value, clear setup/payoff, and memorable content.
- Apply the topic policy when ordering candidates.
- Calibrate global_score across the whole candidate set, not just within one topic.
- Use the full 0.00-1.00 range honestly:
  - 0.95-1.00: rare, episode-defining, likely top 1-2 clips only
  - 0.90-0.94: excellent, clearly worth clipping
  - 0.80-0.89: strong but not elite
  - 0.70-0.79: usable but has flaws
  - below 0.70: weak, context-dependent, or redundant
- Avoid tied scores unless two candidates are genuinely indistinguishable.
- Return JSON matching the provided schema exactly.

Candidates:
{"\n\n---\n\n".join(blocks)}
"""


def _dedupe_candidates(candidates: list[Candidate], threshold: float) -> list[Candidate]:
    ordered = sorted(candidates, key=lambda candidate: candidate.score, reverse=True)
    deduped: list[Candidate] = []

    for candidate in ordered:
        duplicate = next(
            (
                existing
                for existing in deduped
                if _overlap_ratio(candidate, existing) >= threshold
            ),
            None,
        )
        if duplicate is None:
            deduped.append(candidate)
            continue

        duplicate.window_ids.update(candidate.window_ids)
        duplicate.candidate_ids.update(candidate.candidate_ids)

    return sorted(deduped, key=lambda candidate: candidate.score, reverse=True)


def _topic_policy_rule(topics: list[str], topic_policy: str) -> str:
    if not topics:
        return "- No focus topics were provided; judge candidates by standalone highlight quality."
    if topic_policy == "only":
        return (
            "- Candidates must be directly centered on one or more focus topics. "
            "If no such candidate exists, return an empty candidates array."
        )
    if topic_policy == "hard":
        return (
            "- Candidates should substantially address one or more focus topics. "
            "Reject loosely related or merely adjacent moments."
        )
    return (
        "- Prefer candidates about one or more focus topics, but allow exceptional off-topic "
        "clips when they are clearly stronger."
    )


def _is_topic_mismatch(
    candidate: Candidate,
    *,
    topics: list[str],
    topic_policy: str,
    admission_strictness: float,
) -> bool:
    if not topics or topic_policy == "soft":
        return False
    if topic_policy == "only":
        threshold = max(0.85, admission_strictness)
    else:
        threshold = max(0.6, admission_strictness * 0.75)
    return candidate.topic_relevance < threshold


def _select_non_overlapping(
    candidates: list[Candidate],
    *,
    max_clips: int | None,
    allow_overlap_seconds: int,
) -> tuple[list[Candidate], int]:
    selected: list[Candidate] = []
    removed_for_overlap = 0

    for candidate in candidates:
        if max_clips is not None and len(selected) >= max_clips:
            break
        if any(_overlap_seconds(candidate, kept) > allow_overlap_seconds for kept in selected):
            removed_for_overlap += 1
            continue
        selected.append(candidate)

    return selected, removed_for_overlap


def _to_clip(candidate: Candidate, *, rank: int) -> HighlightClip:
    return HighlightClip(
        clip_id=f"clip_{rank:03d}",
        rank=rank,
        start_seconds=round(candidate.start, 3),
        end_seconds=round(candidate.end, 3),
        duration_seconds=round(candidate.duration, 3),
        start_row_id=candidate.start_row.id,
        end_row_id=candidate.end_row.id,
        title=candidate.title or _title_from_rows(candidate.rows),
        summary=candidate.summary
        or f"Candidate span from transcript rows {candidate.start_row.id} through {candidate.end_row.id}.",
        reason=candidate.reason
        or "Selected by structured LLM discovery, recovery validation, and non-overlap constraints.",
        scores=ScoreBreakdown(
            overall=round(candidate.score, 4),
            interestingness=round(candidate.interestingness, 4),
            self_contained=round(candidate.self_contained, 4),
            boundary_quality=round(candidate.boundary_quality, 4),
            topic_relevance=round(candidate.topic_relevance, 4),
            recovery_confidence=round(candidate.recovery_confidence, 4),
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


def _anchor_confidence(rows: list[TranscriptRow], start_anchor: str, end_anchor: str) -> float:
    text = _norm(" ".join(row.text for row in rows))
    start_ok = bool(start_anchor.strip()) and _norm(start_anchor) in text
    end_ok = bool(end_anchor.strip()) and _norm(end_anchor) in text
    if start_ok and end_ok:
        return 1.0
    if start_ok or end_ok:
        return 0.75
    return 0.55


def _calibrated_final_score(
    *,
    global_score: float,
    local_score: float,
    recovery_confidence: float,
    rank: int,
    total: int,
) -> float:
    if total <= 1:
        rank_prior = 0.85
    else:
        rank_prior = 0.68 + 0.24 * (1 - ((rank - 1) / (total - 1)))
    return _clamp(
        global_score * 0.72
        + local_score * 0.13
        + recovery_confidence * 0.08
        + rank_prior * 0.07
    )


def _candidate_id(window_id: str, start_id: object, end_id: object) -> str:
    raw = f"{window_id}:{start_id}:{end_id}".encode("utf-8")
    digest = hashlib.sha1(raw).hexdigest()[:8]
    return f"cand_{digest}"


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
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _overlap_seconds(left: Candidate, right: Candidate) -> float:
    return max(0.0, min(left.end, right.end) - max(left.start, right.start))


def _overlap_ratio(left: Candidate, right: Candidate) -> float:
    overlap = _overlap_seconds(left, right)
    shorter = max(0.001, min(left.duration, right.duration))
    return overlap / shorter


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def format_seconds(value: float) -> str:
    total = int(value)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
