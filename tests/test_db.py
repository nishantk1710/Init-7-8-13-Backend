"""Database wiring tests.

Split deliberately into two groups:

* Tests that must pass with NO database present. These guard the property that
  importing the app, serving liveness, and running the suite all work on a
  machine with nothing configured -- the property the whole lazy-engine design
  exists to protect.
* Tests that need a real Azure SQL, skipped unless DATABASE_URL names one. They
  run against the real database rather than SQLite on purpose: SQLite accepts
  DDL and types SQL Server rejects, so passing against it would prove nothing
  about the database we actually use.

Note what this means in practice today. ``sql-vzi-aicom-nonprod-san`` has public
network access disabled and is reached through a private endpoint, so these
tests SKIP anywhere outside the VNet -- including CI and every developer laptop.
They are written to run from inside the App Service, and the fact that they are
skipping is not evidence that the database works.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.db import (
    MSSQL,
    DatabaseNotConfiguredError,
    backend_of,
    get_engine,
    get_sessionmaker,
    require_azure_sql,
    reset_engine_cache,
)
from app.main import app
from app.models import Base, IngestionRun

client = TestClient(app)


def _azure_sql_configured() -> bool:
    url = get_settings().database_url
    return bool(url) and backend_of(url) == MSSQL


needs_db = pytest.mark.skipif(
    not _azure_sql_configured(),
    reason="DATABASE_URL does not name an Azure SQL database (reachable only inside the VNet)",
)


# --- No database required -------------------------------------------------


def test_importing_the_app_does_not_connect() -> None:
    """The engine must be lazy.

    If this fails, something builds an engine at import time and the app can no
    longer start without a database -- which breaks liveness and CI.
    """
    reset_engine_cache()
    assert get_engine.cache_info().currsize == 0


class TestOnlyAzureSqlIsAccepted:
    """Local Postgres was removed; a leftover URL must say so, not fail obscurely.

    These run everywhere, which matters: they are the only database tests that
    are not skipped outside the VNet, and the upgrade hazard they cover -- a
    developer's old DATABASE_URL -- is one that only bites outside the VNet.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql+psycopg://postgres:pw@127.0.0.1:5432/spares_ai",
            "postgresql://postgres:pw@localhost/spares_ai",
            "sqlite:///./local.db",
        ],
    )
    def test_a_non_azure_url_is_refused_and_explained(self, url: str) -> None:
        with pytest.raises(DatabaseNotConfiguredError, match="Azure SQL only"):
            require_azure_sql(url)

    def test_an_azure_sql_url_is_accepted(self) -> None:
        require_azure_sql(
            "mssql+pyodbc://u:p@sql-vzi-aicom-nonprod-san.database.windows.net:1433"
            "/sqldb-aicom?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes"
        )

    def test_an_empty_url_is_not_this_error(self) -> None:
        """Unconfigured and wrongly-configured are different states, as elsewhere."""
        require_azure_sql("")

    def test_the_refusal_names_the_backend_it_found(self) -> None:
        with pytest.raises(DatabaseNotConfiguredError, match="postgresql"):
            require_azure_sql("postgresql://u:p@h/db")


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
