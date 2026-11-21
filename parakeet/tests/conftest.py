"""Shared fixtures for the Parakeet service tests.

The tests never run real inference, so when onnxruntime / onnx_asr are not
installed (e.g. on a dev laptop) minimal stand-in modules are registered so
`parakeet_service.model` can be imported. Everything that would touch them is
monkeypatched per test.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # required by fastapi.testclient
pytest.importorskip("numpy")
pytest.importorskip("audioop")  # stdlib, removed in Python 3.13

os.environ.setdefault("PARAKEET_USE_GPU", "false")
os.environ.setdefault("PARAKEET_BATCHED", "0")
os.environ.setdefault("PARAKEET_DEFAULT_MODEL", "parakeet-tdt-0.6b-v3")


def _stub_module(name: str, **attrs) -> None:
    try:
        __import__(name)
    except ImportError:
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod


_stub_module("onnxruntime", get_available_providers=lambda: ["CPUExecutionProvider"])
_stub_module("onnx_asr", load_model=lambda *a, **k: None)

from tests.fakes import FakeModel  # noqa: E402


@pytest.fixture
def make_client(monkeypatch):
    """Build a TestClient around a fresh app whose model lookup is faked."""
    from fastapi.testclient import TestClient

    from parakeet_service import main

    clients = []

    def _make(get_model=None):
        model = FakeModel()
        monkeypatch.setattr(main, "load_model", lambda *a, **k: model)
        monkeypatch.setattr(main, "get_model", get_model or (lambda name: model))
        client = TestClient(main.create_app())
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)
