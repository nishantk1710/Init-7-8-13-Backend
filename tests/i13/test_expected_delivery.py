"""Expected delivery date (EKET-EINDT) per PO line, for the utilisation ledger.

``load_all`` runs its real query against in-memory SQLite. The request side is
checked for the property that matters: it never waits on the database, and
rows get their PO line's date once the background load has landed.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i13 import expected_delivery as ed

D = Decimal


class TestNextExpected:
    def test_one_schedule_line_is_its_date(self) -> None:
        assert ed.next_expected([(date(2026, 10, 25), D(10), D(0))]) == date(2026, 10, 25)

    def test_the_first_line_not_fully_delivered(self) -> None:
        lines = [
            (date(2026, 10, 1), D(5), D(5)),  # delivered
            (date(2026, 11, 15), D(5), D(0)),  # still to come
            (date(2026, 10, 20), D(5), D(2)),  # partly delivered -- earlier, so next
        ]
        assert ed.next_expected(lines) == date(2026, 10, 20)

    def test_all_delivered_is_the_last_date(self) -> None:
        lines = [(date(2026, 10, 1), D(5), D(5)), (date(2026, 10, 9), D(5), D(5))]
        assert ed.next_expected(lines) == date(2026, 10, 9)

    def test_lines_without_a_date_are_ignored(self) -> None:
        assert ed.next_expected([(None, D(5), D(0))]) is None
        assert ed.next_expected([]) is None


def _engine():
    # One shared connection: an in-memory database is private to the
    # connection that made it, and the load runs on another thread.
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE n_eket (purchasing_document TEXT, item TEXT, delivery_date TEXT, "
                "scheduled_quantity TEXT, qty_delivered TEXT)"
            )
        )
        c.execute(
            text("INSERT INTO n_eket VALUES (:po, :item, :d, :m, :w)"),
            [
                {"po": "4100001227", "item": "20", "d": "2026-10-25", "m": "10", "w": "0"},
                {"po": "4100001300", "item": "10", "d": "2026-10-01", "m": "4", "w": "4"},
                {"po": "4100001300", "item": "10", "d": "2026-11-05", "m": "6", "w": "1"},
                {"po": "4100001400", "item": "10", "d": "", "m": "1", "w": "0"},
                {"po": "", "item": "10", "d": "2026-10-25", "m": "1", "w": "0"},
            ],
        )
    return engine


def test_load_all_reads_every_scheduled_po_line():
    found, complete = ed.load_all(Session(_engine()))
    assert complete is True
    assert found == {("4100001227", "20"): date(2026, 10, 25), ("4100001300", "10"): date(2026, 11, 5)}


def test_a_missing_eket_is_reported_not_raised():
    found, complete = ed.load_all(Session(create_engine("sqlite://")))
    assert (found, complete) == ({}, False)


@pytest.fixture
def database(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: sessionmaker(bind=engine))
    ed.clear_cache()
    yield engine
    ed.clear_cache()


def test_a_request_never_waits_for_the_load(monkeypatch):
    ed.clear_cache()
    started = []
    monkeypatch.setattr(ed, "_ensure_loading", lambda: started.append(True))
    assert ed.expected_dates([("4100001227", "20")]) == {}
    assert started == [True]


def test_rows_get_their_po_lines_date_once_loaded(database):
    ed.start_loading()
    ed.wait_for_load(5)
    rows = [
        SimpleNamespace(po_number="4100001227", po_item="20"),
        SimpleNamespace(po_number=" 4100001300 ", po_item="10"),
        SimpleNamespace(po_number="4100009999", po_item="10"),  # no EKET row
        SimpleNamespace(po_number=None, po_item=None),  # PR only
    ]
    ed.attach_expected_delivery(rows)
    assert [r.expected_delivery_date for r in rows] == [date(2026, 10, 25), date(2026, 11, 5), None, None]


def test_the_route_decorator_fills_both_ledger_shapes(database):
    from app.api.i13.deps import with_expected_delivery

    ed.start_loading()
    ed.wait_for_load(5)

    @with_expected_delivery()
    def one():
        return SimpleNamespace(po_number="4100001227", po_item="20")

    @with_expected_delivery()
    def many():
        return [SimpleNamespace(po_number="4100001300", po_item="10")]

    assert one().expected_delivery_date == date(2026, 10, 25)
    assert many()[0].expected_delivery_date == date(2026, 11, 5)
