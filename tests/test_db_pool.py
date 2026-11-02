"""The connection pool: sized by settings, and a shortage says how full it was.

SQLAlchemy's own 5 + 10 ran out on nonprod on 14 Oct 2026 ("QueuePool limit of
size 5 overflow 10 reached") with requests and the background jobs sharing it.
Building an engine does not connect, so none of this needs a database.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.core import db
from app.core.config import Settings

# Imported here, not in the tests: importing app.main builds the app, which
# replaces the root log handlers -- pytest's capture among them, were it
# already installed for a test.
from app.main import create_app

AZURE_SQL_URL = (
    "mssql+pyodbc://user:pass@example.database.windows.net:1433/sqldb"
    "?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes"
)


@pytest.fixture
def engine_with(monkeypatch: pytest.MonkeyPatch):
    """Build the process engine from the given settings; restore the real one after."""

    def build(**settings):
        monkeypatch.setattr(
            "app.core.db.get_settings",
            lambda: Settings(database_url=AZURE_SQL_URL, _env_file=None, **settings),
        )
        db.reset_engine_cache()
        return db.get_engine()

    yield build
    db.reset_engine_cache()


def test_the_defaults_are_ten_kept_and_twenty_more_under_a_burst(engine_with) -> None:
    pool = engine_with().pool
    assert pool.size() == 10
    assert pool._max_overflow == 20
    assert pool._timeout == 30


def test_each_is_an_app_setting(engine_with) -> None:
    pool = engine_with(database_pool_size=4, database_max_overflow=6, database_pool_timeout_seconds=12).pool
    assert (pool.size(), pool._max_overflow, pool._timeout) == (4, 6, 12)


def test_a_mistyped_setting_cannot_leave_the_app_without_connections(engine_with) -> None:
    pool = engine_with(database_pool_size=0, database_max_overflow=-3, database_pool_timeout_seconds=0).pool
    assert (pool.size(), pool._max_overflow, pool._timeout) == (1, 0, 1)


def test_what_was_already_set_on_the_engine_is_unchanged(engine_with) -> None:
    engine = engine_with()
    assert engine.pool._pre_ping is True
    assert engine.pool._recycle == 1500


def test_pool_status_reads_the_pool(engine_with) -> None:
    engine_with(database_pool_size=3)
    assert "Pool size: 3" in db.pool_status()


def test_pool_status_never_creates_an_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    db.reset_engine_cache()
    monkeypatch.setattr(db, "get_settings", lambda: pytest.fail("pool_status built an engine"))
    assert db.pool_status() == "no engine created yet"


def test_a_request_that_waited_out_the_pool_logs_how_full_it_was(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # create_app() replaces the root log handlers, pytest's capture among them.
    monkeypatch.setattr("app.main.configure_logging", lambda settings: None)
    monkeypatch.setattr("app.main.pool_status", lambda: "Pool size: 10  Checked out connections: 30")
    application = create_app()
    router = APIRouter()

    @router.get("/pool-test")
    def exhausted() -> None:
        raise SqlAlchemyTimeoutError("QueuePool limit of size 10 overflow 20 reached")

    application.include_router(router)
    with caplog.at_level(logging.ERROR):
        response = TestClient(application, raise_server_exceptions=False).get("/pool-test")

    assert response.status_code == 500
    assert "Database connection pool exhausted: Pool size: 10  Checked out connections: 30" in caplog.text


def test_any_other_error_logs_no_pool_line(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # create_app() replaces the root log handlers, pytest's capture among them.
    monkeypatch.setattr("app.main.configure_logging", lambda settings: None)
    monkeypatch.setattr("app.main.pool_status", lambda: pytest.fail("read the pool for an unrelated error"))
    application = create_app()
    router = APIRouter()

    @router.get("/other-test")
    def broken() -> None:
        raise RuntimeError("something else")

    application.include_router(router)
    with caplog.at_level(logging.ERROR):
        response = TestClient(application, raise_server_exceptions=False).get("/other-test")

    assert response.status_code == 500
    assert "pool exhausted" not in caplog.text
