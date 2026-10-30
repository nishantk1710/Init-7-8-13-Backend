"""The routes behind server-side paging (20 Oct 2026): filters as query
parameters, totals in ``X-Total-Count``, and the ``/counts`` endpoints the
dashboard's bars read instead of loading rows.

The stored snapshot is the SQLite one from ``test_server_paging``; the ACT
queue is its own SQLite table. No Azure SQL needed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i13.deps import snapshot_or_live
from app.core.config import get_settings
from app.core.db import get_db
from app.main import create_app
from app.models.base import Base
from app.models.i13_act_exception import ActExceptionRecord
from tests.i13.test_server_paging import stored  # noqa: F401 -- the fixture

API = f"{get_settings().api_prefix}/i13"


@pytest.fixture
def client(stored, monkeypatch):  # noqa: F811
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ActExceptionRecord.__table__])
    factory = sessionmaker(bind=engine)
    with factory() as session:
        for i, (status, owner) in enumerate(
            [("OPEN", None), ("OPEN", "u1"), ("AWAITING_REQUESTER", "u2"), ("RESOLVED", None)]
        ):
            session.add(
                ActExceptionRecord(
                    exception_id=f"ACT-{i}", exception_type="NO_PLAN", status=status, material="M1",
                    plant="1300", owner_requester_id=owner, detected_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                    reason="r", evidence_json="{}",
                )
            )
        session.commit()

    def _db():
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[snapshot_or_live] = lambda: stored
    monkeypatch.setattr("app.initiatives.i13.snapshot.served_info", lambda: None)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_ledger_filters_and_total_header(client):
    response = client.get(f"{API}/ledger", params={"procurement_status": "RECEIVED", "limit": 1})
    assert response.status_code == 200, response.text
    assert response.headers["X-Total-Count"] == "2"
    assert [r["ledger_id"] for r in response.json()] == ["P2"]


def test_ledger_counts_route_is_not_taken_for_a_ledger_id(client):
    response = client.get(f"{API}/ledger/counts", params={"delivery_status": "NO_PO"})
    assert response.status_code == 200, response.text
    assert response.json() == {"total": 1, "by_linkage_status": {"UNMATCHED": 1}}


def test_utilisation_defaults_to_most_recent_activity(client):
    response = client.get(f"{API}/act/utilisation", params={"limit": 2})
    assert response.status_code == 200, response.text
    assert response.headers["X-Total-Count"] == "4"
    assert [r["material"] for r in response.json()] == ["W3", "W4"]
    by_number = client.get(f"{API}/act/utilisation", params={"limit": 2, "sort": "material"})
    assert [r["material"] for r in by_number.json()] == ["W1", "W2"]


def test_utilisation_counts(client):
    response = client.get(f"{API}/act/utilisation/counts", params={"aging_band": "NON_MOVING"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2 and body["by_aging_band"] == {"NON_MOVING": 2}


def test_exception_counts_cover_the_whole_queue(client):
    response = client.get(f"{API}/act/exceptions/counts")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "total": 4,
        "by_status": {"OPEN": 2, "AWAITING_REQUESTER": 1, "RESOLVED": 1},
        "with_requester": 2,
    }


def _work_po(stored, rows):  # noqa: F811
    from sqlalchemy import text

    stored.db.execute(text("CREATE TABLE i13_w1_po (purchasing_document TEXT, item TEXT, order_quantity TEXT)"))
    for row in rows:
        stored.db.execute(text("INSERT INTO i13_w1_po VALUES (:d, :i, :q)"), dict(zip("diq", row)))


def test_a_ledger_line_carries_what_its_po_line_ordered(client, stored):  # noqa: F811
    """UAT, 29 Oct: "Open PO 6" on the assistant and "Not delivered" on the
    ledger, with no 6 anywhere on the ledger. The line now says it: 0 of 6."""
    _work_po(stored, [("POP1", "10", "6.000"), ("POP1", "20", "9.000")])
    response = client.get(f"{API}/ledger", params={"delivery_status": "NOT_DELIVERED"})
    assert response.status_code == 200, response.text
    assert [(r["ledger_id"], r["ordered_quantity"]) for r in response.json()] == [("P1", "6.000")]

    detail = client.get(f"{API}/ledger/P1").json()
    assert detail["ordered_quantity"] == "6.000"
    # No PO line, nothing ordered to show.
    assert client.get(f"{API}/ledger/P3").json()["ordered_quantity"] is None


def test_the_ledger_still_answers_without_the_work_table(client):
    """The quantity is a nicety: a version whose work table cannot be read
    still serves its lines, without it."""
    response = client.get(f"{API}/ledger", params={"delivery_status": "NOT_DELIVERED"})
    assert response.status_code == 200, response.text
    assert [r["ordered_quantity"] for r in response.json()] == [None]


def test_the_in_memory_snapshot_answers_the_same_ordered_quantity() -> None:
    """The stored and in-memory snapshots must answer every route alike
    (test_snapshot_store); the in-memory one reads its procurement chain."""
    from decimal import Decimal
    from types import SimpleNamespace

    from app.initiatives.i13.snapshot import I13Snapshot

    chain = (
        SimpleNamespace(po_number="POP1", po_item="10", ordered_quantity=Decimal("6.000")),
        SimpleNamespace(po_number=None, po_item=None, ordered_quantity=Decimal("2")),
        SimpleNamespace(po_number="POP2", po_item="10", ordered_quantity=None),
    )
    snapshot = SimpleNamespace(procurement_chain=chain)
    found = I13Snapshot.ordered_quantities_for.__get__(snapshot)  # the method, on a stand-in
    snapshot._ordered_by_po_line = I13Snapshot._ordered_by_po_line.func(snapshot)
    assert found([("POP1", "10"), ("POP2", "10"), ("POX", "10")]) == {("POP1", "10"): Decimal("6.000")}
