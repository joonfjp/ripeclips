from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app import main
from app.config import AppSettings, LLMSettings
from app.highlight_contracts import (
    ClipMetadataResponse,
    DiscoveryResponse,
    TopicVerificationResponse,
)
from app.llm import LLMConnectionError
from app.main import create_app
from tests.fakes import FakeLLMClient


def _settings(**overrides) -> AppSettings:
    values = {
        "llm": LLMSettings(max_retries=0),
        "job_ttl_seconds": 60,
        "max_jobs": 10,
        "max_srt_upload_bytes": 1024,
    }
    values.update(overrides)
    return AppSettings.model_validate(values)


def _empty_request() -> dict:
    return {
        "transcript": {
            "source_id": "episode-empty",
            "title": "Empty Episode",
            "rows": [],
        }
    }


def test_health_and_readiness_do_not_expose_api_key():
    settings = _settings(
        llm=LLMSettings(
            base_url="https://provider.example/v1",
            model="model-name",
            api_key="secret",
            max_retries=0,
        )
    )
    with TestClient(create_app(settings=settings)) as client:
        health = client.get("/health")
        ready = client.get("/ready")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "configured": True,
        "llm_base_url": "https://provider.example/v1",
        "llm_model": "model-name",
    }
    assert "secret" not in ready.text


def test_identify_empty_transcript_does_not_call_llm():
    fake = FakeLLMClient([])
    with TestClient(create_app(settings=_settings(), llm_client=fake)) as client:
        response = client.post("/v2/highlights/identify", json=_empty_request())

    assert response.status_code == 200
    assert response.json()["clips"] == []
    assert fake.calls == []


def test_llm_errors_are_mapped_to_bad_gateway():
    fake = FakeLLMClient(
        [LLMConnectionError("provider unavailable", attempts=3)]
    )
    payload = {
        "transcript": {
            "source_id": "episode",
            "rows": [
                {
                    "id": 0,
                    "start_seconds": 0,
                    "end_seconds": 60,
                    "text": "A complete minute of transcript content.",
                }
            ],
        },
        "selection": {
            "min_clip_duration_seconds": 10,
            "max_clip_duration_seconds": 120,
        },
    }
    with TestClient(create_app(settings=_settings(), llm_client=fake)) as client:
        response = client.post("/v2/highlights/identify", json=payload)

    assert response.status_code == 502
    assert response.json()["detail"] == "provider unavailable"


def test_srt_upload_limit_and_empty_caption_validation():
    with TestClient(create_app(settings=_settings())) as client:
        oversized = client.post(
            "/v2/highlights/identify-srt",
            files={"file": ("large.srt", b"x" * 1025, "application/x-subrip")},
        )
        empty = client.post(
            "/v2/highlights/identify-srt",
            files={"file": ("empty.srt", b"not an srt", "application/x-subrip")},
        )

    assert oversized.status_code == 413
    assert empty.status_code == 400


def test_srt_upload_rejects_legacy_llm_transport():
    with TestClient(create_app(settings=_settings())) as client:
        response = client.post(
            "/v2/highlights/identify-srt",
            data={
                "llm_endpoint": "https://provider.example/v1/chat/completions",
                "llm_model": "model-name",
                "llm_compatibility_mode": "legacy",
            },
            files={"file": ("empty.srt", b"", "application/x-subrip")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported LLM compatibility mode"


def test_srt_upload_provider_override_omits_model_when_not_supplied(monkeypatch):
    api = create_app(
        settings=_settings(llm=LLMSettings(model="configured-model", max_retries=0))
    )
    captured = {}

    def build_client(settings):
        captured["settings"] = settings
        return FakeLLMClient([])

    monkeypatch.setattr(main, "LLMClient", build_client)
    with TestClient(api) as client:
        response = client.post(
            "/v2/highlights/identify-srt",
            data={
                "llm_endpoint": "https://provider.example/v3/chat/completions",
            },
            files={"file": ("empty.srt", b"", "application/x-subrip")},
        )

    assert response.status_code == 400
    assert captured["settings"].base_url == (
        "https://provider.example/v3/chat/completions"
    )
    assert captured["settings"].model is None


def test_srt_upload_provider_override_uses_supplied_model(monkeypatch):
    api = create_app(settings=_settings())
    captured = {}

    def build_client(settings):
        captured["settings"] = settings
        return FakeLLMClient([])

    monkeypatch.setattr(main, "LLMClient", build_client)
    with TestClient(api) as client:
        response = client.post(
            "/v2/highlights/identify-srt",
            data={
                "llm_endpoint": "https://provider.example/v1/chat/completions",
                "llm_model": "explicit-model",
            },
            files={"file": ("empty.srt", b"", "application/x-subrip")},
        )

    assert response.status_code == 400
    assert captured["settings"].model == "explicit-model"


def test_background_job_lifecycle_uses_shared_window_plan():
    with TestClient(create_app(settings=_settings())) as client:
        created = client.post("/v2/highlights/jobs", json=_empty_request())
        assert created.status_code == 202
        job_id = created.json()["job_id"]

        status = client.get(f"/v2/highlights/jobs/{job_id}")
        result = client.get(f"/v2/highlights/jobs/{job_id}/result")

    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["progress"] == {"windows_done": 0, "windows_total": 0}
    assert result.status_code == 200
    assert result.json()["source_id"] == "episode-empty"


def test_async_srt_topic_focused_request_reaches_independent_verifier():
    def response_factory(prompt, response_model, _schema_name):
        if response_model is DiscoveryResponse:
            assert "Focus topics: AI" in prompt
            assert "Topic policy: hard" in prompt
            return {
                "candidates": [
                    {
                        "title": "How AI models learn",
                        "summary": "Training data shapes the model.",
                        "reason": "The clip directly explains AI.",
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
                    }
                ]
            }

        candidate_id = re.search(r"^candidate_id: (\S+)$", prompt, re.MULTILINE)
        assert candidate_id is not None
        candidate_id = candidate_id.group(1)
        if response_model is TopicVerificationResponse:
            assert "Focus topics exactly as entered by the user: AI" in prompt
            return {
                "candidates": [
                    {
                        "candidate_id": candidate_id,
                        "match": "direct",
                        "confidence": 0.96,
                        "evidence_quote": "trained an AI model",
                        "reason": "The passage directly discusses AI model training.",
                    }
                ]
            }
        assert response_model is ClipMetadataResponse
        return {
            "clips": [
                {
                    "candidate_id": candidate_id,
                    "title_string": "AI Model Training",
                    "best_headlines": ["How AI Learns", "Training Shapes AI", "Inside AI"],
                    "summary": "training data leaves fingerprints.",
                    "best_headline": "How AI Learns",
                    "best_questions": [
                        "How does AI learn?",
                        "What shapes an AI model?",
                        "Can data leave fingerprints?",
                    ],
                    "best_question": "How does AI learn?",
                    "best_description_sentences": [
                        "Training data determines the patterns an artificial intelligence model can learn."
                    ],
                    "best_quotes": [
                        "trained an AI model",
                        "patterns in its training",
                        "its training data",
                    ],
                    "hashtags": "ai, models, training",
                    "keywords": "AI, model training, training data, patterns, software",
                }
            ]
        }

    fake = FakeLLMClient(response_factory=response_factory)
    srt = (
        "1\n"
        "00:00:00,000 --> 00:00:30,000\n"
        "We trained an AI model and found patterns in its training data.\n"
    )
    with TestClient(create_app(settings=_settings(), llm_client=fake)) as client:
        created = client.post(
            "/v2/highlights/srt-jobs",
            data={
                "topics": "AI",
                "topic_policy": "hard",
                "admission_strictness": "0.65",
                "min_clip_duration_seconds": "10",
                "max_clip_duration_seconds": "60",
                "candidates_per_window": "5",
            },
            files={"file": ("ai.srt", srt.encode(), "application/x-subrip")},
        )
        assert created.status_code == 202
        job_id = created.json()["job_id"]
        result = client.get(f"/v2/highlights/jobs/{job_id}/result")

    assert result.status_code == 200
    assert len(result.json()["clips"]) == 1
    assert result.json()["clips"][0]["topic_verification"]["match"] == "direct"
    assert [call[1] for call in fake.calls] == [
        DiscoveryResponse,
        TopicVerificationResponse,
        ClipMetadataResponse,
    ]


def test_unknown_job_returns_not_found():
    with TestClient(create_app(settings=_settings())) as client:
        response = client.get("/v2/highlights/jobs/job_missing")
    assert response.status_code == 404


_CLIP_SRT = (
    b"1\n00:00:00,000 --> 00:00:04,000\nThe ocean absorbs most of the extra heat.\n\n"
    b"2\n00:00:04,000 --> 00:00:09,500\nThat changes how storms form every season.\n"
)


def _clip_metadata(**overrides) -> dict:
    values = {
        "candidate_id": "clip_001",
        "title_string": "Ocean heat and storms",
        "best_headlines": [
            "The ocean is quietly storing heat",
            "Why storms are getting stronger",
            "Heat hiding under the waves",
        ],
        "summary": "the ocean is doing the heavy lifting",
        "best_headline": "Why storms are getting stronger",
        "best_questions": [
            "Where does the heat go?",
            "Why are storms stronger?",
            "Is the ocean a battery?",
        ],
        "best_question": "Where does the heat go?",
        "best_description_sentences": [
            "Oceans absorb most excess heat.",
            "Warm water fuels storms.",
            "Seasons shift as a result.",
        ],
        "best_quotes": [
            "The ocean absorbs most",
            "of the extra heat",
            "how storms form every season",
        ],
        "hashtags": "ocean, climate, storms",
        "keywords": "ocean heat, climate, storms, hurricanes, weather",
    }
    values.update(overrides)
    return values


def test_clip_metadata_srt_treats_whole_transcript_as_one_clip():
    fake = FakeLLMClient([ClipMetadataResponse.model_validate({"clips": [_clip_metadata()]})])
    with TestClient(create_app(settings=_settings(), llm_client=fake)) as client:
        response = client.post(
            "/v2/clips/metadata-srt",
            data={"title": "Ocean heat"},
            files={"file": ("ocean.srt", _CLIP_SRT, "application/x-subrip")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == "ocean"
    assert body["title"] == "Ocean heat"
    assert body["llm_calls"] == 1
    clip = body["clip"]
    assert clip["start_seconds"] == 0
    assert clip["end_seconds"] == 9.5
    assert clip["best_headline"] == "Why storms are getting stronger"
    assert clip["best_question"] == "Where does the heat go?"
    assert clip["hashtags"] == "ocean, climate, storms"
    assert clip["best_description"] == (
        "Oceans absorb most excess heat. Warm water fuels storms.\n\n"
        "Seasons shift as a result."
    )
    assert len(fake.calls) == 1
    prompt, response_model, _schema = fake.calls[0]
    assert response_model is ClipMetadataResponse
    assert "That changes how storms form every season." in prompt


def test_clip_metadata_srt_retries_overlong_headlines_once():
    overlong = _clip_metadata(
        best_headlines=[
            "This headline has far too many words to be usable here",
            "Why storms are getting stronger",
            "Heat hiding under the waves",
        ],
        best_headline="This headline has far too many words to be usable here",
    )
    fake = FakeLLMClient(
        [
            ClipMetadataResponse.model_validate({"clips": [overlong]}),
            ClipMetadataResponse.model_validate({"clips": [_clip_metadata()]}),
        ]
    )
    with TestClient(create_app(settings=_settings(), llm_client=fake)) as client:
        response = client.post(
            "/v2/clips/metadata-srt",
            files={"file": ("ocean.srt", _CLIP_SRT, "application/x-subrip")},
        )

    assert response.status_code == 200
    assert response.json()["llm_calls"] == 2
    assert response.json()["clip"]["best_headline"] == "Why storms are getting stronger"
    assert fake.calls[1][2] == "clip_metadata_word_limit_retry"


def test_clip_metadata_srt_maps_llm_errors_and_rejects_empty_srt():
    fake = FakeLLMClient([LLMConnectionError("provider unavailable", attempts=3)])
    with TestClient(create_app(settings=_settings(), llm_client=fake)) as client:
        failed = client.post(
            "/v2/clips/metadata-srt",
            files={"file": ("ocean.srt", _CLIP_SRT, "application/x-subrip")},
        )
        empty = client.post(
            "/v2/clips/metadata-srt",
            files={"file": ("empty.srt", b"not an srt", "application/x-subrip")},
        )

    assert failed.status_code == 502
    assert failed.json()["detail"] == "provider unavailable"
    assert empty.status_code == 400
