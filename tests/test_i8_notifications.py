"""FR-11 notifications: one logged per exception, the first time it opens.

Run against in-memory SQLite, like the exception-state tests: the table is
ordinary and portable, so SQLite exercises the real logic on a laptop that
cannot reach Azure SQL.

What is worth pinning:

* re-reading the queue logs nothing new (one row per exception id, ever)
* only the configured types are logged, and only actionable items -- an item
  raised before its control existed is history, not news
* a database failure (the table not migrated yet) never breaks the queue
* the route pages newest first and filters by type
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i8.router import attestation_view_dependency, snapshot_dependency
from app.core.db import get_db
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.exceptions import ExceptionItem, ExceptionStats
from app.initiatives.i8.models import ExceptionEvent, Notification
from app.initiatives.i8.notifications import log_new

NOTIFICATIONS = "/api/i8/notifications"
EXCEPTIONS = "/api/i8/exceptions"


def item(n: int, type: str = "MISSING_ATTESTATION", **overrides) -> ExceptionItem:
    document = f"45000000{n:02d}"
    fields = dict(
        id=f"EX-{type}-{document}-10",
        type=type,
        severity="warning",
        material_id=f"80000000{n:02d}",
        description=f"Pump seal {n}",
        plant="1300",
        purchasing_document=document,
        item="10",
        title="No condition declaration",
        detail="No attestation within 30 days.",
        raised_at=date(2026, 9, 1),
    )
    fields.update(overrides)
    return ExceptionItem(**fields)


@pytest.fixture
def cfg():
    return get_i8_settings().model_copy(
        update={"notification_types": "MISSING_ATTESTATION,OVERDUE_REPAIR"}
    )


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ExceptionEvent.__table__.create(engine)
    Notification.__table__.create(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def db(factory):
    with factory() as session:
        yield session


def count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Notification))


class TestLogNew:
    def test_logs_each_new_item_once(self, db, cfg) -> None:
        items = (item(1), item(2))
        assert log_new(db, items, cfg) == 2
        # The second read finds the same items: nothing new to say.
        assert log_new(db, items, cfg) == 0
        assert count(db) == 2

    def test_logs_only_the_item_that_is_new(self, db, cfg) -> None:
        log_new(db, (item(1),), cfg)
        assert log_new(db, (item(1), item(2)), cfg) == 1
        assert set(db.scalars(select(Notification.exception_id))) == {
            item(1).id,
            item(2).id,
        }

    def test_copies_the_item_as_found(self, db, cfg) -> None:
        log_new(db, (item(3, plant=None),), cfg)
        row = db.scalars(select(Notification)).one()
        assert (row.exception_type, row.severity, row.title, row.material_id) == (
            "MISSING_ATTESTATION",
            "warning",
            "No condition declaration",
            "8000000003",
        )
        assert row.plant is None
        assert row.channel == "EMAIL"
        assert row.emailed_at is None
        assert row.logged_at is not None

    def test_skips_a_type_not_configured(self, db, cfg) -> None:
        assert log_new(db, (item(1, type="MISSING_SESSION_ID"),), cfg) == 0
        assert count(db) == 0

    def test_blank_setting_logs_nothing(self, db, cfg) -> None:
        off = cfg.model_copy(update={"notification_types": ""})
        assert log_new(db, (item(1),), off) == 0

    def test_skips_an_item_raised_before_its_control(self, db, cfg) -> None:
        assert log_new(db, (item(1, pre_automation=True),), cfg) == 0
        assert count(db) == 0

    def test_a_duplicate_id_in_one_read_is_one_row(self, db, cfg) -> None:
        assert log_new(db, (item(1), item(1)), cfg) == 1

    def test_swallows_a_missing_table(self, cfg) -> None:
        engine = create_engine("sqlite://")
        with Session(engine) as session:
            # No table: the migration has not run yet. The queue must load.
            assert log_new(session, (item(1),), cfg) == 0

    def test_default_types(self) -> None:
        assert get_i8_settings().model_copy(
            update={
                "notification_types": (
                    "OVERDUE_REPAIR,MISSING_ATTESTATION,"
                    "MISSING_SESSION_ID,UNJUSTIFIED_ACQUISITION"
                )
            }
        ).notification_type_list == (
            "OVERDUE_REPAIR",
            "MISSING_ATTESTATION",
            "MISSING_SESSION_ID",
            "UNJUSTIFIED_ACQUISITION",
        )


def stats() -> ExceptionStats:
    return ExceptionStats(
        total=0,
        by_type={},
        by_severity={},
        lines_checked=0,
        lines_covered=0,
        attestation_window_days=30,
        pre_automation=0,
        actionable=0,
        acquisitions_checked=0,
        justification_window_days=14,
    )


@pytest.fixture
def client(factory, cfg):
    from app.main import create_app

    app = create_app()
    state = SimpleNamespace(exceptions=())

    def _db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_i8_settings] = lambda: cfg
    app.dependency_overrides[attestation_view_dependency] = lambda: SimpleNamespace(
        exceptions=state.exceptions, exception_stats=stats()
    )
    app.dependency_overrides[snapshot_dependency] = lambda: SimpleNamespace(
        reference_date=date(2026, 10, 27)
    )
    # No `with`: entering the context runs the lifespan against the real database.
    state.client = TestClient(app)
    yield state
    app.dependency_overrides.clear()


class TestRoute:
    def test_reading_the_queue_logs_the_new_items(self, client) -> None:
        client.exceptions = (
            item(1),
            item(2, type="OVERDUE_REPAIR"),
            item(3, pre_automation=True),
        )
        assert client.client.get(EXCEPTIONS).status_code == 200
        assert client.client.get(EXCEPTIONS).status_code == 200
        body = client.client.get(NOTIFICATIONS).json()
        assert body["total"] == 2
        assert body["types"] == ["MISSING_ATTESTATION", "OVERDUE_REPAIR"]
        first = body["items"][0]
        assert set(first) >= {
            "id", "exceptionId", "type", "severity", "title", "materialId",
            "plant", "loggedAt", "channel", "emailed",
        }
        assert first["channel"] == "EMAIL"
        assert first["emailed"] is False

    def test_pages_newest_first_and_filters_by_type(self, client, factory) -> None:
        with factory() as session:
            for n in range(5):
                session.add(
                    Notification(
                        exception_id=f"EX-{n}",
                        exception_type="OVERDUE_REPAIR" if n % 2 else "MISSING_ATTESTATION",
                        severity="warning",
                        title="t",
                        material_id=f"800{n}",
                        plant="1300",
                        logged_at=datetime(2026, 10, 20 + n, tzinfo=timezone.utc),
                    )
                )
            session.commit()

        page = client.client.get(NOTIFICATIONS, params={"page": 1, "pageSize": 2}).json()
        assert [i["exceptionId"] for i in page["items"]] == ["EX-4", "EX-3"]
        assert (page["total"], page["page"], page["pageSize"]) == (5, 1, 2)
        page3 = client.client.get(NOTIFICATIONS, params={"page": 3, "pageSize": 2}).json()
        assert [i["exceptionId"] for i in page3["items"]] == ["EX-0"]

        overdue = client.client.get(NOTIFICATIONS, params={"type": "overdue_repair"}).json()
        assert [i["exceptionId"] for i in overdue["items"]] == ["EX-3", "EX-1"]
        assert overdue["total"] == 2
        # Every type logged, not only the filtered one: the dropdown's values.
        assert overdue["types"] == ["MISSING_ATTESTATION", "OVERDUE_REPAIR"]

    def test_page_size_defaults_to_25_and_is_capped(self, client) -> None:
        assert client.client.get(NOTIFICATIONS).json()["pageSize"] == 25
        capped = client.client.get(NOTIFICATIONS, params={"pageSize": 10_000}).json()
        assert capped["pageSize"] == 500


class TestLabels:
    """28-Oct-2026: the log names the repair line, the material description
    and the plant name, so rows of the same type can be told apart. Looked up
    when the log is read; nothing new is stored."""

    @pytest.fixture
    def labels(self, client, factory, monkeypatch):
        from app.api.i8 import router as i8_router

        state = SimpleNamespace(view=None, names={})
        monkeypatch.setattr(i8_router, "cached_attestation_view", lambda: state.view)
        monkeypatch.setattr(i8_router, "cached_snapshot", lambda: None)
        monkeypatch.setattr(
            i8_router, "material_names", lambda materials: {m: state.names[m] for m in materials if m in state.names}
        )
        state.client = client.client
        state.factory = factory
        return state

    def _log(self, factory, *rows: tuple[str, str, str, str | None]) -> None:
        with factory() as session:
            for n, (exception_id, type_, material, plant) in enumerate(rows):
                session.add(
                    Notification(
                        exception_id=exception_id,
                        exception_type=type_,
                        severity="warning",
                        title="t",
                        material_id=material,
                        plant=plant,
                        logged_at=datetime(2026, 10, 1 + n, tzinfo=timezone.utc),
                    )
                )
            session.commit()

    def _items(self, labels) -> dict[str, dict]:
        body = labels.client.get(NOTIFICATIONS).json()
        return {i["exceptionId"]: i for i in body["items"]}

    def test_the_repair_line_is_read_back_from_the_id(self, labels) -> None:
        self._log(
            labels.factory,
            ("EX-OVERDUE_REPAIR-4500001719-10", "OVERDUE_REPAIR", "8000000001", "1300"),
            ("EX-MISSING_ATTESTATION-4500001720-20", "MISSING_ATTESTATION", "8000000002", "1500"),
            ("EX-REPAIR_PR_NOT_DECLARED-PR-2000028756-10", "REPAIR_PR_NOT_DECLARED", "8000000003", "1300"),
            ("EX-UNJUSTIFIED_ACQUISITION-PR-1000201201-10", "UNJUSTIFIED_ACQUISITION", "8000000004", "1300"),
            ("EX-MISSING_SESSION_ID-R-1916164-1", "MISSING_SESSION_ID", "8000000005", "9999"),
        )

        items = self._items(labels)

        assert items["EX-OVERDUE_REPAIR-4500001719-10"]["repairLine"] == "4500001719-10"
        assert items["EX-MISSING_ATTESTATION-4500001720-20"]["repairLine"] == "4500001720-20"
        assert items["EX-REPAIR_PR_NOT_DECLARED-PR-2000028756-10"]["repairLine"] == "PR-2000028756-10"
        # A bought-new id names the purchase, not the repair; no queue in memory.
        assert items["EX-UNJUSTIFIED_ACQUISITION-PR-1000201201-10"]["repairLine"] is None
        assert items["EX-MISSING_SESSION_ID-R-1916164-1"]["repairLine"] is None
        # Plant names: documented codes only.
        assert items["EX-OVERDUE_REPAIR-4500001719-10"]["plantName"] == get_i8_settings().plant_name_map["1300"]
        assert items["EX-MISSING_ATTESTATION-4500001720-20"]["plantName"] == get_i8_settings().plant_name_map["1500"]
        assert items["EX-MISSING_SESSION_ID-R-1916164-1"]["plantName"] is None

    def test_the_current_queue_names_a_bought_new_findings_repair(self, labels) -> None:
        current = item(
            4,
            type="UNJUSTIFIED_ACQUISITION",
            id="EX-UNJUSTIFIED_ACQUISITION-PR-1000201201-10",
            purchasing_document="2000029101",
            item="10",
            repair_document_type="PR",
            description="PUMP SEAL KIT",
        )
        labels.view = SimpleNamespace(exceptions=(current,))
        self._log(labels.factory, (current.id, current.type, current.material_id, "1300"))

        [row] = self._items(labels).values()

        assert row["repairLine"] == "PR-2000029101-10"
        # No name in the material list: the finding's own description.
        assert row["description"] == "PUMP SEAL KIT"

    def test_the_material_name_comes_first(self, labels) -> None:
        labels.names = {"8000000001": "SEAL, MECHANICAL"}
        labels.view = SimpleNamespace(exceptions=(item(1, type="OVERDUE_REPAIR", id="EX-OVERDUE_REPAIR-4500000001-10"),))
        self._log(
            labels.factory,
            ("EX-OVERDUE_REPAIR-4500000001-10", "OVERDUE_REPAIR", "8000000001", "1300"),
            ("EX-OVERDUE_REPAIR-4500000009-10", "OVERDUE_REPAIR", "8000000009", "1300"),
        )

        items = self._items(labels)

        assert items["EX-OVERDUE_REPAIR-4500000001-10"]["description"] == "SEAL, MECHANICAL"
        assert items["EX-OVERDUE_REPAIR-4500000009-10"]["description"] is None
