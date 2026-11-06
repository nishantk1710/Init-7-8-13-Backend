"""FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

import contextlib
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.api import root
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.db import pool_status
from app.core.http_perf import RequestTimingMiddleware, gzip_options
from app.core.logging import configure_logging, get_logger
from app.core.security import azure_sdk_identity_warning
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


def _watch_fingerprint(
    label: str,
    check: Callable[..., object],
    stop: threading.Event,
    interval: int,
) -> None:
    """Rebuild a snapshot when a reload (or a new day) changes its fingerprint.

    One cheap grouped query per interval, per initiative. ``check`` is called
    with ``min_interval_seconds=0`` because this loop is already the interval;
    its own rate limit is there for any caller that is not.

    Shared by I08 and I13 rather than written twice: the loop is entirely about
    not letting a failed check kill the thread, and that is not a property worth
    maintaining in two copies.
    """
    from app.core.db import get_sessionmaker

    while not stop.wait(interval):
        try:
            db = get_sessionmaker()()
            try:
                check(db, min_interval_seconds=0)
            finally:
                db.close()
        except Exception:  # noqa: BLE001 -- a failed check must never kill the watcher
            logger.exception("%s snapshot fingerprint check failed", label)


def _warm_on_startup(settings: Settings) -> None:
    """The start-up warm-up. Runs on its own thread, so the server takes requests at once.

    The views first, then the in-memory loaders, then the I08 and I13 snapshots,
    one after the other and never side by side: built together they starved the
    one-vCore Azure SQL (see app/shared/snapshot_builds.py).

    One exception. When a stored I08 snapshot is ready, I08 starts as soon as the
    material names are in, ahead of the delivery-date map (~20 s on the nonprod
    extract) and the material index (~5 s). Serving a stored snapshot is a read of
    one row and a decode of ~0.2 MB, not a build, so it can sit beside those two
    loaders; before this, I08 answered 503 "building" for the ~25 s they took
    (measured on nonprod, 4-Nov-2026). The names stay first, as before: screens ask
    for them as soon as the server is up. With nothing stored (a new database, or
    a codec bump) I08 has to build, and keeps its place after the loaders.

    A failed warm-up must not take the API down: it is logged and the API serves.
    """
    try:
        i8_warm: threading.Thread | None = None
        i8_wanted = bool(settings.database_url and settings.i8_snapshot_warm_on_startup)
        if settings.database_url:
            # First, so the I08 views and snapshots below are built over the
            # criticality the July delivery supplies. Idempotent, and it
            # logs rather than raises when the database or seed is absent.
            from app.integrations.criticality import baseline

            baseline.run()
        if settings.database_url and settings.normalise_views_on_startup:
            from app.shared import sap_normalise

            # Skipped when another worker or instance is rebuilding them.
            sap_normalise.rebuild_read_layers(at_start_up=True)
        if settings.database_url:
            # After the views (n_makt is one), before the snapshot builds:
            # material names are then ready by the time screens ask, rather
            # than loading on the first request after every deploy.
            from app.shared import material_names

            material_names.start_loading()
            material_names.wait_for_load(material_names.STARTUP_WAIT_SECONDS)

            # I08 now, when a stored snapshot is ready: it is a load (see the
            # docstring), and no I08 route reads what the two loaders below
            # produce -- the I13 ledger takes the delivery dates, the assistant's
            # material search takes the index. I13's snapshot still waits for I08.
            if i8_wanted:
                from app.initiatives.i8 import service as i8_service
                from app.initiatives.i8.snapshot_store import lifecycle as i8_store

                if i8_store.stored_ready():
                    i8_service.start_background_build("start-up")
                    i8_warm = i8_service.build_thread()

            # EKET is small: the ledger's expected delivery dates, loaded
            # now so the first ledger page has them.
            from app.initiatives.i13 import expected_delivery

            expected_delivery.start_loading()
            expected_delivery.wait_for_load(material_names.STARTUP_WAIT_SECONDS)

            # The material search's copy of MAKT, ZMM065, MARC and MARD.
            # Waited for, like names: it scans the same views, and the
            # snapshot builds below should not scan alongside it.
            from app.assistant import material_index

            material_index.start_loading()
            material_index.wait_for_load(material_index.STARTUP_WAIT_SECONDS)

            # The opener's department list (n_eban). Not waited for: it is
            # one DISTINCT, and until it lands the opener checks the
            # code's shape only.
            from app.assistant import departments

            departments.start_loading()
        # Nothing stored to load (or the check failed): I08 builds here, after the
        # loaders, as it always did.
        if i8_wanted and i8_warm is None:
            from app.initiatives.i8 import service as i8_service

            i8_service.start_background_build("start-up")
            i8_warm = i8_service.build_thread()
        if settings.i13_snapshot_enabled and settings.i13_snapshot_warm_on_startup:
            i13_snapshot.start_background_build("start-up", after=i8_warm)
    except Exception:  # a failed warm-up must not take the API down
        logger.exception("start-up warm-up failed; the API keeps serving")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Build the normalise views, then start the I08 and I13 snapshot builds
    (in that order, one after the other) and the delta scheduler; stop them on
    shutdown.

    The server takes requests immediately; I08 and I13 snapshot routes answer
    503 ``building`` until their first build lands. I13's waits for I08's, so
    it lands after both have run (~40 s for I13 alone on the seeded data).

    The scheduler is imported here rather than at module scope so that
    importing app.main -- which the test suite and every CLI entry point do --
    never reaches the database. The scheduler decides for itself whether it is
    switched on.
    """
    from app.ingest import scheduler

    settings = get_settings()

    # The views first, then I08, then I13 -- one after the other, never side by
    # side (built together they starved the one-vCore Azure SQL; see
    # app/shared/snapshot_builds.py) -- but all of it in the background.
    #
    # The view rebuild used to be awaited here, before the server listened. On
    # the production extract, with the database busy, it outlasted Azure's
    # 230-second start-up probe and Azure killed the container (10 Oct 2026,
    # 13:37). The views persist in the database, so the previous ones serve
    # while they are rebuilt; only a brand-new database has none for a moment.
    stop = threading.Event()

    threading.Thread(target=_warm_on_startup, args=(settings,), name="start-up-warm", daemon=True).start()

    if settings.database_url and settings.i8_snapshot_warm_on_startup:
        from app.initiatives.i8 import service as i8_service

        # I08 gets the same watcher I13 has. Without it a CSV full pull -- which
        # replaces raw_<table> whole -- was invisible to the register until
        # somebody restarted the App Service.
        threading.Thread(
            target=_watch_fingerprint,
            args=(
                "I08",
                i8_service.check_source_fingerprint,
                stop,
                max(settings.i8_snapshot_check_interval_seconds, 5),
            ),
            name="i8-snapshot-watch",
            daemon=True,
        ).start()

    if settings.i13_snapshot_enabled and settings.i13_snapshot_warm_on_startup:
        threading.Thread(
            target=_watch_fingerprint,
            args=(
                "I13",
                i13_snapshot.check_fingerprint,
                stop,
                max(settings.i13_snapshot_check_interval_seconds, 5),
            ),
            name="i13-snapshot-watch",
            daemon=True,
        ).start()

    if settings.database_url and settings.i7_pipeline_watch_enabled:
        # Off by default. Starts only a timer thread; a run begins only inside
        # an open nightly slot, never at start-up -- see pipeline_scheduler.
        from app.initiatives.i7 import pipeline_scheduler

        pipeline_scheduler.start(stop, settings)

    if settings.database_url:
        # Sends queued emails, reminds and escalates approvals, and escalates
        # unanswered I13 exceptions. Reminders do nothing until SLA timings are
        # configured, and email is only logged until Microsoft Graph is set up.
        from app.integrations.notifications import scheduler as notification_scheduler
        from app.services.notification_jobs import build_jobs

        notification_scheduler.start(stop, settings, build_jobs())

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
        # A delta a PR event asked for and has not started yet: cancelled, not
        # left to run against a process that is going away.
        with contextlib.suppress(Exception):
            await scheduler.stop_event_runner()
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

    # Compression is added FIRST, so it is the innermost layer and sees each
    # response whole. The I13 header middleware below is a BaseHTTPMiddleware,
    # which re-streams every body in chunks; gzip outside it cannot tell how
    # big a streamed body is and compresses even a 60-byte one.
    if settings.response_gzip_enabled:
        application.add_middleware(GZipMiddleware, **gzip_options(settings.response_gzip_min_bytes))

    @application.middleware("http")
    async def i13_snapshot_headers(request: Request, call_next):
        """Say which I13 snapshot answered, on every I13 response."""
        response = await call_next(request)
        if request.url.path.startswith(f"{settings.api_prefix}/i13"):
            served = i13_snapshot.served_info()
            if served is not None:
                response.headers["X-I13-Snapshot-Built-At"] = served["built_at"].isoformat()
                response.headers["X-I13-Data-As-Of"] = served["reference_date"].isoformat()
                response.headers["X-I13-Snapshot-Status"] = "rebuilding" if served["rebuilding"] else "ready"
        return response

    # The last middleware added is the outermost: CORS stays outside the
    # compression and the I13 header middleware, so it labels every response,
    # errors included; the timer goes in last so it sees the whole request.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
    )
    application.add_middleware(RequestTimingMiddleware, slow_seconds=settings.slow_request_log_seconds)

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
        if isinstance(exc, SqlAlchemyTimeoutError):
            # Waited pool_timeout for a connection and got none: say how full
            # the pool was, so the next shortage is sized rather than guessed.
            logger.error("Database connection pool exhausted: %s", pool_status())
        response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
        origin = request.headers.get("origin")
        if origin in settings.cors_origins:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    identity_warning = azure_sdk_identity_warning()
    if identity_warning:
        logger.warning(identity_warning)

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
