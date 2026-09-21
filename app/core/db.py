"""Database engine, session factory and the FastAPI session dependency.

Every database session in the application is created here. No module builds its
own engine, and no module names a driver, host or database: the single source is
``Settings.database_url`` (see ``app.core.config``).

Two backends are genuinely supported, chosen by ``DATABASE_URL``'s own scheme,
never by a separate flag:

  * **Postgres** -- the local Docker container (``compose.yaml``), already
    seeded with the real SAP extract every I13 endpoint reads
    (``raw_eban``/``raw_ekpo``/``raw_mseg``/``raw_resb``/... -- see
    ``app/seed/manifest.py``). This is the data source every I13 test and
    real-data validation in this codebase has actually been run against.
  * **Azure SQL** -- ``sqldb-aicom`` on ``sql-vzi-aicom-nonprod-san``, the
    target deployed environment. Its network sits behind a private endpoint,
    so anything outside the VNet cannot reach it at all; that is
    infrastructure, not a bug here, and the connect timeout below exists so
    it presents as a fast failure rather than a hang.

A previous version of this module accepted Azure SQL only, on the premise
that local Postgres was purely a stand-in to be retired once Azure SQL was
provisioned. That has not happened yet -- no I13 data is known to exist in
Azure SQL -- while the real seeded Postgres data is what every endpoint
needs to actually serve. Restricting to one backend prematurely made the
whole API unusable with the only data source that currently has data in it,
so both are accepted again; whichever ``DATABASE_URL`` names is the one used,
with connection options appropriate to that driver (see ``get_engine``).

**Everything is lazy, deliberately.** Creating the engine at import time would
make ``import app.main`` fail whenever no database is configured -- which would
break the liveness endpoint and the existing tests, both of which are specified
to work with zero configuration. Instead the engine is built on first use and
cached, so a process that never touches the database never connects to one.
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

MSSQL = "mssql"
POSTGRESQL = "postgresql"
SUPPORTED_BACKENDS = frozenset({MSSQL, POSTGRESQL})


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


def require_supported_backend(url: str) -> None:
    """Refuse a DATABASE_URL that names neither supported backend, and say why.

    Worth failing loudly on rather than letting SQLAlchemy try: a URL for an
    untested dialect (sqlite, mysql, ...) would otherwise fail deep inside a
    query with a driver error, and the actual problem -- that this system
    only knows how to run against Postgres or Azure SQL -- would not be
    obvious from it.
    """
    backend = backend_of(url)
    if backend and backend not in SUPPORTED_BACKENDS:
        raise DatabaseNotConfiguredError(
            f"DATABASE_URL names the {backend!r} backend. This system supports "
            "Postgres (local development, e.g. postgresql+psycopg://postgres:"
            "<password>@127.0.0.1:5432/spares_ai) or Azure SQL (the deployed "
            "target, e.g. mssql+pyodbc://...@sql-vzi-aicom-nonprod-san"
            ".database.windows.net:1433/sqldb-aicom?driver=ODBC+Driver+18+for+"
            "SQL+Server&Encrypt=yes). See README, 'Database'."
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
    require_supported_backend(settings.database_url)

    # Bound how long a connection attempt waits -- the connect-timeout keyword
    # itself is driver-specific, so it has to be chosen per backend. Without
    # this, a database that is unreachable (no local Postgres container
    # running; outside the VNet for Azure SQL, which has public network
    # access disabled) makes the caller HANG on the OS default rather than
    # failing fast and saying why.
    backend = backend_of(settings.database_url)
    if backend == MSSQL:
        # pyodbc's login timeout.
        connect_args: dict[str, object] = {"timeout": settings.database_connect_timeout_seconds}
    elif backend == POSTGRESQL:
        # psycopg's connect timeout -- a different keyword, not "timeout".
        connect_args = {"connect_timeout": settings.database_connect_timeout_seconds}
    else:
        connect_args = {}

    return create_engine(
        settings.database_url,
        echo=settings.database_echo,
        # Azure SQL closes idle connections aggressively and the App Service can
        # sit idle between requests, so the stale-connection problem this solves
        # is more likely there than locally -- but pre-ping is cheap enough to
        # keep on for Postgres too (e.g. after a container restart).
        pool_pre_ping=True,
        # Recycle below Azure SQL's idle cut so a pooled connection is replaced
        # on our schedule rather than discovered dead on someone's request.
        # Harmless for Postgres, which has no comparably aggressive idle cut.
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
