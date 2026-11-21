"""Inference pool for the optimized Parakeet v3 service.

The default deployment is GPU-backed and uses cross-request micro-batching.
For CPU INT8 deployments, set `PARAKEET_BATCHED=0`; batched `recognize([N])`
scales close to linear in time on CPU and is counter-productive there.
"""
from __future__ import annotations
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, List, Optional

import numpy as np

from .config import (
    BATCHED,
    BATCH_WINDOW_MS,
    INFER_WORKERS,
    MAX_BATCH_SIZE,
    MAX_QUEUE,
    logger,
)


class QueueFullError(RuntimeError):
    """Raised when admitting more chunks would exceed the inference queue bound."""


def _check_capacity(pending: int, incoming: int, limit: int) -> None:
    # An idle server always admits one request, even if it alone exceeds the
    # bound, so very long files are never permanently rejected.
    if pending and pending + incoming > limit:
        raise QueueFullError(
            f"inference queue full ({pending} pending, limit {limit})"
        )


async def _resolve_model(get_model_fn, model_name: str):
    # First use of a model may load it from disk; keep that off the event loop.
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, get_model_fn, model_name)


@dataclass
class _Job:
    wav: np.ndarray
    model_name: str
    future: asyncio.Future = field(default_factory=asyncio.Future)


class InferencePool:
    """Default mode: parallel single-item ORT calls (CPU-friendly)."""

    def __init__(self, get_model_fn, *, workers: int = INFER_WORKERS,
                 max_queue: int = MAX_QUEUE):
        self._get_model = get_model_fn
        self._workers = max(1, workers)
        self._max_queue = max_queue
        self._pending = 0
        self._executor = ThreadPoolExecutor(
            max_workers=self._workers, thread_name_prefix="ort"
        )
        logger.info("InferencePool started (workers=%d)", self._workers)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def submit(self, wav: np.ndarray, model_name: str) -> Any:
        return (await self.submit_many([wav], model_name))[0]

    async def submit_many(self, wavs: List[np.ndarray], model_name: str) -> List[Any]:
        if not wavs:
            return []
        _check_capacity(self._pending, len(wavs), self._max_queue)
        self._pending += len(wavs)
        try:
            loop = asyncio.get_running_loop()
            model = await _resolve_model(self._get_model, model_name)
            tasks = [loop.run_in_executor(self._executor, model.recognize, w) for w in wavs]
            return list(await asyncio.gather(*tasks))
        finally:
            self._pending -= len(wavs)


class BatchWorker:
    """Cross-request micro-batching (recommended for GPU only)."""

    def __init__(self, get_model_fn, *, max_batch: int = MAX_BATCH_SIZE,
                 window_ms: float = BATCH_WINDOW_MS, max_queue: int = MAX_QUEUE):
        self._get_model = get_model_fn
        self._max_batch = max_batch
        self._window_s = window_ms / 1000.0
        self._max_queue = max_queue
        # Admission is bounded in submit*(); the queue itself stays unbounded so
        # the worker can re-queue a mismatched job without blocking on itself.
        self._queue: asyncio.Queue[_Job] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ort")

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="batch_worker")
            logger.info("BatchWorker started (max_batch=%d window=%.1fms)",
                        self._max_batch, self._window_s * 1000)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def submit(self, wav: np.ndarray, model_name: str) -> Any:
        return (await self.submit_many([wav], model_name))[0]

    async def submit_many(self, wavs: List[np.ndarray], model_name: str) -> List[Any]:
        _check_capacity(self._queue.qsize(), len(wavs), self._max_queue)
        jobs = [_Job(wav=w, model_name=model_name) for w in wavs]
        for j in jobs:
            self._queue.put_nowait(j)
        return await asyncio.gather(*(j.future for j in jobs))

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                return
            batch: List[_Job] = [first]
            deadline = time.monotonic() + self._window_s
            while len(batch) < self._max_batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    nxt = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if nxt.model_name != first.model_name:
                    await self._queue.put(nxt)
                    break
                batch.append(nxt)

            wavs = [b.wav for b in batch]
            try:
                model = await _resolve_model(self._get_model, first.model_name)
                results = await loop.run_in_executor(
                    self._executor, _infer_batch, model, wavs
                )
            except Exception as exc:
                logger.exception("batch inference failed (size=%d)", len(batch))
                for b in batch:
                    if not b.future.done():
                        b.future.set_exception(exc)
                continue
            for b, r in zip(batch, results):
                if not b.future.done():
                    b.future.set_result(r)


def _infer_batch(model, wavs: List[np.ndarray]):
    if len(wavs) == 1:
        return [model.recognize(wavs[0])]
    return list(model.recognize(wavs))


def build_worker(get_model_fn):
    """Factory: choose pool mode based on env."""
    if BATCHED:
        logger.info("Using BatchWorker (cross-request micro-batching)")
        return BatchWorker(get_model_fn)
    return InferencePool(get_model_fn)
