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
