"""Database engine, session factory and the FastAPI session dependency.

Every database session in the application is created here. No module builds its
own engine, and no module names a driver, host or database: the single source is
``Settings.database_url`` (see ``app.core.config``), so moving between local
Postgres and the deployed database is a config change.

**Everything is lazy, deliberately.** Creating the engine at import time would
make ``import app.main`` fail whenever no database is configured -- which would
break the liveness endpoint and the existing tests, both of which are specified
to work with zero configuration. Instead the engine is built on first use and
cached, so a process that never touches the database never connects to one.
"""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    """Raised when database access is attempted with no DATABASE_URL set.

    A distinct type so callers can answer "not configured" differently from
    "configured but unreachable" -- the readiness endpoint reports them as
    different states, and conflating them sends people to the wrong fix.
    """


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
    return create_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        future=True,
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
