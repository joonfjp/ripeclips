from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Any, TypeVar, cast

from pydantic import BaseModel

from .highlight_candidates import (
    Candidate,
    Window,
    _anchor,
    _anchor_confidence,
    _build_windows,
    _candidate_from_llm_item,
    _candidate_id,
    _category_from_llm_item,
    _clamp,
    _clip_excerpt,
    _float,
    _norm,
    _normalize_rows,
    _smart_discovery_window,
    _smart_stride,
    _title_from_rows,
    _words,
    format_seconds,
    plan_highlight_windows,
    resolve_window_and_stride,
)
from .highlight_contracts import (
    ClipMetadataResponse,
    DiscoveryResponse,
    GlobalRankResponse,
    TopicVerificationResponse,
)
from .highlight_metadata import (
    MAX_QUESTION_CHARS,
    MAX_QUESTION_WORDS,
    _apply_clip_metadata,
    _fallback_hashtags,
    _fallback_quotes,
    _format_description,
    _limit_words,
    _metadata_exceeds_word_limit,
    _normalize_csv,
    _question_exceeds_limits,
    _to_clip,
    _unique_strings,
)
from .highlight_prompts import (
    _build_clip_metadata_prompt,
    _build_discovery_prompt,
    _build_global_rank_prompt,
    _build_topic_verification_prompt,
    _topic_policy_rule,
)
from .highlight_selection import (
    _calibrated_final_score,
    _dedupe_candidates,
    _is_topic_mismatch,
    _overlap_ratio,
    _overlap_seconds,
    _select_non_overlapping,
    _topic_filter_enabled,
    _topic_selection_key,
)
from .llm import (
    LLMClient,
    LLMConnectionError,
    LLMError,
    LLMHTTPError,
    LLMPromptTooLargeError,
    LLMResponseError,
    LLMResult,
    get_default_llm_client,
)
from .models import (
    ClipMetadataResult,
    HighlightResult,
    IdentifyHighlightsRequest,
    ProcessingSummary,
    RejectedStats,
    Transcript,
)
from .version import __version__

logger = logging.getLogger(__name__)

DISCOVERY_SCHEMA = DiscoveryResponse.model_json_schema()
GLOBAL_RANK_SCHEMA = GlobalRankResponse.model_json_schema()
TOPIC_VERIFICATION_SCHEMA = TopicVerificationResponse.model_json_schema()
CLIP_METADATA_SCHEMA = ClipMetadataResponse.model_json_schema()

ProgressCallback = Callable[[int, int, str], None]

T = TypeVar("T", bound=BaseModel)


class _UsageTrackingClient:
    """Delegating LLM client that sums token usage across one highlight run.

    Thread-safe because discovery fans calls out over a worker pool.
    """

    def __init__(self, inner: LLMClient):
        self._inner = inner
        self._lock = threading.Lock()
        self.settings = inner.settings
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def call_structured(
        self,
        prompt: str,
        response_model: type[T],
        **kwargs: Any,
    ) -> LLMResult[T]:
        result = self._inner.call_structured(prompt, response_model, **kwargs)
        usage = result.usage
        with self._lock:
            self.prompt_tokens += usage.prompt_tokens or 0
            self.completion_tokens += usage.completion_tokens or 0
        return result


def identify_highlights(
    request: IdentifyHighlightsRequest,
    *,
    progress_callback: ProgressCallback | None = None,
    llm_client: LLMClient | None = None,
) -> HighlightResult:
    rows = _normalize_rows(request.transcript.rows)
    result_title = (request.transcript.title or "").strip() or request.transcript.source_id
    selection = request.selection
    processing = request.processing
    discovery_window_seconds, stride_seconds = resolve_window_and_stride(
        selection.max_clip_duration_seconds,
        processing.discovery_window_seconds,
        processing.stride_seconds,
    )

    if not rows:
        return _empty_result(
            request,
            result_title=result_title,
            discovery_window_seconds=discovery_window_seconds,
            stride_seconds=stride_seconds,
        )

    windows = _build_windows(
        rows,
        window_seconds=discovery_window_seconds,
        stride_seconds=stride_seconds,
    )
    usage = _UsageTrackingClient(llm_client or get_default_llm_client())
    client = cast(LLMClient, usage)
    warnings: list[str] = []
    discovered, llm_calls = _discover_all_windows(
        windows,
        request=request,
        llm_client=client,
        progress_callback=progress_callback,
        warnings=warnings,
    )

    candidate_pool: list[Candidate] = []
    removed_for_duration = 0
    removed_for_low_confidence = 0
    removed_for_low_score = 0
    removed_for_topic_mismatch = 0

    for candidates in discovered:
        valid_candidates: list[Candidate] = []
        for candidate in candidates:
            if not (
                selection.min_clip_duration_seconds
                <= candidate.duration
                <= selection.max_clip_duration_seconds
            ):
                removed_for_duration += 1
                continue
            if candidate.recovery_confidence < 0.5:
                removed_for_low_confidence += 1
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
            candidate_pool.append(candidate)
            admitted_from_window += 1

    topic_focused = _topic_filter_enabled(
        topics=selection.topics,
        topic_policy=selection.topic_policy,
    )
    if topic_focused and candidate_pool:
        if progress_callback:
            progress_callback(len(windows), len(windows), "verifying_topics")
        llm_calls += _verify_topic_candidates_llm(
            candidate_pool,
            selection.topics,
            llm_client=client,
        )

    raw_candidates: list[Candidate] = []
    for candidate in candidate_pool:
        if _is_topic_mismatch(
            candidate,
            topics=selection.topics,
            topic_policy=selection.topic_policy,
            admission_strictness=selection.admission_strictness,
        ):
            removed_for_topic_mismatch += 1
            continue
        raw_candidates.append(candidate)

    deduped = _dedupe_candidates(
        raw_candidates,
        processing.dedupe_overlap_threshold,
        topic_focused=topic_focused,
    )
    if len(deduped) > 1:
        if progress_callback:
            progress_callback(len(windows), len(windows), "global_ranking")
        original_scores = {candidate.id: candidate.score for candidate in deduped}
        try:
            deduped, calls = _global_rank_candidates_llm(
                deduped,
                selection.style or "",
                selection.topics,
                selection.topic_policy,
                llm_client=client,
            )
            llm_calls += calls
        except LLMError as error:
            llm_calls += error.attempts
            for candidate in deduped:
                candidate.score = original_scores[candidate.id]
            deduped.sort(
                key=_topic_selection_key if topic_focused else lambda candidate: candidate.score,
                reverse=True,
            )
            message = f"Global ranking unavailable; using discovery scores: {error}"
            warnings.append(message)
            logger.warning(message)

    selected, removed_for_overlap = _select_non_overlapping(
        deduped,
        max_clips=selection.max_clips,
        allow_overlap_seconds=selection.allow_overlap_seconds,
    )
    removed_for_limit = max(
        0,
        len(deduped) - len(selected) - removed_for_overlap,
    )

    if selected:
        if progress_callback:
            progress_callback(len(windows), len(windows), "enriching_metadata")
        try:
            llm_calls += _enrich_selected_clips_llm(
                selected,
                episode_title=result_title,
                language=request.transcript.language,
                llm_client=client,
            )
        except (LLMError, ValueError, TypeError) as error:
            warnings.append(f"Clip metadata enrichment unavailable: {error}")
            logger.warning("Clip metadata enrichment failed: %s", error)
            llm_calls += getattr(error, "attempts", 0)

    clips = [_to_clip(candidate, rank=i + 1) for i, candidate in enumerate(selected)]
    return HighlightResult(
        source_id=request.transcript.source_id,
        title=result_title,
        clips=clips,
        rejected=RejectedStats(
            raw_candidates=len(raw_candidates),
            deduped_candidates=len(deduped),
            removed_for_overlap=removed_for_overlap,
            removed_for_duration=removed_for_duration,
            removed_for_low_confidence=removed_for_low_confidence,
            removed_for_low_score=removed_for_low_score,
            removed_for_topic_mismatch=removed_for_topic_mismatch,
            removed_as_duplicate=max(0, len(raw_candidates) - len(deduped)),
            removed_for_limit=removed_for_limit,
        ),
        processing_summary=ProcessingSummary(
            window_count=len(windows),
            llm_calls=llm_calls,
            warnings=warnings,
            mode="llm_structured",
            version=__version__,
            discovery_window_seconds=discovery_window_seconds,
            stride_seconds=stride_seconds,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        ),
    )


def _empty_result(
    request: IdentifyHighlightsRequest,
    *,
    result_title: str,
    discovery_window_seconds: int,
    stride_seconds: int,
) -> HighlightResult:
    return HighlightResult(
        source_id=request.transcript.source_id,
        title=result_title,
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
            version=__version__,
            discovery_window_seconds=discovery_window_seconds,
            stride_seconds=stride_seconds,
        ),
    )


def _discover_all_windows(
    windows: list[Window],
    *,
    request: IdentifyHighlightsRequest,
    llm_client: LLMClient,
    progress_callback: ProgressCallback | None,
    warnings: list[str] | None = None,
) -> tuple[list[list[Candidate]], int]:
    if not windows:
        return [], 0

    failures: list[LLMError] = []

    def discover_unchecked(window: Window) -> tuple[list[Candidate], int]:
        return _discover_window_candidates_llm(
            window,
            candidates_per_window=request.processing.candidates_per_window,
            min_duration=request.selection.min_clip_duration_seconds,
            max_duration=request.selection.max_clip_duration_seconds,
            style=request.selection.style or "",
            target=request.selection.target,
            topics=request.selection.topics,
            topic_policy=request.selection.topic_policy,
            admission_strictness=request.selection.admission_strictness,
            min_candidates_per_window=request.selection.min_candidates_per_window,
            language=request.transcript.language,
            llm_client=llm_client,
        )

    def discover(window: Window) -> tuple[list[Candidate], int]:
        try:
            return discover_unchecked(window)
        except (LLMResponseError, LLMConnectionError, LLMHTTPError) as error:
            if isinstance(error, LLMHTTPError) and error.status_code != 429 and error.status_code < 500:
                raise
            failures.append(error)
            message = f"Discovery window {window.id} failed; continuing with other windows: {error}"
            logger.warning(message)
            if warnings is not None:
                warnings.append(message)
            return [], error.attempts

    if llm_client.settings.max_concurrency == 1 or len(windows) == 1:
        results: list[list[Candidate]] = []
        calls = 0
        for index, window in enumerate(windows, 1):
            candidates, attempts = discover(window)
            results.append(candidates)
            calls += attempts
            if progress_callback:
                progress_callback(index, len(windows), "identifying")
        if len(failures) == len(windows):
            raise failures[0]
        return results, calls

    ordered_results: list[tuple[list[Candidate], int] | None] = [None] * len(windows)
    max_workers = min(llm_client.settings.max_concurrency, len(windows))
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="highlight-discovery",
    ) as executor:
        future_indexes: dict[Future[tuple[list[Candidate], int]], int] = {
            executor.submit(discover, window): index
            for index, window in enumerate(windows)
        }
        completed = 0
        try:
            for future in as_completed(future_indexes):
                ordered_results[future_indexes[future]] = future.result()
                completed += 1
                if progress_callback:
                    progress_callback(completed, len(windows), "identifying")
        except Exception:
            for future in future_indexes:
                future.cancel()
            raise

    results = []
    calls = 0
    for result in ordered_results:
        if result is None:  # pragma: no cover - defensive executor boundary
            raise RuntimeError("Discovery worker did not return a result")
        candidates, attempts = result
        results.append(candidates)
        calls += attempts
    if len(failures) == len(windows):
        raise failures[0]
    return results, calls


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
    llm_client: LLMClient | None = None,
) -> tuple[list[Candidate], int]:
    if not window.rows:
        return [], 0
    client = llm_client or get_default_llm_client()
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
    result = client.call_structured(
        prompt,
        DiscoveryResponse,
        schema_name="highlight_discovery",
    )
    row_by_id = {str(row.id): row for row in window.rows}
    candidates = [
        candidate
        for item in result.output.candidates
        if (candidate := _candidate_from_llm_item(item, window, row_by_id))
        is not None
    ]
    return (
        sorted(candidates, key=lambda candidate: candidate.score, reverse=True),
        result.attempts,
    )


def _verify_topic_candidates_llm(
    candidates: list[Candidate],
    topics: list[str],
    *,
    llm_client: LLMClient,
) -> int:
    batches = _partition_candidates_for_prompt(
        candidates,
        max_items=llm_client.settings.ranking_batch_size,
        max_prompt_chars=llm_client.settings.max_prompt_chars,
        prompt_builder=lambda batch: _build_topic_verification_prompt(batch, topics),
    )
    calls = 0
    for batch in batches:
        result = llm_client.call_structured(
            _build_topic_verification_prompt(batch, topics),
            TopicVerificationResponse,
            schema_name="topic_verification",
            system_prompt=(
                "You are a conservative topic-relevance auditor. Classify a clip "
                "as direct or related only when its transcript provides clear, "
                "verbatim evidence. When uncertain, classify it as unrelated. "
                "Respond only with JSON matching the provided schema."
            ),
        )
        calls += result.attempts
        _apply_topic_verification(batch, result.output)
    return calls


def _apply_topic_verification(
    candidates: list[Candidate],
    response: TopicVerificationResponse,
) -> None:
    by_id = {candidate.id: candidate for candidate in candidates}
    seen: set[str] = set()

    for item in response.candidates:
        candidate = by_id.get(item.candidate_id)
        if candidate is None or candidate.id in seen:
            continue
        seen.add(candidate.id)

        evidence = item.evidence_quote.strip()
        evidence_is_valid = (
            item.match == "unrelated"
            or _topic_evidence_matches(candidate, evidence)
        )
        candidate.topic_verification_confidence = item.confidence
        candidate.topic_verification_reason = item.reason.strip()

        if item.match in {"direct", "related"} and evidence_is_valid:
            candidate.topic_match = item.match
            candidate.topic_evidence = evidence
            candidate.topic_relevance = _verified_topic_relevance(
                item.match,
                item.confidence,
            )
        else:
            candidate.topic_match = "unrelated"
            candidate.topic_evidence = ""
            candidate.topic_relevance = 0.0
            if item.match != "unrelated" and not evidence_is_valid:
                candidate.topic_verification_reason = (
                    "The verifier did not provide a verbatim evidence quote. "
                    f"{candidate.topic_verification_reason}"
                ).strip()

    for candidate in candidates:
        if candidate.id in seen:
            continue
        candidate.topic_match = "unverified"
        candidate.topic_relevance = 0.0
        candidate.topic_verification_reason = (
            "The topic verifier omitted this candidate."
        )


def _topic_evidence_matches(candidate: Candidate, evidence: str) -> bool:
    normalized_evidence = _norm(evidence)
    transcript = _norm(" ".join(row.text for row in candidate.rows))
    return bool(normalized_evidence) and normalized_evidence in transcript


def _verified_topic_relevance(match: str, confidence: float) -> float:
    if match == "direct":
        return _clamp(0.8 + confidence * 0.2)
    if match == "related":
        return _clamp(0.6 + confidence * 0.2)
    return 0.0


def _global_rank_candidates_llm(
    candidates: list[Candidate],
    style: str,
    topics: list[str],
    topic_policy: str,
    *,
    llm_client: LLMClient | None = None,
) -> tuple[list[Candidate], int]:
    client = llm_client or get_default_llm_client()
    ranked, calls = _rank_candidate_groups(
        candidates,
        style=style,
        topics=topics,
        topic_policy=topic_policy,
        llm_client=client,
    )
    if _topic_filter_enabled(topics=topics, topic_policy=topic_policy):
        ranked.sort(key=_topic_selection_key, reverse=True)
    return ranked, calls


def _rank_candidate_groups(
    candidates: list[Candidate],
    *,
    style: str,
    topics: list[str],
    topic_policy: str,
    llm_client: LLMClient,
) -> tuple[list[Candidate], int]:
    if len(candidates) <= 1:
        return candidates, 0
    batches = _partition_candidates_for_prompt(
        candidates,
        max_items=llm_client.settings.ranking_batch_size,
        max_prompt_chars=llm_client.settings.max_prompt_chars,
        prompt_builder=lambda batch: _build_global_rank_prompt(
            batch,
            style,
            topics,
            topic_policy,
        ),
    )
    ranked_groups: list[list[Candidate]] = []
    calls = 0
    for batch in batches:
        ranked, attempts = _rank_candidate_batch(
            batch,
            style=style,
            topics=topics,
            topic_policy=topic_policy,
            llm_client=llm_client,
        )
        ranked_groups.append(ranked)
        calls += attempts

    if len(ranked_groups) == 1:
        return ranked_groups[0], calls

    leaders = [group[0] for group in ranked_groups if group]
    ordered_leaders, leader_calls = _rank_candidate_groups(
        leaders,
        style=style,
        topics=topics,
        topic_policy=topic_policy,
        llm_client=llm_client,
    )
    calls += leader_calls
    group_by_leader = {group[0].id: group for group in ranked_groups if group}
    ordered: list[Candidate] = []
    for leader in ordered_leaders:
        ordered.extend(group_by_leader[leader.id])
    return ordered, calls


def _rank_candidate_batch(
    candidates: list[Candidate],
    *,
    style: str,
    topics: list[str],
    topic_policy: str,
    llm_client: LLMClient,
) -> tuple[list[Candidate], int]:
    if len(candidates) <= 1:
        return candidates, 0
    result = llm_client.call_structured(
        _build_global_rank_prompt(candidates, style, topics, topic_policy),
        GlobalRankResponse,
        schema_name="global_candidate_ranking",
    )
    by_id = {candidate.id: candidate for candidate in candidates}
    ranked: list[Candidate] = []
    seen: set[str] = set()
    for item in result.output.ranked_candidates:
        candidate = by_id.get(item.candidate_id)
        if candidate is None or candidate.id in seen:
            continue
        candidate.score = _calibrated_final_score(
            global_score=item.global_score,
            local_score=candidate.score,
            recovery_confidence=candidate.recovery_confidence,
            rank=len(ranked) + 1,
            total=len(candidates),
            topic_relevance=candidate.topic_relevance,
            topic_focused=_topic_filter_enabled(
                topics=topics,
                topic_policy=topic_policy,
            ),
        )
        rank_reason = item.rank_reason.strip()
        if rank_reason:
            candidate.reason = (
                f"{candidate.reason} Global ranking: {rank_reason}".strip()
            )
        ranked.append(candidate)
        seen.add(candidate.id)

    ranked.extend(candidate for candidate in candidates if candidate.id not in seen)
    return ranked, result.attempts


def _enrich_selected_clips_llm(
    candidates: list[Candidate],
    *,
    episode_title: str,
    language: str | None,
    llm_client: LLMClient | None = None,
) -> int:
    client = llm_client or get_default_llm_client()
    batches = _partition_candidates_for_prompt(
        candidates,
        max_items=client.settings.metadata_batch_size,
        max_prompt_chars=client.settings.max_prompt_chars,
        prompt_builder=lambda batch: _build_clip_metadata_prompt(
            batch,
            episode_title,
            language,
        ),
    )
    calls = 0
    pending = deque(batches)
    while pending:
        batch = pending.popleft()
        try:
            calls += _enrich_metadata_batch(
                batch,
                episode_title=episode_title,
                language=language,
                llm_client=client,
            )
        except LLMError as error:
            calls += getattr(error, "attempts", 0)
            logger.warning(
                "Clip metadata batch failed candidate_ids=%s: %s",
                [candidate.id for candidate in batch],
                error,
            )
            # Smaller outputs can recover from truncation or invalid structured
            # responses. Each batch splits at most down to single candidates;
            # transport/provider failures already have client-level retries.
            if isinstance(error, LLMResponseError) and len(batch) > 1:
                midpoint = len(batch) // 2
                logger.info("Retrying clip metadata in smaller batches")
                pending.appendleft(batch[midpoint:])
                pending.appendleft(batch[:midpoint])
    return calls


def _enrich_metadata_batch(
    candidates: list[Candidate],
    *,
    episode_title: str,
    language: str | None,
    llm_client: LLMClient,
) -> int:
    result = llm_client.call_structured(
        _build_clip_metadata_prompt(candidates, episode_title, language),
        ClipMetadataResponse,
        schema_name="clip_metadata",
    )
    calls = result.attempts
    by_id = {candidate.id: candidate for candidate in candidates}
    retry_candidates: list[Candidate] = []
    for item in result.output.clips:
        candidate = by_id.get(item.candidate_id)
        if candidate is None:
            continue
        payload = item.model_dump()
        _apply_clip_metadata(candidate, payload)
        if _metadata_exceeds_word_limit(payload):
            retry_candidates.append(candidate)

    if not retry_candidates:
        return calls

    try:
        retry_result = llm_client.call_structured(
            _build_clip_metadata_prompt(
                retry_candidates,
                episode_title,
                language,
                word_limit_retry=True,
            ),
            ClipMetadataResponse,
            schema_name="clip_metadata_word_limit_retry",
        )
        calls += retry_result.attempts
        retry_by_id = {candidate.id: candidate for candidate in retry_candidates}
        for item in retry_result.output.clips:
            payload = item.model_dump()
            if _metadata_exceeds_word_limit(payload):
                continue
            candidate = retry_by_id.get(item.candidate_id)
            if candidate is not None:
                _apply_clip_metadata(candidate, payload)
    except LLMError as error:
        calls += getattr(error, "attempts", 0)
        logger.warning("Clip metadata word-limit retry failed: %s", error)
    return calls


def generate_clip_metadata(
    transcript: Transcript,
    *,
    llm_client: LLMClient | None = None,
) -> ClipMetadataResult:
    """Write publishing metadata for a transcript that is already one clip.

    Skips discovery and ranking: the whole transcript becomes a single
    candidate and goes through the same metadata prompt, word-limit retry,
    and fallbacks as clips selected by ``identify_highlights``.
    """
    rows = _normalize_rows(transcript.rows)
    if not rows:
        raise ValueError("Transcript has no caption rows")
    supplied_title = (transcript.title or "").strip()
    title = supplied_title or transcript.source_id
    candidate = Candidate(
        id="clip_001",
        start=rows[0].start_seconds,
        end=max(row.end_seconds for row in rows),
        start_row=rows[0],
        end_row=rows[-1],
        rows=rows,
        score=0.0,
        interestingness=0.0,
        shareability=0.0,
        self_contained=0.0,
        boundary_quality=0.0,
        topic_relevance=0.0,
        recovery_confidence=0.0,
        title=supplied_title,
    )
    client = llm_client or get_default_llm_client()
    prompt_budget = max(1, client.settings.max_prompt_chars - 512)
    if len(_build_clip_metadata_prompt([candidate], title, transcript.language)) > prompt_budget:
        raise LLMPromptTooLargeError(
            "Transcript cannot fit within the configured "
            f"{client.settings.max_prompt_chars:,}-character prompt limit"
        )
    llm_calls = _enrich_metadata_batch(
        [candidate],
        episode_title=title,
        language=transcript.language,
        llm_client=client,
    )
    return ClipMetadataResult(
        source_id=transcript.source_id,
        title=title,
        clip=_to_clip(candidate, rank=1),
        llm_calls=llm_calls,
    )


def _partition_candidates_for_prompt(
    candidates: list[Candidate],
    *,
    max_items: int,
    max_prompt_chars: int,
    prompt_builder: Callable[[list[Candidate]], str],
) -> list[list[Candidate]]:
    if not candidates:
        return []
    batches: list[list[Candidate]] = []
    current: list[Candidate] = []
    prompt_budget = max(1, max_prompt_chars - 512)

    for candidate in candidates:
        trial = [*current, candidate]
        too_many = len(trial) > max_items
        too_large = len(prompt_builder(trial)) > prompt_budget
        if current and (too_many or too_large):
            batches.append(current)
            current = [candidate]
        else:
            current = trial

        if len(prompt_builder(current)) > prompt_budget:
            raise LLMPromptTooLargeError(
                f"Candidate {candidate.id} cannot fit within the configured "
                f"{max_prompt_chars:,}-character prompt limit"
            )

    if current:
        batches.append(current)
    return batches


__all__ = [
    "Candidate",
    "CLIP_METADATA_SCHEMA",
    "DISCOVERY_SCHEMA",
    "GLOBAL_RANK_SCHEMA",
    "TOPIC_VERIFICATION_SCHEMA",
    "MAX_QUESTION_CHARS",
    "MAX_QUESTION_WORDS",
    "Window",
    "_anchor",
    "_anchor_confidence",
    "_apply_clip_metadata",
    "_apply_topic_verification",
    "_build_clip_metadata_prompt",
    "_build_discovery_prompt",
    "_build_global_rank_prompt",
    "_build_topic_verification_prompt",
    "_build_windows",
    "_calibrated_final_score",
    "_candidate_from_llm_item",
    "_candidate_id",
    "_category_from_llm_item",
    "_clamp",
    "_clip_excerpt",
    "_dedupe_candidates",
    "_fallback_hashtags",
    "_fallback_quotes",
    "_float",
    "_format_description",
    "_global_rank_candidates_llm",
    "_is_topic_mismatch",
    "_limit_words",
    "_metadata_exceeds_word_limit",
    "_norm",
    "_normalize_csv",
    "_normalize_rows",
    "_overlap_ratio",
    "_overlap_seconds",
    "_question_exceeds_limits",
    "_select_non_overlapping",
    "_smart_discovery_window",
    "_smart_stride",
    "_title_from_rows",
    "_to_clip",
    "_topic_evidence_matches",
    "_topic_policy_rule",
    "_unique_strings",
    "_words",
    "format_seconds",
    "generate_clip_metadata",
    "identify_highlights",
    "plan_highlight_windows",
    "resolve_window_and_stride",
    "_verified_topic_relevance",
    "_verify_topic_candidates_llm",
]
