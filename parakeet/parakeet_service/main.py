"""FastAPI app factory + lifespan."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config
from .batchworker import build_worker
from .config import AUDIO_WORKERS, DEFAULT_MODEL, logger
from .model import get_model, load_model
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Lifespan startup: loading default model")
    load_model(DEFAULT_MODEL)
    app.state.worker = build_worker(get_model)
    await app.state.worker.start()
    app.state.audio_pool = ThreadPoolExecutor(
        max_workers=AUDIO_WORKERS, thread_name_prefix="audio"
    )
    logger.info("Service ready")
    try:
        yield
    finally:
        logger.info("Lifespan shutdown")
        await app.state.worker.stop()
        app.state.audio_pool.shutdown(wait=False, cancel_futures=True)


# Allowance for multipart boundaries and form fields on top of the file itself.
_MULTIPART_SLACK = 64 * 1024


class UploadLimitMiddleware:
    """Reject oversized bodies by Content-Length before they are spooled to disk.

    Chunked uploads without a Content-Length are still bounded per file by the
    route's streaming read.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "POST":
            limit = config.MAX_UPLOAD_BYTES + _MULTIPART_SLACK
            for name, value in scope["headers"]:
                if name == b"content-length":
                    if value.isdigit() and int(value) > limit:
                        await send({
                            "type": "http.response.start",
                            "status": 413,
                            "headers": [(b"content-type", b"application/json")],
                        })
                        await send({
                            "type": "http.response.body",
                            "body": b'{"detail":"upload exceeds size limit"}',
                        })
                        return
                    break
        await self.app(scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(
        title="Parakeet TDT 0.6B v3 (optimized)",
        version="1.0.0",
        description="High-throughput OpenAI-compatible ASR service for Parakeet TDT 0.6B v3.",
        lifespan=lifespan,
    )
    app.add_middleware(UploadLimitMiddleware)
    app.include_router(router)
    return app


app = create_app()
