"""Response compression and the request timer (app/core/http_perf.py).

A 100-row I08 register page is ~156 KB of JSON on the wire and the API sent it
uncompressed; and nothing in the logs said how long a request took. These
cover what has to stay true once both are on:

* JSON over the threshold is gzipped, small bodies and clients that do not ask
  for gzip are not, and the switch turns it off;
* CORS still labels a compressed response (and an error), because the browser
  discards a response whose CORS headers are missing;
* Excel and PDF downloads are not compressed a second time;
* every response carries ``Server-Timing``, a slow request is logged by its
  route template, and its query string never reaches the log.

``create_app`` is built without entering its lifespan, so no warm-up runs and
no database is needed.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re

import pytest
from fastapi import FastAPI, Response
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.http_perf import NO_GZIP_TYPES, RequestTimingMiddleware, gzip_options
from app.main import create_app

ORIGIN = "http://localhost:3000"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _client(**overrides) -> TestClient:
    settings = get_settings().model_copy(update={"frontend_origin": ORIGIN, **overrides})
    return TestClient(create_app(settings))


# --- compression -----------------------------------------------------------------


def test_big_json_is_gzipped_for_a_client_that_accepts_it():
    client = _client()

    packed = client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
    plain = client.get("/openapi.json", headers={"Accept-Encoding": "identity"})

    assert packed.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    assert packed.json() == plain.json()  # the client decodes it back to the same JSON
    assert int(packed.headers["content-length"]) < int(plain.headers["content-length"]) / 3


def test_a_small_response_is_left_alone():
    response = _client().get("/api/health", headers={"Accept-Encoding": "gzip"})

    assert response.status_code == 200
    assert "content-encoding" not in response.headers


def test_the_switch_turns_compression_off():
    response = _client(response_gzip_enabled=False).get("/openapi.json", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in response.headers


def test_the_threshold_is_a_setting():
    response = _client(response_gzip_min_bytes=10_000_000).get("/openapi.json", headers={"Accept-Encoding": "gzip"})

    assert "content-encoding" not in response.headers


def test_cors_still_labels_a_compressed_response():
    response = _client().get("/openapi.json", headers={"Accept-Encoding": "gzip", "Origin": ORIGIN})

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_cors_still_labels_an_error_response():
    response = _client().get("/api/no-such-route", headers={"Accept-Encoding": "gzip", "Origin": ORIGIN})

    assert response.status_code == 404
    assert response.headers["access-control-allow-origin"] == ORIGIN


def test_gzip_options_uses_a_cheaper_level_and_skips_office_and_pdf_files():
    options = gzip_options(2048)

    assert options["minimum_size"] == 2048
    assert options["compresslevel"] == 5
    if "exclude_content_types" in inspect.signature(GZipMiddleware.__init__).parameters:
        for kind in (*NO_GZIP_TYPES, "image/png", "text/event-stream"):
            assert kind in options["exclude_content_types"]


@pytest.mark.skipif(
    "exclude_content_types" not in inspect.signature(GZipMiddleware.__init__).parameters,
    reason="this Starlette cannot exclude content types",
)
def test_excel_and_pdf_downloads_are_not_compressed_again():
    app = FastAPI()
    app.add_middleware(GZipMiddleware, **gzip_options(100))
    body = b"x" * 5000

    @app.get("/sheet")
    def sheet():
        return Response(content=body, media_type=XLSX)

    @app.get("/report.pdf")
    def pdf():
        return Response(content=body, media_type="application/pdf")

    @app.get("/rows")
    def rows():
        return {"rows": ["x" * 50] * 100}

    client = TestClient(app)
    headers = {"Accept-Encoding": "gzip"}

    assert "content-encoding" not in client.get("/sheet", headers=headers).headers
    assert "content-encoding" not in client.get("/report.pdf", headers=headers).headers
    assert client.get("/rows", headers=headers).headers["content-encoding"] == "gzip"


# --- timing ----------------------------------------------------------------------


def _timed_app(slow_seconds: float) -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestTimingMiddleware, slow_seconds=slow_seconds)

    @app.get("/fast")
    async def fast():
        return {"ok": True}

    @app.get("/slow/{item_id}")
    async def slow(item_id: int):
        await asyncio.sleep(0.05)
        return {"id": item_id}

    @app.get("/boom")
    async def boom():
        await asyncio.sleep(0.05)
        raise RuntimeError("unhandled")

    return TestClient(app, raise_server_exceptions=False)


def _slow_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("slow request")]


def test_every_response_carries_a_server_timing_header(caplog):
    caplog.set_level(logging.INFO, logger="app.core.http_perf")

    response = _timed_app(slow_seconds=5).get("/fast")

    assert re.fullmatch(r"app;dur=\d+", response.headers["server-timing"])
    assert _slow_lines(caplog) == []  # quick, so nothing logged


def test_a_slow_request_is_logged_by_its_route_template(caplog):
    caplog.set_level(logging.INFO, logger="app.core.http_perf")

    response = _timed_app(slow_seconds=0.01).get("/slow/42")

    assert response.status_code == 200
    (line,) = _slow_lines(caplog)
    assert line.startswith("slow request: GET /slow/{item_id} -> 200 in ")
    assert "/slow/42" not in line  # the template, not the id


def test_the_query_string_never_reaches_the_log(caplog):
    caplog.set_level(logging.INFO, logger="app.core.http_perf")

    _timed_app(slow_seconds=0.01).get("/slow/7?q=confidential-search-text")

    (line,) = _slow_lines(caplog)
    assert "confidential" not in line


def test_an_unhandled_error_is_logged_as_a_500(caplog):
    caplog.set_level(logging.INFO, logger="app.core.http_perf")

    response = _timed_app(slow_seconds=0.01).get("/boom")

    assert response.status_code == 500
    (line,) = _slow_lines(caplog)
    assert line.startswith("slow request: GET /boom -> 500 in ")


def test_zero_switches_the_log_off_but_keeps_the_header(caplog):
    caplog.set_level(logging.INFO, logger="app.core.http_perf")

    response = _timed_app(slow_seconds=0).get("/slow/1")

    assert _slow_lines(caplog) == []
    assert "server-timing" in response.headers
