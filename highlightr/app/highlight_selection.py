from __future__ import annotations

from .highlight_candidates import Candidate, _clamp

TOPIC_VERIFICATION_MIN_CONFIDENCE = 0.7
VERIFIED_TOPIC_MATCHES = {"direct", "related"}


def _topic_filter_enabled(*, topics: list[str], topic_policy: str) -> bool:
    return bool(topics) and topic_policy in {"hard", "only"}


def _topic_match_priority(candidate: Candidate) -> int:
    if candidate.topic_match == "direct":
        return 2
    if candidate.topic_match == "related":
        return 1
    return 0


def _topic_selection_key(candidate: Candidate) -> tuple[int, float, float]:
    return (
        _topic_match_priority(candidate),
        candidate.topic_verification_confidence,
        candidate.score,
    )


def _dedupe_candidates(
    candidates: list[Candidate],
    threshold: float,
    *,
    topic_focused: bool = False,
) -> list[Candidate]:
    key = _topic_selection_key if topic_focused else lambda candidate: candidate.score
    ordered = sorted(candidates, key=key, reverse=True)
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

    return sorted(deduped, key=key, reverse=True)


def _is_topic_mismatch(
    candidate: Candidate,
    *,
    topics: list[str],
    topic_policy: str,
    admission_strictness: float,
) -> bool:
    del admission_strictness
    if not _topic_filter_enabled(topics=topics, topic_policy=topic_policy):
        return False
    return (
        candidate.topic_match not in VERIFIED_TOPIC_MATCHES
        or candidate.topic_verification_confidence
        < TOPIC_VERIFICATION_MIN_CONFIDENCE
        or not candidate.topic_evidence.strip()
    )


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
        if any(
            _overlap_seconds(candidate, kept) > allow_overlap_seconds
            for kept in selected
        ):
            removed_for_overlap += 1
            continue
        selected.append(candidate)

    return selected, removed_for_overlap


def _calibrated_final_score(
    *,
    global_score: float,
    local_score: float,
    recovery_confidence: float,
    rank: int,
    total: int,
    topic_relevance: float = 0.0,
    topic_focused: bool = False,
) -> float:
    rank_prior = (
        0.85
        if total <= 1
        else 0.68 + 0.24 * (1 - ((rank - 1) / (total - 1)))
    )
    if topic_focused:
        return _clamp(
            global_score * 0.48
            + topic_relevance * 0.30
            + local_score * 0.12
            + recovery_confidence * 0.05
            + rank_prior * 0.05
        )
    return _clamp(
        global_score * 0.72
        + local_score * 0.13
        + recovery_confidence * 0.08
        + rank_prior * 0.07
    )


def _overlap_seconds(left: Candidate, right: Candidate) -> float:
    return max(0.0, min(left.end, right.end) - max(left.start, right.start))


def _overlap_ratio(left: Candidate, right: Candidate) -> float:
    overlap = _overlap_seconds(left, right)
    shorter = max(0.001, min(left.duration, right.duration))
    return overlap / shorter
