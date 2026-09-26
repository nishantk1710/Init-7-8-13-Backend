"""FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import contextlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import root
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.schemas.i7.errors import ApiError, ErrorBody, ErrorResponse
from app.initiatives.i13 import snapshot as i13_snapshot

logger = get_logger(__name__)

#: Response headers the browser is allowed to read (fetch hides the rest
#: from JS on a cross-origin call): the page total, and which I13 snapshot
#: answered.
EXPOSED_HEADERS = [
    "X-Total-Count",
    "X-I13-Snapshot-Built-At",
    "X-I13-Data-As-Of",
    "X-I13-Snapshot-Status",
    "X-I13-History-Months",
    "Retry-After",
]


def _watch_i13_fingerprint(stop: threading.Event, interval: int) -> None:
    """Rebuild the I13 snapshot when a reseed (or a new day) changes its
    fingerprint. One cheap grouped query per interval."""
    from app.core.db import get_sessionmaker

    while not stop.wait(interval):
        try:
            db = get_sessionmaker()()
            try:
                i13_snapshot.check_fingerprint(db, min_interval_seconds=0)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- a failed check must never kill the watcher
            logger.exception("I13 snapshot fingerprint check failed")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Start the I13 snapshot build and the delta scheduler; stop both on shutdown.

    The server takes requests immediately; I13 snapshot routes answer 503
    ``building`` until the first build lands (~40 s on the seeded data).

    The scheduler is imported here rather than at module scope so that
    importing app.main -- which the test suite and every CLI entry point do --
    never reaches the database. The scheduler decides for itself whether it is
    switched on.
    """
    from app.ingest import scheduler

    settings = get_settings()
    stop = threading.Event()
    if settings.i13_snapshot_enabled and settings.i13_snapshot_warm_on_startup:
        i13_snapshot.start_background_build("start-up")
        threading.Thread(
            target=_watch_i13_fingerprint,
            args=(stop, max(settings.i13_snapshot_check_interval_seconds, 5)),
            name="i13-snapshot-watch",
            daemon=True,
        ).start()

    task = None
    try:
        task = scheduler.start(application)
    except Exception:
        # A scheduler that cannot start must not take the API down with it.
        logger.exception("the delta scheduler failed to start; the API is unaffected")

    try:
        yield
    finally:
        stop.set()
        if task is not None:
            task.cancel()
            with contextlib.suppress(Exception):
                await task
            logger.info("delta scheduler stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings)

    application = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)

    @application.middleware("http")
    async def i13_snapshot_headers(request: Request, call_next):
        """Say which I13 snapshot answered, on every I13 response."""
        response = await call_next(request)
        if request.url.path.startswith(f"{settings.api_prefix}/i13"):
            current = i13_snapshot.peek_i13_snapshot()
            if current is not None:
                response.headers["X-I13-Snapshot-Built-At"] = current.built_at.isoformat()
                response.headers["X-I13-Data-As-Of"] = current.reference_date.isoformat()
                response.headers["X-I13-Snapshot-Status"] = (
                    "rebuilding" if i13_snapshot.snapshot_status()["rebuilding"] else "ready"
                )
        return response

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
    )

    # Service index at `/`, outside the API prefix.
    application.include_router(root.router)
    application.include_router(api_router, prefix=settings.api_prefix)

    @application.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        """The single place an I07 domain error becomes an HTTP response.

        Routes raise ``not_found`` / ``conflict`` / ``bad_request`` and never
        format a response body themselves -- this keeps the error envelope
        consistent (``{"error": {"code", "message", "details"}}``) without
        duplicating that shape in every route.
        """
        body = ErrorResponse(error=ErrorBody(code=exc.code, message=exc.message, details=exc.error_details))
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Log unexpected failures instead of leaking internals to the caller.

        Registering on the bare ``Exception`` class makes Starlette treat this as
        the ServerErrorMiddleware handler, which sits *outside* CORSMiddleware in
        the stack -- so its response never gets CORS headers added automatically.
        A browser then refuses to expose the response to JS at all and fetch()
        reports "Failed to fetch", indistinguishable from the backend being
        unreachable. Add the header by hand so real 500s surface as 500s.
        """
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
        origin = request.headers.get("origin")
        if origin in settings.cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    logger.info(
        "%s v%s ready (env=%s, api_prefix=%s, cors=%s)",
        settings.app_name,
        settings.app_version,
        settings.app_env,
        settings.api_prefix,
        settings.cors_origins,
    )
    return application


app = create_app()
