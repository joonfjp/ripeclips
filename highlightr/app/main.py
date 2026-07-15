from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .highlights import identify_highlights, resolve_window_and_stride
from .models import (
    HighlightResult,
    IdentifyHighlightsRequest,
    JobProgress,
    JobStatus,
    ProcessingOptions,
    SelectionOptions,
    Transcript,
)
from .srt import decode_srt_bytes, parse_srt


app = FastAPI(
    title="Ripe Clips API",
    version="0.1.0",
    description="Row-backed highlight identification for long-form transcripts.",
)

# The browser app may run on either local development port. Tauri's native HTTP
# client does not need CORS, but allowing these local origins keeps both paths
# working without exposing the API to arbitrary websites.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

_jobs: dict[str, dict[str, object]] = {}
_jobs_lock = threading.Lock()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v2/highlights/identify", response_model=HighlightResult)
def identify(request: IdentifyHighlightsRequest) -> HighlightResult:
    try:
        return identify_highlights(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _parse_topics(value: str | None) -> list[str]:
    if not value:
        return []
    return [topic.strip() for topic in value.split(",") if topic.strip()]


async def _build_request_from_srt_upload(
    file: UploadFile = File(...),
    source_id: str | None = Form(default=None),
    language: str | None = Form(default=None),
    max_clip_duration_seconds: int = Form(default=1200),
    min_clip_duration_seconds: int = Form(default=60),
    max_clips: int | None = Form(default=None),
    allow_overlap_seconds: int = Form(default=0),
    target: str = Form(default="best"),
    style: str | None = Form(default="insightful, surprising, emotionally engaging"),
    topics: str | None = Form(default=None),
    topic_policy: str = Form(default="soft"),
    admission_strictness: float = Form(default=0.7),
    min_candidates_per_window: int = Form(default=0),
    discovery_window_seconds: int | None = Form(default=None),
    stride_seconds: int | None = Form(default=None),
    candidates_per_window: int = Form(default=5),
    dedupe_overlap_threshold: float = Form(default=0.65),
) -> IdentifyHighlightsRequest:
    raw = await file.read()
    rows = parse_srt(decode_srt_bytes(raw))
    if not rows:
        raise HTTPException(status_code=400, detail="No caption rows found in SRT file")

    return IdentifyHighlightsRequest(
        transcript=Transcript(
            source_id=source_id or Path(file.filename or "uploaded.srt").stem,
            language=language,
            rows=rows,
        ),
        selection=SelectionOptions(
            max_clip_duration_seconds=max_clip_duration_seconds,
            min_clip_duration_seconds=min_clip_duration_seconds,
            max_clips=max_clips,
            allow_overlap_seconds=allow_overlap_seconds,
            target=target,
            style=style,
            topics=_parse_topics(topics),
            topic_policy=topic_policy,  # type: ignore[arg-type]
            admission_strictness=admission_strictness,
            min_candidates_per_window=min_candidates_per_window,
        ),
        processing=ProcessingOptions(
            discovery_window_seconds=discovery_window_seconds,
            stride_seconds=stride_seconds,
            candidates_per_window=candidates_per_window,
            dedupe_overlap_threshold=dedupe_overlap_threshold,
        ),
    )


SrtRequest = Annotated[IdentifyHighlightsRequest, Depends(_build_request_from_srt_upload)]


@app.post("/v2/highlights/identify-srt", response_model=HighlightResult)
def identify_srt(identify_request: SrtRequest) -> HighlightResult:
    try:
        return identify_highlights(identify_request)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v2/highlights/srt-jobs", response_model=JobStatus, status_code=202)
def create_srt_job(
    identify_request: SrtRequest,
    background_tasks: BackgroundTasks,
) -> JobStatus:
    return _create_job(identify_request, background_tasks)


@app.post("/v2/highlights/jobs", response_model=JobStatus, status_code=202)
def create_job(
    request: IdentifyHighlightsRequest,
    background_tasks: BackgroundTasks,
) -> JobStatus:
    return _create_job(request, background_tasks)


def _create_job(
    request: IdentifyHighlightsRequest,
    background_tasks: BackgroundTasks,
) -> JobStatus:
    job_id = f"job_{uuid.uuid4().hex[:12]}"
    total_windows = _estimate_window_count(request)
    status = JobStatus(
        job_id=job_id,
        status="queued",
        stage="queued",
        progress=JobProgress(windows_done=0, windows_total=total_windows),
    )

    with _jobs_lock:
        _jobs[job_id] = {"status": status, "result": None}

    background_tasks.add_task(_run_job, job_id, request)
    return status


@app.get("/v2/highlights/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    job = _get_job(job_id)
    return job["status"]  # type: ignore[return-value]


@app.get("/v2/highlights/jobs/{job_id}/result", response_model=HighlightResult)
def get_job_result(job_id: str) -> HighlightResult:
    job = _get_job(job_id)
    status = job["status"]
    if not isinstance(status, JobStatus):
        raise HTTPException(status_code=500, detail="Job state is corrupt")
    if status.status == "failed":
        raise HTTPException(status_code=500, detail=status.error or "Job failed")
    if status.status != "completed":
        raise HTTPException(status_code=409, detail="Job is not complete")

    result = job.get("result")
    if not isinstance(result, HighlightResult):
        raise HTTPException(status_code=500, detail="Job result is missing")
    return result


def _run_job(job_id: str, request: IdentifyHighlightsRequest) -> None:
    total_windows = _estimate_window_count(request)
    _set_job_status(
        job_id,
        JobStatus(
            job_id=job_id,
            status="running",
            stage="identifying",
            progress=JobProgress(windows_done=0, windows_total=total_windows),
        ),
    )

    try:
        def update_progress(done: int, total: int, stage: str) -> None:
            _set_job_status(
                job_id,
                JobStatus(
                    job_id=job_id,
                    status="running",
                    stage=stage,
                    progress=JobProgress(windows_done=done, windows_total=total),
                ),
            )

        result = identify_highlights(request, progress_callback=update_progress)
    except Exception as exc:  # pragma: no cover - defensive job boundary
        current_progress = _current_job_progress(job_id, total_windows)
        _set_job_status(
            job_id,
            JobStatus(
                job_id=job_id,
                status="failed",
                stage="failed",
                progress=current_progress,
                error=str(exc),
            ),
        )
        return

    with _jobs_lock:
        job = _jobs[job_id]
        job["result"] = result
        job["status"] = JobStatus(
            job_id=job_id,
            status="completed",
            stage="completed",
            progress=JobProgress(
                windows_done=result.processing_summary.window_count,
                windows_total=result.processing_summary.window_count,
            ),
        )


def _get_job(job_id: str) -> dict[str, object]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _set_job_status(job_id: str, status: JobStatus) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = status


def _current_job_progress(job_id: str, fallback_total: int) -> JobProgress:
    with _jobs_lock:
        job = _jobs.get(job_id)
        status = job.get("status") if job else None
    if isinstance(status, JobStatus):
        return status.progress
    return JobProgress(windows_done=0, windows_total=fallback_total)


def _estimate_window_count(request: IdentifyHighlightsRequest) -> int:
    rows = request.transcript.rows
    if not rows:
        return 0

    transcript_start = min(row.start_seconds for row in rows)
    transcript_end = max(row.end_seconds for row in rows)
    duration = max(0.0, transcript_end - transcript_start)
    window, stride = resolve_window_and_stride(
        request.selection.max_clip_duration_seconds,
        request.processing.discovery_window_seconds,
        request.processing.stride_seconds,
    )
    if duration <= 0:
        return 0
    if duration <= window:
        return 1
    return int((duration - window + stride - 1) // stride) + 1
