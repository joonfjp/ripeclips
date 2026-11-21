from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import AppSettings, LLMSettings, get_settings
from .highlight_candidates import plan_highlight_windows
from .highlights import generate_clip_metadata, identify_highlights
from .jobs import (
    InMemoryJobStore,
    JobNotFoundError,
    JobRecord,
    JobStore,
    JobStoreFullError,
)
from .llm import LLMClient, LLMError
from .models import (
    ClipMetadataResult,
    HighlightResult,
    IdentifyHighlightsRequest,
    JobProgress,
    JobStatus,
    ProcessingOptions,
    SelectionOptions,
    Transcript,
)
from .srt import decode_srt_bytes, parse_srt
from .version import __version__

logger = logging.getLogger(__name__)
router = APIRouter()


def create_app(
    *,
    settings: AppSettings | None = None,
    llm_client: LLMClient | None = None,
    job_store: JobStore | None = None,
) -> FastAPI:
    configured_settings = settings or get_settings()
    api = FastAPI(
        title="Ripe Clips API",
        version=__version__,
        description="Row-backed highlight identification for long-form transcripts.",
    )
    api.state.settings = configured_settings
    api.state.llm_client = llm_client or LLMClient(configured_settings.llm)
    api.state.job_store = job_store or InMemoryJobStore(
        ttl_seconds=configured_settings.job_ttl_seconds,
        max_jobs=configured_settings.max_jobs,
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(configured_settings.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    @api.exception_handler(LLMError)
    async def handle_llm_error(_request: Request, error: LLMError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(error)})

    api.include_router(router)
    return api


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/ready")
def ready(request: Request) -> dict[str, str | bool | None]:
    settings: AppSettings = request.app.state.settings
    return {
        "status": "ready",
        "configured": True,
        "llm_base_url": settings.llm.base_url,
        "llm_model": settings.llm.model,
    }


@router.post("/v2/highlights/identify", response_model=HighlightResult)
def identify(
    payload: IdentifyHighlightsRequest,
    request: Request,
) -> HighlightResult:
    return identify_highlights(
        payload,
        llm_client=request.app.state.llm_client,
    )


def _parse_topics(value: str | None) -> list[str]:
    if not value:
        return []
    return [topic.strip() for topic in value.split(",") if topic.strip()]


def _apply_llm_override(
    request: Request,
    *,
    llm_endpoint: str | None,
    llm_model: str | None,
    llm_api_key: str | None,
    llm_compatibility_mode: str | None,
) -> None:
    if llm_endpoint is None:
        return
    settings: AppSettings = request.app.state.settings
    compatibility = (llm_compatibility_mode or "openai").strip().lower()
    if compatibility not in {"openai", "openrouter", "generic"}:
        raise HTTPException(status_code=400, detail="Unsupported LLM compatibility mode")
    try:
        values = settings.llm.model_dump()
        values.update(
            base_url=llm_endpoint,
            model=llm_model,
            api_key=llm_api_key or "",
            require_api_key=False,
        )
        request.state.llm_client = LLMClient(LLMSettings.model_validate(values))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"Invalid LLM provider: {error}") from error


async def _read_srt_transcript(
    request: Request,
    file: UploadFile,
    *,
    source_id: str | None,
    title: str | None,
    language: str | None,
) -> Transcript:
    settings: AppSettings = request.app.state.settings
    raw = await file.read(settings.max_srt_upload_bytes + 1)
    if len(raw) > settings.max_srt_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=(
                f"SRT upload exceeds the {settings.max_srt_upload_bytes:,}-byte limit"
            ),
        )
    rows = parse_srt(decode_srt_bytes(raw))
    if not rows:
        raise HTTPException(status_code=400, detail="No caption rows found in SRT file")
    return Transcript(
        source_id=source_id or Path(file.filename or "uploaded.srt").stem,
        title=title,
        language=language,
        rows=rows,
    )


async def _build_request_from_srt_upload(
    request: Request,
    file: Annotated[UploadFile, File()],
    source_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
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
    llm_endpoint: str | None = Form(default=None),
    llm_model: str | None = Form(default=None),
    llm_api_key: str | None = Form(default=None),
    llm_compatibility_mode: str | None = Form(default=None),
) -> IdentifyHighlightsRequest:
    _apply_llm_override(
        request,
        llm_endpoint=llm_endpoint,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_compatibility_mode=llm_compatibility_mode,
    )
    return IdentifyHighlightsRequest(
        transcript=await _read_srt_transcript(
            request,
            file,
            source_id=source_id,
            title=title,
            language=language,
        ),
        selection=SelectionOptions(
            max_clip_duration_seconds=max_clip_duration_seconds,
            min_clip_duration_seconds=min_clip_duration_seconds,
            max_clips=max_clips,
            allow_overlap_seconds=allow_overlap_seconds,
            target=target,
            style=style,
            topics=_parse_topics(topics),
            topic_policy=topic_policy,
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


@router.post("/v2/highlights/identify-srt", response_model=HighlightResult)
def identify_srt(
    identify_request: SrtRequest,
    request: Request,
) -> HighlightResult:
    return identify_highlights(
        identify_request,
        llm_client=getattr(request.state, "llm_client", request.app.state.llm_client),
    )


@router.post("/v2/clips/metadata-srt", response_model=ClipMetadataResult)
async def clip_metadata_srt(
    request: Request,
    file: Annotated[UploadFile, File()],
    source_id: str | None = Form(default=None),
    title: str | None = Form(default=None),
    language: str | None = Form(default=None),
    llm_endpoint: str | None = Form(default=None),
    llm_model: str | None = Form(default=None),
    llm_api_key: str | None = Form(default=None),
    llm_compatibility_mode: str | None = Form(default=None),
) -> ClipMetadataResult:
    """Generate post copy for an SRT that already covers exactly one clip."""
    _apply_llm_override(
        request,
        llm_endpoint=llm_endpoint,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        llm_compatibility_mode=llm_compatibility_mode,
    )
    transcript = await _read_srt_transcript(
        request,
        file,
        source_id=source_id,
        title=title,
        language=language,
    )
    llm_client = getattr(request.state, "llm_client", request.app.state.llm_client)
    return await run_in_threadpool(
        generate_clip_metadata,
        transcript,
        llm_client=llm_client,
    )


@router.post("/v2/highlights/srt-jobs", response_model=JobStatus, status_code=202)
def create_srt_job(
    identify_request: SrtRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> JobStatus:
    return _create_job(
        identify_request,
        background_tasks,
        store=request.app.state.job_store,
        llm_client=getattr(request.state, "llm_client", request.app.state.llm_client),
    )


@router.post("/v2/highlights/jobs", response_model=JobStatus, status_code=202)
def create_job(
    payload: IdentifyHighlightsRequest,
    background_tasks: BackgroundTasks,
    request: Request,
) -> JobStatus:
    return _create_job(
        payload,
        background_tasks,
        store=request.app.state.job_store,
        llm_client=request.app.state.llm_client,
    )


def _create_job(
    request: IdentifyHighlightsRequest,
    background_tasks: BackgroundTasks,
    *,
    store: JobStore,
    llm_client: LLMClient,
) -> JobStatus:
    try:
        status = store.create(_estimate_window_count(request))
    except JobStoreFullError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    background_tasks.add_task(
        _run_job,
        status.job_id,
        request,
        store,
        llm_client,
    )
    return status


@router.get("/v2/highlights/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, request: Request) -> JobStatus:
    try:
        return request.app.state.job_store.get_status(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@router.get("/v2/highlights/jobs/{job_id}/result", response_model=HighlightResult)
def get_job_result(job_id: str, request: Request) -> HighlightResult:
    record = _get_job_record(request.app.state.job_store, job_id)
    if record.status.status == "failed":
        raise HTTPException(
            status_code=500,
            detail=record.status.error or "Job failed",
        )
    if record.status.status != "completed":
        raise HTTPException(status_code=409, detail="Job is not complete")
    if record.result is None:
        raise HTTPException(status_code=500, detail="Job result is missing")
    return record.result


def _run_job(
    job_id: str,
    request: IdentifyHighlightsRequest,
    store: JobStore,
    llm_client: LLMClient,
) -> None:
    total_windows = _estimate_window_count(request)
    store.set_status(
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
            store.set_status(
                job_id,
                JobStatus(
                    job_id=job_id,
                    status="running",
                    stage=stage,
                    progress=JobProgress(windows_done=done, windows_total=total),
                ),
            )

        result = identify_highlights(
            request,
            progress_callback=update_progress,
            llm_client=llm_client,
        )
    except Exception as exc:  # pragma: no cover - defensive job boundary
        logger.exception("Highlight job failed job_id=%s", job_id)
        try:
            current_progress = store.get_status(job_id).progress
            store.set_status(
                job_id,
                JobStatus(
                    job_id=job_id,
                    status="failed",
                    stage="failed",
                    progress=current_progress,
                    error=str(exc),
                ),
            )
        except JobNotFoundError:
            logger.warning("Failed job expired before status update job_id=%s", job_id)
        return

    try:
        store.complete(job_id, result)
    except JobNotFoundError:
        logger.warning("Completed job expired before result storage job_id=%s", job_id)


def _get_job_record(store: JobStore, job_id: str) -> JobRecord:
    try:
        return store.get(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


def _estimate_window_count(request: IdentifyHighlightsRequest) -> int:
    return len(plan_highlight_windows(request))


app = create_app()
