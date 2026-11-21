from __future__ import annotations

import asyncio
import io
import threading
import time
import wave

import numpy as np
import pytest

from parakeet_service import config, model, routes
from parakeet_service.batchworker import InferencePool, QueueFullError
from tests.fakes import FakeModel

URL = "/v1/audio/transcriptions"


def _wav_bytes(seconds: float = 0.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.TARGET_SR)
        w.writeframes(b"\x00\x01" * int(seconds * config.TARGET_SR))
    return buf.getvalue()


def test_transcribe_returns_text(make_client):
    client = make_client()
    resp = client.post(URL, files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
    assert resp.status_code == 200
    assert resp.json() == {"text": "hello world"}


def test_upload_over_limit_is_rejected_by_content_length(make_client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)
    client = make_client()
    resp = client.post(URL, files={"file": ("a.wav", b"\0" * 200_000, "audio/wav")})
    assert resp.status_code == 413


def test_upload_over_limit_is_rejected_by_streaming_read(make_client, monkeypatch):
    # Small enough to pass the Content-Length guard (which allows multipart
    # slack), but over the per-file limit enforced while reading.
    monkeypatch.setattr(config, "MAX_UPLOAD_BYTES", 1024)
    client = make_client()
    resp = client.post(
        URL + "/batch",
        files=[("files", ("a.wav", b"\0" * 4096, "audio/wav"))],
    )
    assert resp.status_code == 413


def test_health_is_not_blocked_by_slow_decode(make_client, monkeypatch):
    decoding = threading.Event()

    def slow_load_audio(raw):
        decoding.set()
        time.sleep(1.0)
        return np.zeros(config.TARGET_SR, dtype=np.float32)

    monkeypatch.setattr(routes, "load_audio", slow_load_audio)
    client = make_client()

    result = {}

    def post():
        result["resp"] = client.post(URL, files={"file": ("a.mp3", b"ID3fake", "audio/mpeg")})

    t = threading.Thread(target=post)
    t.start()
    assert decoding.wait(2.0)
    started = time.perf_counter()
    health = client.get("/healthz")
    elapsed = time.perf_counter() - started
    t.join(5.0)

    assert health.status_code == 200
    assert elapsed < 0.5, f"/healthz took {elapsed:.2f}s while decode was running"
    assert result["resp"].status_code == 200


def test_model_loads_once_across_concurrent_requests(make_client, monkeypatch):
    loads = []
    _patch_slow_loader(monkeypatch, loads)
    client = make_client(get_model=model.get_model)
    responses = []

    def post():
        responses.append(
            client.post(URL, files={"file": ("a.wav", _wav_bytes(), "audio/wav")})
        )

    threads = [threading.Thread(target=post) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10.0)

    assert [r.status_code for r in responses] == [200] * 6
    assert len(loads) == 1


def _patch_slow_loader(monkeypatch, loads):
    def slow_onnx_load(hf_id, **kwargs):
        loads.append(hf_id)
        time.sleep(0.3)
        return FakeModel()

    monkeypatch.setattr(model, "_MODELS", {})
    monkeypatch.setattr(model.onnx_asr, "load_model", slow_onnx_load)
    monkeypatch.setattr(model, "_resolve_providers", lambda: ["CPUExecutionProvider"])
    monkeypatch.setattr(model, "_build_sess_options", lambda **kw: None)
    monkeypatch.setattr(model, "_validate_gpu_binding", lambda name, m: None)


def test_get_model_loads_once_across_threads(monkeypatch):
    loads = []
    _patch_slow_loader(monkeypatch, loads)
    got = []
    threads = [
        threading.Thread(target=lambda: got.append(model.get_model(config.DEFAULT_MODEL)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)
    assert len(loads) == 1
    assert len(got) == 8 and all(m is got[0] for m in got)


def test_inference_pool_rejects_when_queue_is_full():
    slow = FakeModel(delay=0.3)
    pool = InferencePool(lambda name: slow, workers=1, max_queue=2)
    wav = np.zeros(10, dtype=np.float32)

    async def scenario():
        first = asyncio.create_task(pool.submit_many([wav, wav], "m"))
        await asyncio.sleep(0.05)
        with pytest.raises(QueueFullError):
            await pool.submit_many([wav], "m")
        await first
        # Capacity is released once the in-flight request finishes.
        assert len(await pool.submit_many([wav], "m")) == 1

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(pool.stop())


def test_slice_chunks_returns_views():
    from parakeet_service.chunker import slice_chunks

    wav = np.arange(10, dtype=np.float32)
    pieces = slice_chunks(wav, [(0, 4), (4, 10)])
    assert all(np.shares_memory(p, wav) for p in pieces)
    assert pieces[1].tolist() == list(range(4, 10))
