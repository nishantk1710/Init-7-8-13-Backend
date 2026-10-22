"""Material descriptions on the declaration log (``GET /api/i8/attestations``).

The attestation table stores a material number and nothing else about the
material. The log reads better with the description beside it, so the route
looks it up in the snapshot -- but only one already built: the declaration form
reads the same route for its fault categories, and must not sit through a cold
start for a label.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i8.mappers import material_descriptions
from app.core.db import get_db
from app.initiatives.i8.models import RepairAttestation

ATTESTATIONS = "/api/i8/attestations"


def snapshot(universe=(), lines=()) -> SimpleNamespace:
    return SimpleNamespace(
        universe=tuple(SimpleNamespace(material_id=m, description=d) for m, d in universe),
        lines=tuple(SimpleNamespace(material_id=m, description=d) for m, d in lines),
    )


class TestMaterialDescriptions:
    def test_no_snapshot_means_no_descriptions(self) -> None:
        assert material_descriptions(None, ["8000000613"]) == {}

    def test_the_universe_comes_first(self) -> None:
        snap = snapshot(
            universe=[("8000000613", "SEAL KIT")],
            lines=[("8000000613", "SEAL KIT (PO TEXT)")],
        )
        assert material_descriptions(snap, ["8000000613"]) == {"8000000613": "SEAL KIT"}

    def test_a_repair_line_fills_in_for_a_material_with_no_universe_row(self) -> None:
        snap = snapshot(lines=[("8000000291", "BEARING")])
        assert material_descriptions(snap, ["8000000291"]) == {"8000000291": "BEARING"}

    def test_only_the_materials_asked_for_and_never_a_blank(self) -> None:
        snap = snapshot(universe=[("8000000001", "PUMP"), ("8000000002", None), ("8000000003", "")])
        assert material_descriptions(snap, ["8000000002", "8000000003", "8000000004"]) == {}


@pytest.fixture
def api(monkeypatch):
    """A client over an in-memory attestation table, with ``state.snapshot`` as
    the cached snapshot (None: nothing built yet)."""
    from app.main import create_app

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    RepairAttestation.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(
            RepairAttestation(
                id="ATT-1",
                material_id="8000000613",
                plant="1300",
                quantity=Decimal(1),
                condition_description="Seal leaking.",
                fault_category="SEAL_LEAK",
                recommendation="REPAIRABLE",
                attestor="tester",
                attested_at=datetime(2026, 10, 21, 9, 5, tzinfo=timezone.utc),
            )
        )
        db.commit()

    state = SimpleNamespace(snapshot=None)
    monkeypatch.setattr("app.api.i8.router.cached_snapshot", lambda: state.snapshot)

    def _db():
        with factory() as session:
            yield session

    app = create_app()
    app.dependency_overrides[get_db] = _db
    # No `with`: entering the context runs the lifespan against the real database.
    state.client = TestClient(app)
    yield state
    app.dependency_overrides.clear()


class TestTheLogCarriesDescriptions:
    def test_from_the_cached_snapshot(self, api) -> None:
        api.snapshot = snapshot(universe=[("8000000613", "SEAL KIT")])
        item = api.client.get(ATTESTATIONS).json()["items"][0]
        assert item["material"]["description"] == "SEAL KIT"

    def test_without_one_the_number_shows_alone_and_nothing_is_built(self, api) -> None:
        body = api.client.get(ATTESTATIONS).json()
        assert body["items"][0]["material"]["description"] is None
        # The form's half of the response is unaffected.
        assert body["faultCategories"]
