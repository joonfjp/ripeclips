from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol

from .models import HighlightResult, JobProgress, JobStatus


class JobNotFoundError(KeyError):
    pass


class JobStoreFullError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JobRecord:
    status: JobStatus
    result: HighlightResult | None
    created_at: float
    updated_at: float
    expires_at: float


class JobStore(Protocol):
    def create(self, total_windows: int) -> JobStatus: ...

    def get(self, job_id: str) -> JobRecord: ...

    def get_status(self, job_id: str) -> JobStatus: ...

    def set_status(self, job_id: str, status: JobStatus) -> None: ...

    def complete(self, job_id: str, result: HighlightResult) -> None: ...

    def cleanup(self) -> int: ...


class InMemoryJobStore:
    """Thread-safe local job store with bounded capacity and expiry."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_jobs: int,
        clock: Callable[[], float] = time.monotonic,
    ):
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_jobs = max_jobs
        self._clock = clock
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def create(self, total_windows: int) -> JobStatus:
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            if len(self._jobs) >= self._max_jobs:
                self._evict_oldest_terminal_locked()
            if len(self._jobs) >= self._max_jobs:
                raise JobStoreFullError(
                    "Job capacity is full; wait for an active job to finish or expire"
                )

            job_id = f"job_{uuid.uuid4().hex[:12]}"
            status = JobStatus(
                job_id=job_id,
                status="queued",
                stage="queued",
                progress=JobProgress(windows_done=0, windows_total=total_windows),
            )
            self._jobs[job_id] = JobRecord(
                status=status,
                result=None,
                created_at=now,
                updated_at=now,
                expires_at=now + self._ttl_seconds,
            )
            return status.model_copy(deep=True)

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            return JobRecord(
                status=record.status.model_copy(deep=True),
                result=record.result.model_copy(deep=True) if record.result else None,
                created_at=record.created_at,
                updated_at=record.updated_at,
                expires_at=record.expires_at,
            )

    def get_status(self, job_id: str) -> JobStatus:
        """Status-only read; skips copying the (potentially large) result."""

        with self._lock:
            now = self._clock()
            self._cleanup_locked(now)
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            return record.status.model_copy(deep=True)

    def set_status(self, job_id: str, status: JobStatus) -> None:
        with self._lock:
            now = self._clock()
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            self._jobs[job_id] = replace(
                record,
                status=status.model_copy(deep=True),
                updated_at=now,
                expires_at=now + self._ttl_seconds,
            )

    def complete(self, job_id: str, result: HighlightResult) -> None:
        with self._lock:
            now = self._clock()
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            status = JobStatus(
                job_id=job_id,
                status="completed",
                stage="completed",
                progress=JobProgress(
                    windows_done=result.processing_summary.window_count,
                    windows_total=result.processing_summary.window_count,
                ),
            )
            self._jobs[job_id] = replace(
                record,
                status=status,
                result=result.model_copy(deep=True),
                updated_at=now,
                expires_at=now + self._ttl_seconds,
            )

    def cleanup(self) -> int:
        with self._lock:
            return self._cleanup_locked(self._clock())

    def _cleanup_locked(self, now: float) -> int:
        expired = [
            job_id
            for job_id, record in self._jobs.items()
            if record.expires_at <= now
        ]
        for job_id in expired:
            del self._jobs[job_id]
        return len(expired)

    def _evict_oldest_terminal_locked(self) -> None:
        terminal = [
            (job_id, record)
            for job_id, record in self._jobs.items()
            if record.status.status in {"completed", "failed"}
        ]
        if not terminal:
            return
        oldest_job_id, _ = min(terminal, key=lambda item: item[1].updated_at)
        del self._jobs[oldest_job_id]
