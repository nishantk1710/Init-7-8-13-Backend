"""Alembic environment.

The database URL is NOT read from alembic.ini. It comes from the application's
own settings (``DATABASE_URL``), so migrations and the running application can
never disagree about which database they mean, and no credential is written to
a tracked file.

``target_metadata`` is the application's metadata, so ``alembic revision
--autogenerate`` diffs the real models against the real database.
"""

import contextlib
import logging
from collections.abc import Iterator
from logging.config import fileConfig

from alembic import context
from sqlalchemy import Engine, engine_from_config, pool

from app.core.config import get_settings

# Importing the models package registers every table on Base.metadata. A model
# that is not reachable from here is invisible to autogenerate.
from app.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The URL to migrate, from application settings."""
    url = get_settings().database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set, so there is no database to migrate. "
            "Copy .env.example to .env and set it (see README, 'Database')."
        )
    return url


def _include_object(object_, name, type_, reflected, compare_to) -> bool:
    """Keep autogenerate away from tables no model owns.

    The ``raw_*`` tables are built by ``app/seed/loader.py`` from the extract
    workbooks, not declared as ORM models, so they are absent from
    ``Base.metadata``. Autogenerate reads that absence as "dropped" and emits a
    ``drop_table`` for all 27 of them -- 3.4M rows, and a half-hour reload.

    Reflecting them into metadata instead would be worse: the loader
    drop-and-creates each table per run, so their shape is the extract's to
    define, not a migration's.
    """
    if type_ == "table" and reflected and name.startswith("raw_"):
        return False
    if type_ == "index" and reflected and getattr(object_.table, "name", "").startswith("raw_"):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting -- `alembic upgrade head --sql`."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


# startup.sh runs `alembic upgrade head` on every instance when
# RUN_MIGRATIONS_ON_STARTUP is on, and nothing else serialises it: two
# instances starting together would apply the same revision twice. One SQL
# Server application lock for the whole upgrade -- the mechanism the ingest
# scheduler uses -- makes the second wait, then find the revisions applied
# and do nothing.
MIGRATION_LOCK_NAME = "spares_ai_migrations"

# How long a second migrator waits before going ahead anyway, as it did before
# the lock existed. Ten minutes: a migration here is seconds.
MIGRATION_LOCK_WAIT_MS = 600_000

_log = logging.getLogger("alembic.env")


@contextlib.contextmanager
def _one_migrator_at_a_time(connectable: Engine) -> Iterator[None]:
    """Hold the migration lock, on a connection of its own, while the body runs.

    Its own connection, so the one the migrations run on -- and its
    transaction -- is exactly what it was without the lock. A session-owned
    lock ends with the session: closing this connection (NullPool closes for
    real) releases it, and a killed process cannot leave it held. Not SQL
    Server, or no lock in time: migrate anyway.
    """
    if connectable.dialect.name != "mssql":
        yield
        return
    with connectable.connect() as lock_connection:
        try:
            granted = lock_connection.exec_driver_sql(
                "DECLARE @r int; "
                "EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', "
                "@LockOwner = 'Session', @LockTimeout = ?; SELECT @r;",
                (MIGRATION_LOCK_NAME, MIGRATION_LOCK_WAIT_MS),
            ).scalar()
            # The lock belongs to the session, not this transaction; ending the
            # transaction keeps the connection idle while the migrations run.
            lock_connection.commit()
        except Exception as exc:  # noqa: BLE001 -- the guard must never stop a migration
            _log.warning("could not take the migration lock (%s); migrating without it", exc)
            granted = None
        if granted is not None and granted < 0:
            _log.warning(
                "the migration lock is still held after %d s; migrating without it",
                MIGRATION_LOCK_WAIT_MS // 1000,
            )
        yield


def run_migrations_online() -> None:
    """Connect and run migrations against the live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with _one_migrator_at_a_time(connectable), connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this a column type change is silently missed by
            # autogenerate, and the migration lies about what it does.
            compare_type=True,
            include_object=_include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
