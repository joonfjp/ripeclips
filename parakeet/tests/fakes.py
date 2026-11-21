from __future__ import annotations

import time
from types import SimpleNamespace


class FakeModel:
    """Stands in for an onnx_asr model: recognize() returns a timestamped result."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls = 0

    def with_timestamps(self):
        return self

    def recognize(self, wav):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return SimpleNamespace(text="hello world", tokens=[], timestamps=[])
