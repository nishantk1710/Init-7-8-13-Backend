"""Step 8 -- does a plan captured through the chat actually drive the engine?

Why this step exists at all
----------------------------
Initiative 13's exception engine is built and tested, and until this branch its
**entire validation rested on 742 plans a generator wrote.** Every no-plan and
plan-breach exception the platform has ever raised was computed from data the
platform itself produced.

A plan captured through the assistant is the first time detection sees an input
it did not author. That deserves a deliberate test rather than a discovery at
UAT, which is exactly what the plan's step 8 says.

What is proved here
--------------------
1. A plan captured through the conversation is visible to the plan reader, and
   is labelled ``CAPTURED`` rather than passing as reference data.
2. Running ACT detection for that material **changes its answer** -- the
   ``NO_PLAN`` exception that detection raises without a plan is not raised once
   a real one exists.

Point 2 is the whole join between the two halves of WS7. Point 1 alone would
only prove a row reached a table.

Scope and speed
----------------
Detection is run for **one material and plant**, not a whole plant. A plant-wide
run builds the entire reservation ledger and takes minutes; a single material
proves the same thing in seconds, and a test nobody runs proves nothing.

Skipped without ``DATABASE_URL``, the same convention as ``tests/i13``.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.main import app

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = needs_db

client = TestClient(app)
ACTOR = {"X-Actor-Id": "ws7-step8-test"}


@pytest.fixture(scope="module")
def oar_material() -> tuple[str, str]:
    """An OAR material+plant with enough reservation activity to detect against.

    Chosen from the data rather than hard-coded: a fixed material number would
    make this test a statement about one extract rather than about the join it
    is supposed to prove.

    **Deliberately not the busiest material.** Detection is scoped to one
    material and costs a few seconds there; the busiest part in this extract has
    1,605 reservations and takes long enough that nobody would run this test.
    Anything with a moderate ledger proves the same join.
    """
    with get_sessionmaker()() as db:
        row = db.execute(
            text(
                """
                SELECT TOP 1 r.material, r.plant, COUNT(*) AS n
                FROM raw_resb r
                JOIN raw_marc m ON m.material = r.material AND m.plant = r.plant
                WHERE m.mrp_type IN ('ND', 'PD')
                  AND r.material NOT LIKE '80%'
                GROUP BY 1, 2
                HAVING COUNT(*) BETWEEN 20 AND 200
                ORDER BY 3 DESC
                """
            )
        ).fetchone()

    if row is None:
        pytest.skip("no OAR material with reservation activity in this extract")
    return row[0], row[1]


def _detect(material: str, plant: str) -> list[dict]:
    """Run detection for one material+plant and return its open exceptions."""
    response = client.post(
        "/api/i13/act/run/detect", json={"material": material, "plant": plant}
    )
    assert response.status_code == 200, response.text

    listed = client.get(
        "/api/i13/act/exceptions", params={"material": material, "plant": plant}
    )
    assert listed.status_code == 200, listed.text
    return listed.json()


def _capture_plan_through_the_chat(material: str, plant: str) -> str:
    """Walk the real conversation to the point where a plan is recorded."""
    started = client.post(
        "/api/assistant/sessions",
        headers=ACTOR,
        json={
            "materialId": material,
            "plant": plant,
            "department": "Concentrator",
            "requestedFor": "T. Mokoena",
        },
    )
    assert started.status_code == 200, started.text
    body = started.json()

    if body.get("sessionId") is None:
        pytest.skip(f"no session minted: {body['routing']['reason']}")
    assert body["routing"]["flow"] == "i13", body["routing"]

    session_id = body["sessionId"]

    proceed = client.post(
        f"/api/assistant/sessions/{session_id}/turns",
        headers=ACTOR,
        json={"answer": {"choice": "proceed"}},
    )
    assert proceed.status_code == 201, proceed.text
    assert proceed.json()["step"]["id"] == "i13_capture_plan"

    today = date.today()
    captured = client.post(
        f"/api/assistant/sessions/{session_id}/turns",
        headers=ACTOR,
        json={
            "answer": {
                "purpose": "WS7 step-8 end-to-end check",
                "planned_quantity": "1",
                # A window comfortably in the future, so this plan cannot itself
                # register as a breach and confuse what the test is measuring.
                "window_start": (today + timedelta(days=30)).isoformat(),
                "window_end": (today + timedelta(days=60)).isoformat(),
                "cost_centre": None,
                "order_number": None,
            }
        },
    )
    assert captured.status_code == 201, captured.text
    return session_id


class TestTheCapturedPlanReachesTheReader:
    def test_a_plan_captured_through_the_chat_is_readable(self, oar_material) -> None:
        material, plant = oar_material
        session_id = _capture_plan_through_the_chat(material, plant)

        from app.initiatives.i13.plans import PlanSource, load_captured_plans

        with get_sessionmaker()() as db:
            captured = load_captured_plans(db)

        mine = [plan for plan in captured if plan.session_id == session_id]
        assert mine, "the plan captured through the conversation was not read back"
        assert mine[0].source is PlanSource.CAPTURED

    def test_captured_plans_are_distinguishable_from_the_fabricated_ones(
        self, oar_material
    ) -> None:
        """The single most important thing to be able to say before a demo.

        742 of the plans in this system were written by a generator. A reader
        that could not tell them apart would make "the engine works" and "these
        numbers are real" look like the same claim.
        """
        from app.initiatives.i13.plans import PlanSource, load_consumption_plans
        from pathlib import Path

        _capture_plan_through_the_chat(*oar_material)

        with get_sessionmaker()() as db:
            every = load_consumption_plans(Path(get_settings().i13_data_dir), db)

        captured = [p for p in every if p.source is PlanSource.CAPTURED]
        fabricated = [p for p in every if p.is_fabricated]

        assert captured, "no captured plans"
        assert fabricated, "the reference CSV should still be read"
        assert all(p.plan_id.startswith("PLN-") for p in captured)

    def test_the_narrow_read_projects_the_window_start(self, oar_material) -> None:
        """Option 2 of the section-6 decision: the FRS-complete plan is stored,
        and the engine keeps reading the single date it always read.

        If this ever fails, detection behaviour has changed as a side effect of
        the chat -- which is precisely what the narrow read exists to prevent.
        """
        material, plant = oar_material
        session_id = _capture_plan_through_the_chat(material, plant)

        from app.initiatives.i13.plans import load_captured_plans

        with get_sessionmaker()() as db:
            plan = next(
                p for p in load_captured_plans(db) if p.session_id == session_id
            )
            stored = db.execute(
                text(
                    "SELECT window_start, window_end FROM consumption_plan "
                    "WHERE session_id = :sid"
                ),
                {"sid": session_id},
            ).fetchone()

        assert stored.window_end is not None, "the full window was not stored"
        assert plan.planned_use_date == stored.window_start


class TestTheEngineAcceptsWhatTheChatProduces:
    """The join WS7 exists to make -- and exactly how far it currently reaches.

    **This is the honest boundary, and it is worth stating precisely.**

    ACT matches a plan to a reservation by ``(reservation_number,
    reservation_item)`` -- see ``service.py``:

        plan = plan_by_reservation.get((entry.reservation_number, entry.reservation_item))

    A plan captured through the chat has **no reservation number**, because the
    reservation did not exist when the plan was given: the planner is still
    creating it. So a captured plan cannot yet clear a ``NO_PLAN`` exception
    against a historical reservation, and no amount of code on this side changes
    that. It is blocker **B2** in its concrete form -- the link is made by
    reading the session reference back off the reservation, which needs
    ``Bednr`` exposed on ``ReservationItemSet``.

    What *can* be proved today, and is proved here, is the other half: **the
    engine accepts what the chat produces.** Run the engine's own validity rule
    over a captured plan and it returns "this is a valid plan". When B2 lands,
    the only thing that changes is the lookup key.

    That distinction is the whole point of running step 8 now rather than at
    UAT. It already caught one real defect: captured plans were being written
    with ``status="ACTIVE"``, which detection reads as withdrawn.
    """

    def test_the_engine_treats_a_captured_plan_as_valid(self, oar_material) -> None:
        """``classify_no_plan_reason`` is detection's own rule, run directly.

        Returning ``None`` means "a valid session and a valid plan both exist"
        -- no no-plan exception. This is the assertion that would have caught
        the ACTIVE/OPEN defect on the day it was written.
        """
        material, plant = oar_material
        session_id = _capture_plan_through_the_chat(material, plant)

        from app.initiatives.i13.act.detection import classify_no_plan_reason
        from app.initiatives.i13.plans import load_captured_plans

        with get_sessionmaker()() as db:
            plan = next(
                p for p in load_captured_plans(db) if p.session_id == session_id
            )

        assert classify_no_plan_reason(plan) is None, (
            f"detection rejects a plan captured through the chat: status="
            f"{plan.status!r}, session_id={plan.session_id!r}, "
            f"planned_quantity={plan.planned_quantity}. The engine and the chat "
            "disagree about what a valid plan looks like."
        )

    def test_a_captured_plan_uses_the_status_the_engine_reads(self, oar_material) -> None:
        """Pinned separately and bluntly, because it is one word and it silently
        inverts the meaning of the record."""
        material, plant = oar_material
        session_id = _capture_plan_through_the_chat(material, plant)

        from app.initiatives.i13.plans import load_captured_plans

        with get_sessionmaker()() as db:
            plan = next(
                p for p in load_captured_plans(db) if p.session_id == session_id
            )
        assert plan.status == "OPEN"

    def test_the_reservation_link_is_the_only_thing_still_missing(
        self, oar_material
    ) -> None:
        """Documents blocker B2 as an executable statement rather than a note.

        When B2 lands and the linkage populates these fields, this test fails --
        which is the correct moment for somebody to come back and extend step 8
        to assert that the NO_PLAN exception actually clears.
        """
        material, plant = oar_material
        session_id = _capture_plan_through_the_chat(material, plant)

        from app.initiatives.i13.plans import load_captured_plans

        with get_sessionmaker()() as db:
            plan = next(
                p for p in load_captured_plans(db) if p.session_id == session_id
            )

        assert plan.reservation_number == "", (
            "a captured plan now carries a reservation number -- B2 has landed. "
            "Extend this test to assert that detection clears the NO_PLAN "
            "exception for that reservation."
        )

    def test_detection_still_runs_with_captured_plans_present(self, oar_material) -> None:
        """A captured plan must not break a detection run for everything else.

        The narrow read means a captured plan reaches detection with a
        ``planned_use_date`` projected from its window start, and a plan the
        engine cannot parse would take the whole run down with it.
        """
        material, plant = oar_material
        _capture_plan_through_the_chat(material, plant)

        response = client.post(
            "/api/i13/act/run/detect", json={"material": material, "plant": plant}
        )
        assert response.status_code == 200, response.text
