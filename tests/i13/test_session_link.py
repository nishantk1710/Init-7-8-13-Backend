"""Reading the assistant session ID back off the reservation's item text (SGTXT).

Unit tests cover the pure parts: finding an ID inside free text, telling a
mistyped ID from an ordinary word, the FR-4 status of a reservation, and a
linked plan matching its reservation exactly.

The Postgres test walks the UAT stand-in end to end -- simulate, stamp, remove
-- against the real extract, and cleans up after itself. Skipped without
``DATABASE_URL``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.assistant import ids
from app.core.config import get_settings
from app.initiatives.i13.plans import ConsumptionPlan, PlanMatcher, PlanSource
from app.initiatives.i13.session_link import (
    COVERED,
    INVALID_SESSION,
    MISSING_SESSION,
    SESSION_WITHOUT_PLAN,
    SessionInfo,
    _links_wanted,
    session_ids_in,
    session_status,
)


def _mistyped(session_id: str) -> str:
    return session_id[:-1] + ("0" if session_id[-1] != "0" else "1")


class TestFindingTheIdInFreeText:
    def test_an_id_alone(self) -> None:
        sid = ids.mint()
        assert session_ids_in(sid).valid == (sid,)

    def test_an_id_among_other_words(self) -> None:
        sid = ids.mint()
        assert session_ids_in(f"Edward - {sid} / pump").valid == (sid,)

    def test_an_id_typed_in_groups_or_lower_case(self) -> None:
        sid = ids.mint()
        assert session_ids_in(f"{sid[:4]} {sid[4:8]} {sid[8:]}").valid == (sid,)
        assert session_ids_in(sid.lower()).valid == (sid,)

    def test_a_mistyped_id_is_invalid_not_ignored(self) -> None:
        found = session_ids_in(f"note {_mistyped(ids.mint())}")
        assert found.valid == ()
        assert len(found.invalid) == 1

    @pytest.mark.parametrize("text", ["SPECIALIST", "STRUCTURES", "SWAHN S26", "Survey - Isandleni", "BOIL", "", None])
    def test_ordinary_item_text_is_neither(self, text) -> None:
        found = session_ids_in(text)
        assert found.valid == () and found.invalid == ()


class TestLinksNeedTheSamePart:
    def test_a_session_for_another_material_is_not_a_link(self) -> None:
        sid = ids.mint()
        sessions = {sid: SessionInfo(sid, "MAT-A", "1300", True)}
        rows = [
            {"Rsnum": "1", "Rspos": "1", "Matnr": "MAT-A", "Werks": "1300", "Sgtxt": sid},
            {"Rsnum": "2", "Rspos": "1", "Matnr": "MAT-B", "Werks": "1300", "Sgtxt": sid},
            {"Rsnum": "3", "Rspos": "1", "Matnr": "MAT-A", "Werks": "1500", "Sgtxt": sid},
        ]
        assert set(_links_wanted(rows, sessions)) == {(sid, "1", "1")}


class TestSessionStatus:
    def test_each_fr4_outcome(self) -> None:
        with_plan, without_plan = ids.mint(), ids.mint()
        sessions = {
            with_plan: SessionInfo(with_plan, "M", "P", True),
            without_plan: SessionInfo(without_plan, "M", "P", False),
        }
        assert session_status(with_plan, with_plan, sessions) == COVERED
        assert session_status(without_plan, without_plan, sessions) == SESSION_WITHOUT_PLAN
        assert session_status(_mistyped(ids.mint()), None, sessions) == INVALID_SESSION
        # A valid-looking ID that no session (for this part) owns.
        assert session_status(ids.mint(), None, sessions) == INVALID_SESSION
        assert session_status("Edward", None, sessions) == MISSING_SESSION
        assert session_status(None, None, sessions) == MISSING_SESSION


class _Entry:
    def __init__(self, number, item, requirement_date):
        self.reservation_number = number
        self.reservation_item = item
        self.material = "M"
        self.plant = "P"
        self.requirement_date = requirement_date
        self.issued_quantity = Decimal("0")


def test_a_linked_plan_matches_its_reservation_exactly_not_by_window() -> None:
    plan = ConsumptionPlan(
        plan_id="PLN-1",
        session_id="S1",
        reservation_number="R1",
        reservation_item="1",
        material="M",
        plant="P",
        requester="r",
        purpose="p",
        planned_quantity=Decimal("2"),
        planned_use_date=date(2026, 10, 1),
        status="OPEN",
        source=PlanSource.CAPTURED,
        window_end=date(2026, 10, 5),
        linked_reservations=(("R1", "1"),),
    )
    linked = _Entry("R1", "1", date(2025, 1, 1))  # outside the window: still matched
    other = _Entry("R2", "1", date(2026, 10, 2))  # inside the window: not matched any more
    matcher = PlanMatcher([plan], [linked, other])
    assert not plan.is_unlinked_capture
    assert matcher.plan_for(linked) is plan
    assert matcher.plan_for(other) is None
    assert matcher.entries_for(plan) == [linked]


# --- Postgres: the UAT stand-in end to end ------------------------------------

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@needs_db
def test_uat_simulate_stamp_and_remove(monkeypatch) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from app.assistant.models import AssistantSession, ConsumptionPlanRecord
    from app.core.db import get_sessionmaker
    from app.main import app

    monkeypatch.setattr(get_settings(), "i13_uat_simulation_enabled", True)
    client = TestClient(app)

    with get_sessionmaker()() as db:
        row = db.execute(
            select(AssistantSession.id)
            .join(ConsumptionPlanRecord, ConsumptionPlanRecord.session_id == AssistantSession.id)
            .where(AssistantSession.flow == "i13")
            .order_by(AssistantSession.issued_at.desc())
            .limit(1)
        ).first()
    if row is None:
        pytest.skip("no I13 session with a captured plan in this database")
    session_id = row[0]
    actor = {"X-Actor-Id": "test-session-link"}

    # Leave no residue from an earlier failed run.
    for leftover in client.get("/api/i13/uat/reservations", params={"session_id": session_id}).json():
        client.post(f"/api/i13/uat/reservations/{leftover['id']}/remove")

    created = []
    try:
        simulated = client.post("/api/i13/uat/reservations/simulate", headers=actor, json={"session_id": session_id})
        assert simulated.status_code == 201, simulated.text
        created.append(simulated.json()["id"])
        number = simulated.json()["reservation_number"]
        assert int(number) >= 99_000_001

        ledger = client.get("/api/i13/utilisation-ledger", params={"session_id": session_id}).json()
        assert [(e["reservation_number"], e["session_id"], e["uat_simulated"]) for e in ledger] == [
            (number, session_id, True)
        ]
        trace = client.get(f"/api/assistant/sessions/{session_id}").json()
        assert [l["reservationNumber"] for l in trace["linkedReservations"]] == [number]
        assert trace["plans"][0]["reservationNumber"] == number

        again = client.post("/api/i13/uat/reservations/simulate", headers=actor, json={"session_id": session_id})
        assert again.status_code == 409

        candidates = client.get("/api/i13/uat/candidates", params={"session_id": session_id, "limit": 1}).json()
        if candidates:
            target = candidates[0]
            stamped = client.post(
                "/api/i13/uat/reservations/stamp",
                headers=actor,
                json={
                    "session_id": session_id,
                    "reservation_number": target["reservation_number"],
                    "reservation_item": target["reservation_item"],
                },
            )
            assert stamped.status_code == 201, stamped.text
            created.append(stamped.json()["id"])
            links = client.get("/api/i13/session-links", params={"session_id": session_id}).json()
            assert {(l["reservation_number"], l["reservation_item"]) for l in links} >= {
                (target["reservation_number"], target["reservation_item"])
            }
    finally:
        for uat_id in created:
            client.post(f"/api/i13/uat/reservations/{uat_id}/remove")

    assert client.get("/api/i13/session-links", params={"session_id": session_id}).json() == []


@needs_db
def test_uat_routes_are_hidden_when_disabled(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(get_settings(), "i13_uat_simulation_enabled", False)
    client = TestClient(app)
    assert client.get("/api/i13/uat").json()["enabled"] is False
    assert client.post("/api/i13/uat/reservations/simulate", json={"session_id": "S0000000000"}).status_code == 404
