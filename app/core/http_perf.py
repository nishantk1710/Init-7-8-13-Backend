"""HTTP-level speed: response compression, and a timer that says where time goes.

Nothing here knows about an initiative. Both are wired in ``app.main.create_app``.

Compression
-----------
JSON is repetitive: a 100-row I08 register page is ~156 KB on the wire and
compresses about eight-fold. Starlette's ``GZipMiddleware`` already leaves
images, zips and event streams alone; Excel and PDF downloads are compressed
files too, so they are added to that list (:data:`NO_GZIP_TYPES`).

Timing
------
The access log the App Service keeps has no durations, so a slow screen could
not be traced to a route. :class:`RequestTimingMiddleware` adds a
``Server-Timing: app;dur=<ms>`` header (time until the response STARTED, which
the browser's Network tab shows as "Server Timing") and logs a line for any
request that takes ``slow_seconds`` or more in total, body included::

    slow request: GET /api/i8/register -> 200 in 1.43s

The route is the TEMPLATE (``/api/i8/repairs/{id}``), never the query string:
a search box value should not end up in a log.
"""

from __future__ import annotations

import inspect
import time

from fastapi.middleware.gzip import GZipMiddleware
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Already-compressed downloads. Starlette's own list covers images, zips and
#: event streams; these are the Office and PDF files the API hands out.
NO_GZIP_TYPES: tuple[str, ...] = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/pdf",
)


def gzip_options(minimum_size: int) -> dict[str, object]:
    """Keyword arguments for ``GZipMiddleware``.

    Level 5 rather than Starlette's 9: the last few percent of size cost
    several times the CPU. ``exclude_content_types`` is passed only where the
    installed Starlette has it (it is not pinned here); without it Excel and
    PDF are compressed too, which is wasteful and harmless.
    """
    options: dict[str, object] = {"minimum_size": minimum_size, "compresslevel": 5}
    if "exclude_content_types" in inspect.signature(GZipMiddleware.__init__).parameters:
        from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES

        options["exclude_content_types"] = tuple(DEFAULT_EXCLUDED_CONTENT_TYPES) + NO_GZIP_TYPES
    return options


class RequestTimingMiddleware:
    """Adds ``Server-Timing`` to every HTTP response and logs the slow requests.

    ``slow_seconds`` of 0 (or less) switches the log off; the header stays.
    Pure ASGI, so it never buffers a response the way a ``BaseHTTPMiddleware``
    can.
    """

    def __init__(self, app: ASGIApp, *, slow_seconds: float = 0.0) -> None:
        self.app = app
        self.slow_seconds = slow_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status: int | None = None

        async def timed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                millis = (time.perf_counter() - started) * 1000
                MutableHeaders(scope=message).append("Server-Timing", f"app;dur={millis:.0f}")
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        except BaseException:
            if status is None:
                status = 500  # an unhandled error: the server's own handler answers
            raise
        finally:
            elapsed = time.perf_counter() - started
            if self.slow_seconds > 0 and elapsed >= self.slow_seconds:
                route = scope.get("route")
                path = getattr(route, "path", None) or scope.get("path", "")
                logger.info(
                    "slow request: %s %s -> %s in %.2fs",
                    scope.get("method", "?"),
                    path,
                    status if status is not None else "-",
                    elapsed,
                )
