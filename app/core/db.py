"""Database engine, session factory and the FastAPI session dependency.

Every database session in the application is created here. No module builds its
own engine, and no module names a driver, host or database: the single source is
``Settings.database_url`` (see ``app.core.config``).

The target is Azure SQL -- ``sqldb-aicom`` on ``sql-vzi-aicom-nonprod-san``.
Local Postgres was a stand-in while VZI's database was being provisioned and has
been removed. Note that the server has public network access disabled and is
reached through a private endpoint, so anything running outside the VNet cannot
connect at all; that is infrastructure, not a bug here, and the connect timeout
below exists so it presents as a fast failure rather than a hang.

**Everything is lazy, deliberately.** Creating the engine at import time would
make ``import app.main`` fail whenever no database is configured -- which would
break the liveness endpoint and the existing tests, both of which are specified
to work with zero configuration. Instead the engine is built on first use and
cached, so a process that never touches the database never connects to one.
"""

import os
from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# The only backend this system runs against: Azure SQL (sqldb-aicom on
# sql-vzi-aicom-nonprod-san). Local Postgres was a stand-in until VZI's database
# existed and has been removed now that it does -- so there is no second dialect
# to keep working, and no way to accidentally develop against one and deploy
# against the other.
MSSQL = "mssql"


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when database access is attempted with no DATABASE_URL set.

    A distinct type so callers can answer "not configured" differently from
    "configured but unreachable" -- the readiness endpoint reports them as
    different states, and conflating them sends people to the wrong fix.
    """


def backend_of(url: str) -> str:
    """The SQLAlchemy backend name for a URL -- ``postgresql``, ``mssql``, ...

    Returns ``""`` for anything unparseable rather than raising, so a malformed
    DATABASE_URL fails later at ``create_engine`` with SQLAlchemy's own message
    instead of a worse one from here.
    """
    try:
        return make_url(url).get_backend_name()
    except Exception:
        return ""


def require_azure_sql(url: str) -> None:
    """Refuse a DATABASE_URL that is not Azure SQL, and say why.

    Worth failing loudly on rather than letting SQLAlchemy try: a leftover
    Postgres URL in someone's environment would otherwise fail deep inside the
    seed with a driver error, and the actual problem -- that this system no
    longer has a local backend -- would not be obvious from it.

    ``ALLOW_NON_AZURE_SQL=1`` lifts this gate. It exists because Azure SQL sits
    behind a private endpoint that only the VNet can reach, so neither a GitHub
    Actions runner nor most local machines can reach it at all -- CI still needs
    *some* database to prove migrations and app code work. Default is unset, so
    the gate is on everywhere unless this is set deliberately (ci.yml, or a
    developer's own shell); it must never be set in a deployed environment.
    """
    if os.environ.get("ALLOW_NON_AZURE_SQL") == "1":
        return
    backend = backend_of(url)
    if backend and backend != MSSQL:
        raise DatabaseNotConfiguredError(
            f"DATABASE_URL names the {backend!r} backend. This system runs "
            "against Azure SQL only; local Postgres was a stand-in and has been "
            "removed. Expected mssql+pyodbc://...@sql-vzi-aicom-nonprod-san"
            ".database.windows.net:1433/sqldb-aicom?driver=ODBC+Driver+18+for+"
            "SQL+Server&Encrypt=yes. See README, 'Database'. Set "
            "ALLOW_NON_AZURE_SQL=1 to use a non-Azure database anyway (CI/local "
            "dev only -- never in a deployed environment)."
        )


@lru_cache
def get_engine() -> Engine:
    """The process-wide engine, created on first use.

    ``pool_pre_ping`` costs one cheap round trip per checkout and removes the
    stale-connection failures that otherwise appear after an idle period -- a
    container restart locally, an idle-timeout cut in a managed database.
    """
    settings = get_settings()
    if not settings.database_url:
        raise DatabaseNotConfiguredError(
            "DATABASE_URL is not set. Copy .env.example to .env and set it "
            "(see README, 'Database'). No default is assumed: a connection "
            "string must never be hard-coded."
        )
    require_azure_sql(settings.database_url)

    # Bound how long a connection attempt waits. Without this pyodbc waits on the
    # OS default, and a database that is unreachable -- which, with public
    # network access disabled on sql-vzi-aicom-nonprod-san, is what anything
    # outside the VNet sees -- makes the caller HANG with no output rather than
    # failing. Same lesson as the storage root: fail fast and say why.
    #
    # The option name is driver-specific: pyodbc's login timeout is ``timeout``,
    # psycopg's is ``connect_timeout``. Only pyodbc's name applied here before --
    # invisible while Azure SQL was the only backend anyone actually connected
    # with, but a psycopg connection (ALLOW_NON_AZURE_SQL, see require_azure_sql)
    # rejects an option it does not recognise rather than ignoring it.
    timeout_option = "timeout" if backend_of(settings.database_url) == MSSQL else "connect_timeout"
    connect_args: dict[str, object] = {
        timeout_option: settings.database_connect_timeout_seconds
    }

    return create_engine(
        settings.database_url,
        echo=settings.database_echo,
        # Azure SQL closes idle connections aggressively and the App Service can
        # sit idle between requests, so the stale-connection problem this solves
        # is more likely there than locally, not less.
        pool_pre_ping=True,
        # Recycle below Azure SQL's idle cut so a pooled connection is replaced
        # on our schedule rather than discovered dead on someone's request.
        pool_recycle=settings.database_pool_recycle_seconds,
        future=True,
        connect_args=connect_args,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    """Session factory bound to the engine. Cached for the same reason."""
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session, closed when the request ends.

    Usage::

        @router.get("/things")
        def list_things(db: Session = Depends(get_db)) -> list[Thing]:
            ...

    The session is not committed here. A route or service commits its own unit
    of work explicitly, so a read-only request never opens a write transaction
    by accident.
    """
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def check_connection() -> None:
    """Round-trip the database. Raises on failure; returns None on success.

    Used by the readiness endpoint. ``SELECT 1`` is the cheapest statement that
    proves the connection is genuinely usable rather than merely constructed.
    """
    with get_engine().connect() as connection:
        connection.execute(text("SELECT 1"))


def reset_engine_cache() -> None:
    """Drop the cached engine and session factory.

    For tests that change ``DATABASE_URL`` between cases. Production code has no
    reason to call this.
    """
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()
