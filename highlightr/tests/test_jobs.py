from __future__ import annotations

import pytest

from app.jobs import InMemoryJobStore, JobNotFoundError, JobStoreFullError
from app.models import (
    HighlightResult,
    JobProgress,
    JobStatus,
    ProcessingSummary,
    RejectedStats,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def empty_result(window_count: int = 1) -> HighlightResult:
    return HighlightResult(
        source_id="source",
        title="Source",
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
            window_count=window_count,
            llm_calls=0,
            mode="llm_structured",
            version="test",
            discovery_window_seconds=600,
            stride_seconds=300,
        ),
    )


def test_job_store_lifecycle_and_result_copying():
    store = InMemoryJobStore(ttl_seconds=60, max_jobs=10)
    queued = store.create(total_windows=2)
    store.set_status(
        queued.job_id,
        JobStatus(
            job_id=queued.job_id,
            status="running",
            stage="identifying",
            progress=JobProgress(windows_done=1, windows_total=2),
        ),
    )
    assert store.get(queued.job_id).status.progress.windows_done == 1

    result = empty_result(window_count=2)
    store.complete(queued.job_id, result)
    record = store.get(queued.job_id)
    assert record.status.status == "completed"
    assert record.result == result
    assert record.result is not result


def test_job_store_expires_records():
    clock = Clock()
    store = InMemoryJobStore(
        ttl_seconds=10,
        max_jobs=10,
        clock=clock,
    )
    status = store.create(total_windows=1)
    clock.advance(10)
    assert store.cleanup() == 1
    with pytest.raises(JobNotFoundError):
        store.get(status.job_id)


def test_job_store_rejects_capacity_when_every_job_is_active():
    store = InMemoryJobStore(ttl_seconds=60, max_jobs=1)
    store.create(total_windows=1)
    with pytest.raises(JobStoreFullError):
        store.create(total_windows=1)


def test_job_store_evicts_oldest_terminal_record_at_capacity():
    clock = Clock()
    store = InMemoryJobStore(
        ttl_seconds=60,
        max_jobs=1,
        clock=clock,
    )
    first = store.create(total_windows=1)
    store.set_status(
        first.job_id,
        JobStatus(
            job_id=first.job_id,
            status="failed",
            stage="failed",
            progress=JobProgress(windows_done=0, windows_total=1),
            error="failed",
        ),
    )
    clock.advance(1)
    second = store.create(total_windows=1)
    assert second.job_id != first.job_id
    with pytest.raises(JobNotFoundError):
        store.get(first.job_id)


def test_get_status_returns_status_without_result():
    store = InMemoryJobStore(ttl_seconds=60, max_jobs=10)
    queued = store.create(total_windows=1)
    store.complete(queued.job_id, empty_result())
    status = store.get_status(queued.job_id)
    assert status.status == "completed"
    assert status.progress.windows_done == 1
    with pytest.raises(JobNotFoundError):
        store.get_status("job_missing")


def test_job_store_stays_bounded_after_many_completed_jobs():
    clock = Clock()
    max_jobs = 5
    store = InMemoryJobStore(ttl_seconds=3600, max_jobs=max_jobs, clock=clock)
    job_ids = []
    for _ in range(2 * max_jobs):
        status = store.create(total_windows=1)
        store.complete(status.job_id, empty_result())
        job_ids.append(status.job_id)
        clock.advance(1)
        assert len(store._jobs) <= max_jobs

    # The newest max_jobs survive; older completed jobs were evicted.
    for job_id in job_ids[:max_jobs]:
        with pytest.raises(JobNotFoundError):
            store.get(job_id)
    for job_id in job_ids[max_jobs:]:
        assert store.get(job_id).status.status == "completed"
