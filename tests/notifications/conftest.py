"""In-memory SQLite for the shared notification tables.

Only these tables are created: the full metadata has index names that clash on
SQLite, and nothing here needs the rest.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models.app_user import AppUser
from app.models.base import Base
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation
from app.models.notification import (
    NotificationConfigAudit,
    NotificationRecipient,
    NotificationReminderState,
    NotificationRow,
    NotificationSla,
)

NOTIFICATION_TABLES = [
    model.__table__
    for model in (
        NotificationRow,
        NotificationRecipient,
        NotificationSla,
        NotificationReminderState,
        NotificationConfigAudit,
        AppUser,
        Recommendation,
        ApprovalLedgerEntry,
    )
]


def _drop_duplicate_index_definitions() -> None:
    """``i7_recommendation`` declares some indexes twice (``index=True`` and an explicit
    ``Index``). Azure SQL is created by the migrations, so it never notices; SQLite
    refuses to create the same index twice. Keep one of each, in memory only."""
    for table in NOTIFICATION_TABLES:
        seen: set[str] = set()
        for index in sorted(table.indexes, key=lambda i: i.name or ""):
            if index.name in seen:
                table.indexes.discard(index)
            seen.add(index.name)


_drop_duplicate_index_definitions()


def sqlite_engine():
    """In-memory SQLite that handles SAVEPOINTs like a real database.

    pysqlite does not begin a transaction before a SAVEPOINT on its own, which would make
    the first SAVEPOINT commit the work around it. These two listeners are the fix the
    SQLAlchemy documentation gives. Without them a test about a failed savepoint would
    prove nothing."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _no_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection):
        connection.exec_driver_sql("BEGIN")

    return engine


@pytest.fixture
def db():
    engine = sqlite_engine()
    Base.metadata.create_all(engine, tables=NOTIFICATION_TABLES)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def api(db):
    """The real application, with its database dependency pointed at ``db``."""
    from fastapi.testclient import TestClient

    from app.core.db import get_db
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)
