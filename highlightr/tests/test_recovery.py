import json

import pytest
from pydantic import ValidationError

from app import highlights, llm
from app.config import LLMSettings
from app.highlight_contracts import DiscoveryResponse
from app.models import IdentifyHighlightsRequest, Transcript
from tests.fakes import FakeLLMClient
from tests.test_highlights import make_candidate
from tests.test_llm import Answer, ScoredAnswer, _Response, _completion, _settings


def candidate(**overrides):
    return {
        "title": "Valid", "summary": "Summary", "reason": "Reason",
        "category": {"primary": "insight", "secondary": None},
        "start_row_id": "0", "end_row_id": "0", "start_anchor": "words",
        "end_anchor": "words", "interestingness": 0.9, "shareability": 0.9,
        "self_contained": 0.9, "boundary_quality": 0.9,
        "topic_relevance": 0.9, "overall": 0.9, **overrides,
    }


def test_exact_run_352_extra_field_is_removed_without_retry():
    requests = []
    def request(*args, **kwargs):
        requests.append(args)
        return _Response(_completion(json.dumps({"candidates": [candidate(title_extra=None)]})))
    result = llm.LLMClient(_settings(), urlopen=request).call_structured("prompt", DiscoveryResponse)
    assert len(result.output.candidates) == 1
    assert result.output.candidates[0].title == "Valid"
    assert result.attempts == len(requests) == 1


def test_invalid_candidate_does_not_discard_valid_sibling():
    result = DiscoveryResponse.model_validate({"candidates": [candidate(), candidate(overall=9)]}, strict=True)
    assert len(result.candidates) == 1
    with pytest.raises(ValidationError):
        DiscoveryResponse.model_validate({"candidates": [candidate(overall=9)]}, strict=True)


def test_malformed_response_retries_once_then_recovers():
    responses = iter([_Response("broken json"), _Response(_completion('{"answer":"ok"}'))])
    client = llm.LLMClient(_settings(), urlopen=lambda *a, **k: next(responses))
    result = client.call_structured("prompt", Answer)
    assert result.output.answer == "ok"
    assert result.attempts == 2


def test_unknown_keys_do_not_bypass_score_validation():
    client = llm.LLMClient(_settings(), urlopen=lambda *a, **k: _Response(_completion('{"score":9,"extra":null}')))
    with pytest.raises(llm.LLMResponseError) as error:
        client.call_structured("prompt", ScoredAnswer)
    assert error.value.attempts == 2


@pytest.mark.parametrize("concurrency", [1, 2])
@pytest.mark.parametrize("all_fail", [False, True])
def test_window_failures_preserve_successes_but_all_failed_is_an_error(monkeypatch, concurrency, all_fail):
    windows = [highlights.Window(id=str(i), start=i*60, end=(i+1)*60, rows=[]) for i in range(2)]
    kept = make_candidate("keep", 0, 30)
    def discover(window, **kwargs):
        if window.id == "0" or all_fail:
            raise llm.LLMResponseError("bad output", attempts=2)
        return [kept], 1
    monkeypatch.setattr(highlights, "_discover_window_candidates_llm", discover)
    request = IdentifyHighlightsRequest(transcript=Transcript(source_id="test", rows=[]))
    client = FakeLLMClient(settings=LLMSettings(max_concurrency=concurrency))
    warnings = []
    if all_fail:
        with pytest.raises(llm.LLMResponseError):
            highlights._discover_all_windows(windows, request=request, llm_client=client, progress_callback=None, warnings=warnings)
    else:
        results, calls = highlights._discover_all_windows(windows, request=request, llm_client=client, progress_callback=None, warnings=warnings)
        assert results == [[], [kept]]
        assert calls == 3
        assert len(warnings) == 1


def test_ranking_outage_preserves_clips_and_warns(monkeypatch):
    candidates = [make_candidate("first", 0, 30, score=0.9), make_candidate("second", 60, 90, score=0.85)]
    monkeypatch.setattr(highlights, "_discover_all_windows", lambda *a, **k: ([candidates], 1))
    def rank(*a, **k):
        candidates[0].score = 0.01  # A partial ranking must not corrupt fallback ordering.
        raise llm.LLMConnectionError("offline", attempts=2)
    monkeypatch.setattr(highlights, "_global_rank_candidates_llm", rank)
    monkeypatch.setattr(highlights, "_enrich_selected_clips_llm", lambda *a, **k: 0)
    request = IdentifyHighlightsRequest.model_validate({"transcript": {"source_id": "test", "rows": [{"id": 0, "start_seconds": 0, "end_seconds": 90, "text": "words"}]}, "selection": {"min_clip_duration_seconds": 10, "max_clip_duration_seconds": 60}})
    result = highlights.identify_highlights(request, llm_client=FakeLLMClient())
    assert len(result.clips) == 2
    assert candidates[0].score == 0.9
    assert result.processing_summary.llm_calls == 3
    assert "Global ranking unavailable" in result.processing_summary.warnings[0]
