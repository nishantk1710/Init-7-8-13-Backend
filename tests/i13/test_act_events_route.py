"""GET /api/i13/act/events -- every ACT event across all exceptions, newest
first, which is what an audit screen reads.

Unlike tests/i13/test_act_api.py this needs no database: it runs on an
in-memory SQLite database with `get_db` overridden, so it proves the response
contract, ordering, paging and the total-count header without the Azure SQL
server.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import get_db
from app.main import create_app
from app.models.base import Base
from app.models.i13_act_exception import ActExceptionEventRecord, ActExceptionRecord

BASE = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
URL = "/api/i13/act/events"


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[ActExceptionRecord.__table__, ActExceptionEventRecord.__table__])
    return sessionmaker(bind=engine)


@pytest.fixture
def client(factory):
    app = create_app()

    def _db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    # No `with`: entering the context runs the lifespan against the real database.
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed(factory, count: int) -> None:
    """`count` events, one hour apart, ACT-0 the oldest."""
    with factory() as session:
        for i in range(count):
            session.add(
                ActExceptionEventRecord(
                    exception_id=f"ACT-{i}",
                    event_type="DETECTED",
                    from_status=None,
                    to_status="OPEN",
                    actor_id=None,
                    actor_type="SYSTEM",
                    timestamp=BASE + timedelta(hours=i),
                    metadata_json=json.dumps({"n": str(i)}),
                )
            )
        session.commit()


def test_empty_feed_is_an_empty_array_with_a_zero_total(client):
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json() == []
    assert response.headers["X-Total-Count"] == "0"


def test_newest_first_and_each_row_names_its_exception(client, factory):
    _seed(factory, 3)
    body = client.get(URL).json()
    assert [row["exception_id"] for row in body] == ["ACT-2", "ACT-1", "ACT-0"]
    first = body[0]
    assert first["event_type"] == "DETECTED"
    assert first["to_status"] == "OPEN"
    assert first["actor_type"] == "SYSTEM"
    assert first["metadata"] == {"n": "2"}


def test_total_header_is_the_population_not_the_page(client, factory):
    _seed(factory, 5)
    response = client.get(URL, params={"limit": 2})
    assert len(response.json()) == 2
    assert response.headers["X-Total-Count"] == "5"


def test_paging_covers_every_event_exactly_once(client, factory):
    _seed(factory, 5)
    seen: list[str] = []
    for offset in (0, 2, 4):
        seen.extend(r["exception_id"] for r in client.get(URL, params={"limit": 2, "offset": offset}).json())
    assert seen == ["ACT-4", "ACT-3", "ACT-2", "ACT-1", "ACT-0"]


def test_from_ts_is_inclusive_and_to_ts_is_exclusive(client, factory):
    _seed(factory, 5)  # BASE+0h .. BASE+4h
    response = client.get(
        URL,
        params={
            "from_ts": (BASE + timedelta(hours=1)).isoformat(),
            "to_ts": (BASE + timedelta(hours=3)).isoformat(),
        },
    )
    assert [r["exception_id"] for r in response.json()] == ["ACT-2", "ACT-1"]
    assert response.headers["X-Total-Count"] == "2"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 501}, {"offset": -1}])
def test_out_of_range_paging_is_rejected(client, params):
    assert client.get(URL, params=params).status_code == 422


def test_the_feed_is_read_only(client):
    assert client.post(URL, json={}).status_code == 405
    assert client.delete(URL).status_code == 405


def test_events_is_not_swallowed_by_the_exception_id_routes(client, factory):
    # /act/events must not be parsed as /act/exceptions/{id}.
    _seed(factory, 1)
    assert client.get(URL).status_code == 200
