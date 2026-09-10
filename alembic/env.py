"""Alembic environment.

The database URL is NOT read from alembic.ini. It comes from the application's
own settings (``DATABASE_URL``), so migrations and the running application can
never disagree about which database they mean, and no credential is written to
a tracked file.

``target_metadata`` is the application's metadata, so ``alembic revision
--autogenerate`` diffs the real models against the real database.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

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


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting -- `alembic upgrade head --sql`."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations against the live database."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this a column type change is silently missed by
            # autogenerate, and the migration lies about what it does.
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
