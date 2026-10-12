"""GET /api/v1/i7/approval-events -- the approval ledger across all
recommendations, which is what an audit screen reads.

Runs on an in-memory SQLite database with the session dependency overridden
(the shape tests/i7/test_pipeline_api.py uses), so it proves the response
contract and the paging/filtering behaviour without the Azure SQL server.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i7.deps import get_session
from app.main import create_app
from app.models.base import Base
from app.models.i7_recommendation import ApprovalLedgerEntry

BASE = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
URL = "/api/v1/i7/approval-events"


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ApprovalLedgerEntry.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(factory):
    app = create_app()

    def _session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    # No `with`: entering the context runs the lifespan against the real database.
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed(factory, count: int) -> None:
    """`count` ledger rows, one hour apart, REC-0 the oldest."""
    with factory() as session:
        for i in range(count):
            session.add(
                ApprovalLedgerEntry(
                    recommendation_id=f"REC-{i}",
                    recommendation_version=1,
                    actor_id=f"U{i}",
                    actor_role="End User",
                    action="APPROVE",
                    previous_status="PENDING_APPROVAL",
                    new_status="APPROVED",
                    comment=None,
                    timestamp=BASE + timedelta(hours=i),
                )
            )
        session.commit()


def test_empty_ledger_is_an_empty_page_not_an_error(client):
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_newest_first(client, factory):
    _seed(factory, 4)
    ids = [row["recommendation_id"] for row in client.get(URL).json()["items"]]
    assert ids == ["REC-3", "REC-2", "REC-1", "REC-0"]


def test_total_is_the_population_not_the_page(client, factory):
    _seed(factory, 5)
    body = client.get(URL, params={"limit": 2}).json()
    assert len(body["items"]) == 2
    assert body["total"] == 5


def test_paging_covers_every_row_exactly_once(client, factory):
    _seed(factory, 5)
    seen: list[str] = []
    for offset in (0, 2, 4):
        page = client.get(URL, params={"limit": 2, "offset": offset}).json()["items"]
        seen.extend(row["recommendation_id"] for row in page)
    assert seen == ["REC-4", "REC-3", "REC-2", "REC-1", "REC-0"]


def test_rows_with_equal_timestamps_page_deterministically(client, factory):
    with factory() as session:
        for i in range(3):
            session.add(
                ApprovalLedgerEntry(
                    recommendation_id=f"SAME-{i}",
                    recommendation_version=1,
                    actor_id="U",
                    actor_role="End User",
                    action="APPROVE",
                    previous_status="PENDING_APPROVAL",
                    new_status="APPROVED",
                    timestamp=BASE,
                )
            )
        session.commit()
    first = [r["recommendation_id"] for r in client.get(URL, params={"limit": 3}).json()["items"]]
    second = [r["recommendation_id"] for r in client.get(URL, params={"limit": 3}).json()["items"]]
    assert first == second
    assert sorted(first) == ["SAME-0", "SAME-1", "SAME-2"]


def test_from_ts_is_inclusive_and_to_ts_is_exclusive(client, factory):
    _seed(factory, 5)  # BASE+0h .. BASE+4h
    body = client.get(
        URL,
        params={
            "from_ts": (BASE + timedelta(hours=1)).isoformat(),
            "to_ts": (BASE + timedelta(hours=3)).isoformat(),
        },
    ).json()
    assert [r["recommendation_id"] for r in body["items"]] == ["REC-2", "REC-1"]
    assert body["total"] == 2


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
def test_out_of_range_paging_is_rejected(client, params):
    assert client.get(URL, params=params).status_code == 422


def test_the_feed_is_read_only(client):
    # The ledger is append-only and only the workflow writes to it.
    assert client.post(URL, json={}).status_code == 405
    assert client.delete(URL).status_code == 405


def test_route_is_not_swallowed_as_a_recommendation_id(client):
    # /approval-events must not be parsed as /recommendations/{id}.
    body = client.get(URL).json()
    assert set(body) == {"items", "total", "limit", "offset"}
