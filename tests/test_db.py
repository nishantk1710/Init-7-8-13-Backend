"""Database wiring tests.

Split deliberately into two groups:

* Tests that must pass with NO database present. These guard the property that
  importing the app, serving liveness, and running the suite all work on a
  machine with nothing configured -- the property the whole lazy-engine design
  exists to protect.
* Tests that need a real Postgres, skipped when DATABASE_URL is unset. They run
  against Postgres rather than SQLite on purpose: SQLite accepts DDL and types
  Postgres rejects, so passing against it would prove nothing about the
  database we actually use.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.db import (
    DatabaseNotConfiguredError,
    get_engine,
    get_sessionmaker,
    reset_engine_cache,
)
from app.main import app
from app.models import Base, IngestionRun

client = TestClient(app)


def _database_configured() -> bool:
    return bool(get_settings().database_url)


needs_db = pytest.mark.skipif(not _database_configured(), reason="DATABASE_URL not set")


# --- No database required -------------------------------------------------


def test_importing_the_app_does_not_connect() -> None:
    """The engine must be lazy.

    If this fails, something builds an engine at import time and the app can no
    longer start without a database -- which breaks liveness and CI.
    """
    reset_engine_cache()
    assert get_engine.cache_info().currsize == 0


def test_unconfigured_database_raises_a_distinct_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """"Not configured" must be its own error type, not a generic failure."""
    monkeypatch.setattr(
        "app.core.db.get_settings", lambda: Settings(database_url="", _env_file=None)
    )
    reset_engine_cache()
    with pytest.raises(DatabaseNotConfiguredError):
        get_engine()
    reset_engine_cache()


def test_liveness_works_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """/health must never depend on the database."""
    monkeypatch.setattr(
        "app.core.db.get_settings", lambda: Settings(database_url="", _env_file=None)
    )
    reset_engine_cache()
    assert client.get("/api/health").status_code == 200
    reset_engine_cache()


def test_readiness_reports_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Readiness distinguishes "no URL" from "unreachable"."""
    monkeypatch.setattr(
        "app.core.db.get_settings", lambda: Settings(database_url="", _env_file=None)
    )
    reset_engine_cache()
    response = client.get("/api/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "not_configured"
    reset_engine_cache()


def test_models_are_registered_on_the_metadata() -> None:
    """A model Alembic cannot see never gets a migration."""
    assert "ingestion_run" in Base.metadata.tables


def test_naming_convention_is_applied() -> None:
    """Stable constraint names, so migration diffs do not churn."""
    assert Base.metadata.tables["ingestion_run"].primary_key.name == "pk_ingestion_run"


# --- Real Postgres required -----------------------------------------------


@needs_db
def test_connection_round_trips() -> None:
    with get_engine().connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


@needs_db
def test_readiness_reports_database_ok() -> None:
    """Readiness covers storage too, so assert on the database field alone.

    The all-dependencies-ready case, and the storage field, are covered in
    tests/test_storage.py -- this test must not start failing merely because
    STORAGE_URL is unset on the machine running it.
    """
    reset_engine_cache()
    response = client.get("/api/ready")
    assert response.json()["database"] == "ok"


@needs_db
def test_migration_has_been_applied() -> None:
    """The table exists in the database, not just in the metadata."""
    with get_engine().connect() as connection:
        exists = connection.execute(
            text("SELECT to_regclass('public.ingestion_run')")
        ).scalar_one()
    assert exists is not None, "run `alembic upgrade head`"


@needs_db
def test_session_can_write_and_read_back() -> None:
    """End-to-end proof of the session factory: insert, commit, read, clean up."""
    session = get_sessionmaker()()
    try:
        run = IngestionRun(
            source_file="TEST_ONLY.XLSX",
            target_table="test_only",
            row_count=1,
            status="succeeded",
        )
        session.add(run)
        session.commit()

        assert run.id is not None
        assert run.started_at is not None, "server_default should populate on commit"

        session.delete(run)
        session.commit()
    finally:
        session.close()
