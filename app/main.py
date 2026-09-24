"""FastAPI application entry point.

Run locally:
    uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import root
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger

logger = get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings)

    application = FastAPI(title=settings.app_name, version=settings.app_version)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Service index at `/`, outside the API prefix.
    application.include_router(root.router)
    application.include_router(api_router, prefix=settings.api_prefix)

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
