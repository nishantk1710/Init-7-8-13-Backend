"""``GET /api/justifications`` -- paging, the real total, search and facets.

I08's justification screen pages on the server, so it needs three things the
endpoint did not give: a ``total`` that counts every match rather than the page,
an ``offset`` to reach the rest, and the plants and reasons that exist so its
dropdowns are not built from one page. Search and the reason filter moved here
from the browser for the same reason.

Run against in-memory SQLite, the pattern ``test_requests.py`` uses, so it needs
no Azure database.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.assistant.models import AssistantSession, Justification
from app.core.db import get_db
from app.models.base import Base

URL = "/api/justifications"
T0 = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def record(n: int, **overrides) -> Justification:
    """A NEW_ACQUISITION justification, ``n`` minutes before ``T0``."""
    fields = dict(
        id=f"J{n:04d}",
        session_id=None,
        exception_id=None,
        kind="NEW_ACQUISITION",
        reason_category="URGENT_BREAKDOWN",
        free_text=f"reason number {n}",
        material_id="8000004596",
        plant="1300",
        author="requester-1",
        recorded_at=T0 - timedelta(minutes=n),
    )
    fields.update(overrides)
    return Justification(**fields)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[AssistantSession.__table__, Justification.__table__]
    )
    return sessionmaker(bind=engine)


@pytest.fixture
def client(factory):
    from app.main import create_app

    app = create_app()

    def _db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    client = TestClient(app)  # no `with`: the lifespan would reach the real database
    yield client
    app.dependency_overrides.clear()


def seed(factory, *rows: Justification) -> None:
    with factory() as session:
        session.add_all(rows)
        session.commit()


def ids(body: dict) -> list[str]:
    return [item["id"] for item in body["items"]]


class TestTotal:
    def test_counts_every_match_not_the_page(self, client, factory):
        seed(factory, *(record(n) for n in range(30)))

        body = client.get(URL, params={"limit": 25}).json()

        assert len(body["items"]) == 25
        assert body["total"] == 30

    def test_counts_only_what_the_filters_match(self, client, factory):
        seed(
            factory,
            *(record(n) for n in range(4)),
            *(record(n, plant="1500") for n in range(4, 7)),
        )

        body = client.get(URL, params={"plant": "1500", "limit": 1}).json()

        assert body["total"] == 3
        assert len(body["items"]) == 1


class TestPaging:
    def test_pages_cover_every_row_once_even_on_tied_timestamps(self, client, factory):
        # Twelve records, four sharing each timestamp: without a tie-breaker the
        # database may return a tied group in a different order per query, and
        # one row would show on two pages while another showed on none.
        seed(
            factory,
            *(record(n, recorded_at=T0 - timedelta(minutes=n // 4)) for n in range(12)),
        )

        seen: list[str] = []
        for offset in range(0, 12, 5):
            seen += ids(client.get(URL, params={"limit": 5, "offset": offset}).json())

        assert sorted(seen) == sorted(f"J{n:04d}" for n in range(12))
        assert len(seen) == len(set(seen))

    def test_newest_first_then_id(self, client, factory):
        seed(
            factory,
            record(1, id="JA"),
            record(1, id="JB"),
            record(0, id="JC"),
        )

        assert ids(client.get(URL).json()) == ["JC", "JB", "JA"]

    def test_offset_past_the_end_is_empty_with_the_real_total(self, client, factory):
        seed(factory, *(record(n) for n in range(3)))

        body = client.get(URL, params={"offset": 25, "limit": 25}).json()

        assert body["items"] == []
        assert body["total"] == 3

    def test_no_paging_params_behaves_as_before(self, client, factory):
        # The I13 log and the sessions page pass neither offset nor a search,
        # and must get exactly what they got before: the newest, up to limit.
        seed(factory, *(record(n) for n in range(5)))

        body = client.get(URL, params={"limit": 3}).json()

        assert ids(body) == ["J0000", "J0001", "J0002"]


class TestSearch:
    @pytest.fixture(autouse=True)
    def _rows(self, factory):
        seed(
            factory,
            record(1, material_id="8000000007"),
            record(2, free_text="Pump seized during shutdown"),
            record(3, author="Johan Oberholzer"),
            record(4, session_id="SY8VYQZ4RM"),
            record(5, reason_category="NO_SUITABLE_REPAIRABLE"),
            record(6, free_text="only 100% new will do"),
            record(7, free_text="100 units"),
        )

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("8000000007", ["J0001"]),
            ("SEIZED", ["J0002"]),
            ("oberholzer", ["J0003"]),
            ("sy8vyqz4rm", ["J0004"]),
            ("no_suitable", ["J0005"]),
        ],
    )
    def test_each_field_a_card_shows(self, client, q, expected):
        assert ids(client.get(URL, params={"q": q}).json()) == expected

    def test_a_space_matches_the_underscore_in_a_reason(self, client):
        # The card prints "NO SUITABLE REPAIRABLE", so that is what gets typed.
        assert ids(client.get(URL, params={"q": "suitable repairable"}).json()) == ["J0005"]

    def test_like_wildcards_are_literal(self, client):
        assert ids(client.get(URL, params={"q": "100%"}).json()) == ["J0006"]

    def test_blank_search_is_no_search(self, client):
        assert client.get(URL, params={"q": "   "}).json()["total"] == 7


class TestReasonFilter:
    def test_exact_match(self, client, factory):
        seed(
            factory,
            record(1),
            record(2, reason_category="NO_SUITABLE_REPAIRABLE"),
        )

        body = client.get(URL, params={"reason_category": "NO_SUITABLE_REPAIRABLE"}).json()

        assert ids(body) == ["J0002"]
        assert body["total"] == 1


class TestFacets:
    def test_plants_and_reasons_present_among_the_matches(self, client, factory):
        seed(
            factory,
            record(1, plant="1500"),
            record(2, reason_category="NO_SUITABLE_REPAIRABLE"),
            record(3, kind="QUANTITY_OVERRIDE", plant="9999", reason_category="OTHER"),
        )

        facets = client.get(URL, params={"kind": "NEW_ACQUISITION", "limit": 1}).json()["facets"]

        # Over every match, not the one row returned, and none of the other kind.
        assert facets == {
            "plants": ["1300", "1500"],
            "reasonCategories": ["NO_SUITABLE_REPAIRABLE", "URGENT_BREAKDOWN"],
        }

    def test_empty_table(self, client):
        body = client.get(URL).json()

        assert body["total"] == 0
        assert body["facets"] == {"plants": [], "reasonCategories": []}


# --- 28-Oct-2026: who asked, and which purchase the reason answers ----------


def session(sid: str, requested_for: str | None) -> AssistantSession:
    return AssistantSession(
        id=sid,
        flow="i08",
        material_id="8000004596",
        plant="1300",
        requested_for=requested_for,
        eighty_series=True,
        material_scope="EXCLUDED",
        routing_reason="80-series",
        requester="UNAUTHENTICATED-FRONTEND",
        origin="PLATFORM",
        issued_at=T0,
        expires_at=T0 + timedelta(days=1),
        assessment="{}",
    )


def bought(document: str, raised: date, *, document_type: str = "PR", material="8000004596", plant="1300"):
    from decimal import Decimal

    from app.initiatives.i8.acquisitions import NewAcquisition

    return NewAcquisition(
        purchasing_document=document,
        item="10",
        material_id=material,
        plant=plant,
        description="Pump",
        ordered_qty=Decimal(1),
        raised_at=raised,
        document_type=document_type,
    )


@pytest.fixture
def snapshot(monkeypatch):
    """The in-memory I08 snapshot the list reads its purchases from."""
    from types import SimpleNamespace

    from app.initiatives.i8 import service

    held = SimpleNamespace(acquisitions=())
    monkeypatch.setattr(service, "cached_snapshot", lambda: held)
    return held


class TestRequesterName:
    def test_is_the_name_typed_on_the_session(self, client, factory, snapshot):
        seed(
            factory,
            session("S1", "J Miller"),
            session("S2", None),
            record(1, session_id="S1", author="UNAUTHENTICATED-FRONTEND"),
            record(2, session_id="S2"),
            record(3),
        )

        items = {i["id"]: i for i in client.get(URL).json()["items"]}

        assert items["J0001"]["requesterName"] == "J Miller"
        assert items["J0001"]["author"] == "UNAUTHENTICATED-FRONTEND"  # unchanged
        assert items["J0002"]["requesterName"] is None
        assert items["J0003"]["requesterName"] is None


class TestPurchase:
    def test_the_purchase_nearest_the_reason_within_the_window(self, client, factory, snapshot):
        on = T0.date()
        snapshot.acquisitions = (
            bought("1000200001", on + timedelta(days=20)),
            bought("1000200002", on + timedelta(days=3)),
            bought("1000200003", on - timedelta(days=3)),  # as near, but before the reason
            bought("1000200004", on + timedelta(days=60)),  # outside the window
        )
        seed(factory, record(0))

        body = client.get(URL).json()

        assert body["purchasesChecked"] is True
        assert body["items"][0]["purchase"] == {
            "type": "PR",
            "documentNumber": "1000200002",
            "line": "10",
            "raisedAt": (on + timedelta(days=3)).isoformat(),
        }

    def test_a_reason_naming_a_finding_answers_that_purchase(self, client, factory, snapshot):
        snapshot.acquisitions = (
            bought("4100100009", date(2025, 1, 1), document_type="PO"),
            bought("1000200002", T0.date()),
        )
        seed(factory, record(0, exception_id="EX-UNJUSTIFIED_ACQUISITION-4100100009-10"))

        [item] = client.get(URL).json()["items"]

        assert (item["purchase"]["type"], item["purchase"]["documentNumber"]) == ("PO", "4100100009")

    def test_no_purchase_yet_is_null(self, client, factory, snapshot):
        snapshot.acquisitions = (
            bought("1000200001", T0.date(), plant="1500"),
            bought("1000200002", T0.date(), material="8000009999"),
        )
        seed(factory, record(0))

        body = client.get(URL).json()

        assert body["purchasesChecked"] is True
        assert body["items"][0]["purchase"] is None

    def test_only_new_acquisition_reasons_get_one(self, client, factory, snapshot):
        snapshot.acquisitions = (bought("1000200001", T0.date()),)
        seed(factory, record(0, kind="QUANTITY_OVERRIDE"))

        assert client.get(URL).json()["items"][0]["purchase"] is None

    def test_says_when_nothing_was_looked_up(self, client, factory, monkeypatch):
        from app.initiatives.i8 import service

        monkeypatch.setattr(service, "cached_snapshot", lambda: None)
        seed(factory, record(0))

        body = client.get(URL).json()

        assert body["purchasesChecked"] is False
        assert body["items"][0]["purchase"] is None
