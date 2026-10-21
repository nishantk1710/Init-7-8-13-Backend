"""Multi-material requests -- app/assistant/requests.py and its routes.

On an in-memory SQLite database holding the assistant's own tables, so every
record a line writes (session, turns, plan, suggestion, justification) is
written and read back for real. Routing and the assessments are the only
stand-ins: the assessments are the **real** I08/I13 objects the conftest
builds, so the script reads exactly what it reads in production.

One request below carries every case both initiatives produce:

====  =========  ======================================  =====================
line  flow       case                                    expect
====  =========  ======================================  =====================
1     OAR        quantity = suggestion                   plan, no reason
2     OAR        quantity > suggestion                   plan, QUANTITY_OVERRIDE
3     OAR        too little history (no suggestion)      plan, reason required
4     OAR        not needed                              no plan
5     repair     use the existing unit                   no reason
6     repair     new unit although one exists            NEW_ACQUISITION
7     repair     no unit anywhere                        nothing asked
--    --         consumable / OAR with no WATCH row      skipped, no session
====  =========  ======================================  =====================
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.assistant import requests as request_service
from app.assistant import script
from app.assistant import session as session_module
from app.assistant import turns as turn_module
from app.assistant.models import (
    AssistantRequest,
    AssistantSession,
    AssistantTurn,
    ConsumptionPlanRecord,
    Justification,
    QuantitySuggestionRecord,
)
from app.assistant.router import Flow, RoutedFlow
from app.assistant.session import SessionError
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i13.reservation_assistant import build as build_i13
from app.initiatives.i8.reservation_assistant import build as build_i08
from app.models.base import Base
from app.models.i13_session_link import SessionReservationLink
from app.shared.material_scope import MaterialScope
from tests.assistant.conftest import TODAY, repair_line, universe_row, watch_metric

PLANT = "1300"
ACTOR = "coordinator-1"

_TABLES = [
    AssistantRequest.__table__,
    AssistantSession.__table__,
    AssistantTurn.__table__,
    Justification.__table__,
    ConsumptionPlanRecord.__table__,
    QuantitySuggestionRecord.__table__,
    SessionReservationLink.__table__,
]


# --- the materials ---------------------------------------------------------------

#: OAR, 4 in stock, 1 a month, 12 issues: target 12, so 8 suggested.
OAR_NORMAL = "1000000123"
#: Same figures; the requester asks for 10.
OAR_ABOVE = "1000000124"
#: Two issues in a year: no suggestion can be made.
OAR_NO_HISTORY = "1000000125"
OAR_NOT_NEEDED = "1000000126"
REPAIR_EXISTING = "8000005632"
REPAIR_NEW = "8000005633"
REPAIR_NONE = "8000005634"
CONSUMABLE = "3000001122"
OAR_NO_DATA = "1000000999"


def _routed(material: str) -> RoutedFlow:
    if material.startswith("8"):
        return RoutedFlow(Flow.I08, material, PLANT, True, MaterialScope.EXCLUDED, "ND")
    if material == CONSUMABLE:
        return RoutedFlow(Flow.NONE, material, PLANT, False, MaterialScope.EXCLUDED, "VB")
    return RoutedFlow(Flow.I13, material, PLANT, False, MaterialScope.OAR, "PD")


def _assessment(material: str):
    if material == OAR_NO_DATA:
        raise SessionError(f"{material} at plant {PLANT} is OAR but has no WATCH row.")
    if material.startswith("8"):
        lines = [] if material == REPAIR_NONE else [repair_line(material_id=material)]
        return build_i08(
            material_id=material,
            plant=PLANT,
            universe_rows=[universe_row(material_id=material)],
            repair_lines=lines,
            today=TODAY,
        )
    history = 2 if material == OAR_NO_HISTORY else 12
    return build_i13(
        material=material,
        plant=PLANT,
        metric=watch_metric(material=material, consumption_count_12m=history),
        cross_plant_stock=(),
        requested_quantity=None,
    )


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=_TABLES)
    monkeypatch.setattr(request_service, "route", lambda db, material, plant: _routed(material))
    monkeypatch.setattr(
        request_service, "_assessment_for", lambda db, routed, **kw: _assessment(routed.material_id)
    )
    # Used by the conversation itself, when it is not handed an assessment.
    monkeypatch.setattr(
        session_module, "_assessment_for", lambda db, routed, **kw: _assessment(routed.material_id)
    )
    monkeypatch.setattr(session_module, "route", lambda db, material, plant: _routed(material))
    return sessionmaker(bind=engine)


@pytest.fixture
def settings():
    return get_settings().model_copy(
        update={
            "assistant_requests_enabled": True,
            "assistant_department_check_enabled": False,
            "assistant_request_max_lines": 20,
        }
    )


def reason(text: str = "Shutdown scope", category: str = "OTHER") -> dict:
    return {"reason_category": category, "free_text": text}


def oar(material: str, quantity: str, *, seen: str | None = "8", decision: str = "proceed", **extra):
    return request_service.LineAnswers(
        material_id=material,
        plant=PLANT,
        decision=decision,
        quantity=quantity,
        window_start="2026-08-01",
        window_end="2027-03-31",
        purpose="Mill 3 shutdown",
        seen_suggested_quantity=seen,
        check_seen=True,
        **extra,
    )


def repair(material: str, decision: str | None, **extra):
    return request_service.LineAnswers(material_id=material, plant=PLANT, decision=decision, **extra)


def every_case() -> list[request_service.LineAnswers]:
    return [
        oar(OAR_NORMAL, "8"),
        oar(OAR_ABOVE, "10", **{"reason_category": "OTHER", "free_text": "Two spares for the rebuild"}),
        CONSUMABLE_LINE,
        oar(OAR_NO_HISTORY, "3", seen=None, **{"reason_category": "OTHER", "free_text": "New conveyor"}),
        oar(OAR_NOT_NEEDED, "", decision="not_needed"),
        repair(REPAIR_EXISTING, "use_existing"),
        repair(REPAIR_NEW, "proceed_new", reason_category="URGENT_BREAKDOWN", free_text="Line 2 down"),
        repair(REPAIR_NONE, None),
        request_service.LineAnswers(material_id=OAR_NO_DATA, plant=PLANT, decision="proceed"),
    ]


CONSUMABLE_LINE = request_service.LineAnswers(material_id=CONSUMABLE, plant=PLANT)


def create(db, lines, settings, key="draft-0001", department="10100"):
    return request_service.create(
        db,
        plant=PLANT,
        lines=lines,
        idempotency_key=key,
        requester=ACTOR,
        requested_for="J Miller",
        department=department,
        today=TODAY,
        settings=settings,
    )


def settings_categories() -> list[str]:
    return get_settings().assistant_justification_reason_category_list


def count(db, model) -> int:
    return db.execute(select(func.count()).select_from(model)).scalar_one()


# --- the whole request ------------------------------------------------------------


class TestEveryCase:
    @pytest.fixture
    def created(self, factory, settings):
        with factory() as db:
            result = create(db, every_case(), settings)
            request_id = result.request.id
            keys = result.downstream_keys
        with factory() as db:
            yield db, request_id, keys

    def test_one_request_one_line_per_material_that_needs_the_assistant(self, created) -> None:
        db, request_id, _ = created
        request = db.get(AssistantRequest, request_id)
        assert request_id.startswith("R") and len(request_id) == 10
        assert request.plant == PLANT and request.requester == ACTOR and request.line_count == 7
        lines = request_service.lines_of(db, request_id)
        assert [(s.line_no, s.material_id) for s in lines] == [
            (1, OAR_NORMAL), (2, OAR_ABOVE), (3, OAR_NO_HISTORY), (4, OAR_NOT_NEEDED),
            (5, REPAIR_EXISTING), (6, REPAIR_NEW), (7, REPAIR_NONE),
        ]
        assert [s.reference for s in lines] == [f"{request_id}/{n}" for n in range(1, 8)]
        assert all(s.requested_for == "J Miller" and s.requester == ACTOR for s in lines)

    def test_materials_that_open_no_session_are_kept_with_their_reason(self, created) -> None:
        db, request_id, _ = created
        skipped = json.loads(db.get(AssistantRequest, request_id).skipped)
        assert [(s["materialId"], s["kind"]) for s in skipped] == [
            (CONSUMABLE, "out_of_scope"), (OAR_NO_DATA, "data_gap"),
        ]

    def test_every_line_carries_the_request_department(self, created) -> None:
        """One request, one department (UAT, 21 Oct): picked once, on every line."""
        db, request_id, _ = created
        assert {s.department for s in request_service.lines_of(db, request_id)} == {"10100"}
        assert request_service.departments_by_request(db, [request_id])[request_id] == ["10100"]

    def test_every_line_finished_its_conversation(self, created) -> None:
        db, request_id, _ = created
        records = request_service.line_records(db, request_service.lines_of(db, request_id))
        assert {r.outcome for r in records} == {"COMPLETED"}

    def test_what_each_line_recorded(self, created) -> None:
        db, request_id, _ = created
        by_material = {r.session.material_id: r for r in request_service.line_records(
            db, request_service.lines_of(db, request_id)
        )}

        normal = by_material[OAR_NORMAL]
        assert normal.plan.planned_quantity == Decimal("8")
        assert (normal.suggestion.suggested_quantity, normal.suggestion.accepted_quantity) == (Decimal("8"), Decimal("8"))
        assert normal.justification is None

        above = by_material[OAR_ABOVE]
        assert above.suggestion.accepted_quantity == Decimal("10")
        assert above.suggestion.is_override
        assert above.justification.kind == "QUANTITY_OVERRIDE"

        no_history = by_material[OAR_NO_HISTORY]
        assert no_history.plan.planned_quantity == Decimal("3")
        assert no_history.suggestion.suggested_quantity is None
        assert no_history.suggestion.accepted_quantity == Decimal("3")
        assert no_history.justification.free_text == "New conveyor"

        declined = by_material[OAR_NOT_NEEDED]
        assert (declined.decision, declined.plan, declined.suggestion) == ("not_needed", None, None)

        assert by_material[REPAIR_EXISTING].decision == "use_existing"
        assert by_material[REPAIR_EXISTING].justification is None
        assert by_material[REPAIR_NEW].justification.kind == "NEW_ACQUISITION"
        assert by_material[REPAIR_NONE].decision == "proceed_new"
        assert by_material[REPAIR_NONE].justification is None

    def test_the_refresh_covers_every_oar_line_that_captured_a_plan(self, created) -> None:
        _, _, keys = created
        assert [k[0] for k in keys] == [OAR_NORMAL, OAR_ABOVE, OAR_NO_HISTORY]


# --- all or nothing ----------------------------------------------------------------


def _nothing_written(factory) -> None:
    with factory() as db:
        for model in (AssistantRequest, AssistantSession, AssistantTurn, Justification,
                      ConsumptionPlanRecord, QuantitySuggestionRecord):
            assert count(db, model) == 0, model.__tablename__


class TestAllOrNothing:
    def test_a_missing_reason_refuses_the_request_and_names_the_line(self, factory, settings) -> None:
        lines = [oar(OAR_NORMAL, "8"), oar(OAR_ABOVE, "10")]  # 10 > 8, no reason
        with factory() as db, pytest.raises(request_service.RequestError) as error:
            create(db, lines, settings)
        assert error.value.line == 2
        assert "reason" in str(error.value)
        _nothing_written(factory)

    def test_the_line_named_is_the_one_on_screen_not_the_session_count(self, factory, settings) -> None:
        """A consumable on line 1 opens no session; the bad line is still 2."""
        with factory() as db, pytest.raises(request_service.RequestError) as error:
            create(db, [CONSUMABLE_LINE, repair(REPAIR_NEW, "proceed_new")], settings)
        assert error.value.line == 2

    def test_a_repairable_line_with_no_decision_says_what_to_choose(self, factory, settings) -> None:
        with factory() as db, pytest.raises(request_service.RequestError, match="existing unit") as error:
            create(db, [repair(REPAIR_EXISTING, None)], settings)
        assert error.value.line == 1
        _nothing_written(factory)

    def test_no_suggestion_needs_a_reason_too(self, factory, settings) -> None:
        with factory() as db, pytest.raises(request_service.RequestError) as error:
            create(db, [oar(OAR_NO_HISTORY, "3", seen=None)], settings)
        assert error.value.line == 1
        _nothing_written(factory)

    def test_a_bad_answer_refuses_the_request(self, factory, settings) -> None:
        bad = request_service.LineAnswers(
            material_id=OAR_NORMAL, plant=PLANT, decision="proceed", quantity="8",
            window_start="2027-03-31", window_end="2026-08-01", purpose="x",
            seen_suggested_quantity="8", check_seen=True,
        )
        with factory() as db, pytest.raises(request_service.RequestError) as error:
            create(db, [bad], settings)
        assert "ends" in str(error.value)
        _nothing_written(factory)

    def test_a_suggestion_that_moved_since_the_preview_is_a_conflict(self, factory, settings) -> None:
        with factory() as db, pytest.raises(request_service.RequestConflict) as error:
            create(db, [oar(OAR_NORMAL, "8", seen="9")], settings)
        assert error.value.changed == [
            {"materialId": OAR_NORMAL, "seenSuggestedQuantity": "9", "suggestedQuantity": "8"}
        ]
        _nothing_written(factory)

    def test_a_suggestion_that_appeared_since_is_a_conflict(self, factory, settings) -> None:
        with factory() as db, pytest.raises(request_service.RequestConflict):
            create(db, [oar(OAR_NORMAL, "8", seen=None)], settings)


class TestShape:
    @pytest.mark.parametrize(
        "lines, line, words",
        [
            ([], None, "at least one"),
            ([oar(OAR_NORMAL, "8"), oar("0" + OAR_NORMAL, "8")], 2, "twice"),
            ([request_service.LineAnswers(material_id=OAR_NORMAL, plant="1500")], 1, "one plant"),
        ],
    )
    def test_refused_before_anything_is_written(self, factory, settings, lines, line, words) -> None:
        with factory() as db, pytest.raises(request_service.RequestError) as error:
            create(db, lines, settings)
        assert error.value.line == line
        assert words in str(error.value)
        _nothing_written(factory)

    def test_the_line_cap(self, factory, settings) -> None:
        tight = settings.model_copy(update={"assistant_request_max_lines": 2})
        lines = [oar(OAR_NORMAL, "8"), oar(OAR_ABOVE, "8"), oar(OAR_NOT_NEEDED, "", decision="not_needed")]
        with factory() as db, pytest.raises(request_service.RequestError, match="up to 2"):
            create(db, lines, tight)

    def test_the_cap_never_exceeds_the_hard_maximum(self, settings) -> None:
        loose = settings.model_copy(update={"assistant_request_max_lines": 500})
        assert request_service.max_lines(loose) == request_service.HARD_MAX_LINES

    def test_nothing_that_needs_the_assistant_is_refused(self, factory, settings) -> None:
        with factory() as db, pytest.raises(request_service.RequestError, match="nothing to record"):
            create(db, [CONSUMABLE_LINE], settings)

    def test_a_department_that_is_not_a_code_is_refused(self, factory, settings) -> None:
        checked = settings.model_copy(update={"assistant_department_check_enabled": True})
        lines = [oar(OAR_NORMAL, "8"), repair(REPAIR_EXISTING, "use_existing")]
        with factory() as db, pytest.raises(request_service.RequestError, match="not a department code"):
            create(db, lines, checked, department="Mechanical")

    def test_a_request_needs_a_department(self, factory, settings) -> None:
        with factory() as db, pytest.raises(request_service.RequestError, match="Pick the department"):
            create(db, [oar(OAR_NORMAL, "8")], settings, department=None)

    def test_two_departments_on_one_request_are_refused(self, factory, settings) -> None:
        lines = [oar(OAR_NORMAL, "8"), repair(REPAIR_EXISTING, "use_existing", department="10300")]
        with factory() as db, pytest.raises(request_service.RequestError, match="one department"):
            create(db, lines, settings, department="10100")

    def test_older_screens_per_line_departments_are_accepted_when_they_agree(self, factory, settings) -> None:
        lines = [oar(OAR_NORMAL, "8", department="10300"), repair(REPAIR_EXISTING, "use_existing", department="10300")]
        with factory() as db:
            created = create(db, lines, settings, department=None)
            assert {s.department for s in created.sessions} == {"10300"}


class TestIdempotency:
    def test_the_same_draft_twice_is_one_request(self, factory, settings) -> None:
        with factory() as db:
            first = create(db, [oar(OAR_NORMAL, "8")], settings, key="draft-same")
        with factory() as db:
            again = create(db, [oar(OAR_NORMAL, "8")], settings, key="draft-same")
            assert again.replayed and again.request.id == first.request.id
            assert again.downstream_keys == []
            assert count(db, AssistantRequest) == 1 and count(db, AssistantSession) == 1


# --- preview ----------------------------------------------------------------------


class TestPreview:
    def test_what_each_material_would_ask(self, factory) -> None:
        with factory() as db:
            lines = request_service.preview(
                db,
                [(OAR_NORMAL, PLANT), (OAR_NO_HISTORY, PLANT), (REPAIR_EXISTING, PLANT), (REPAIR_NONE, PLANT),
                 (CONSUMABLE, PLANT), (OAR_NO_DATA, PLANT), (OAR_NORMAL, "9999")],
                today=TODAY,
            )
        kinds = [(l.material_id, l.kind) for l in lines]
        assert kinds == [
            (OAR_NORMAL, "i13"), (OAR_NO_HISTORY, "i13"), (REPAIR_EXISTING, "i08"), (REPAIR_NONE, "i08"),
            (CONSUMABLE, "out_of_scope"), (OAR_NO_DATA, "data_gap"), (OAR_NORMAL, "invalid"),
        ]
        normal, no_history, existing, none = lines[:4]
        assert normal.suggestion.quantity == Decimal("8")
        assert (normal.suggestion.window_start, normal.suggestion.window_end, normal.suggestion.window_months) == (
            TODAY, date(2027, 3, 31), 8,
        )
        assert no_history.suggestion.quantity is None and no_history.suggestion.window_start is None
        assert "No quantity is suggested" in no_history.suggestion.reason
        assert (existing.unit_exists, none.unit_exists) == (True, False)
        assert normal.facts["stockOnHand"] == "4"

    def test_the_suggestion_is_not_capped_by_any_quantity(self, factory) -> None:
        with factory() as db:
            [line] = request_service.preview(db, [(OAR_NORMAL, PLANT)], today=TODAY)
        assert line.suggestion.quantity == Decimal("8")


def test_the_window_runs_until_the_quantity_is_used_up() -> None:
    assert request_service.suggested_window(Decimal("4"), Decimal("0.5"), date(2026, 10, 15)) == (
        date(2026, 10, 15), date(2027, 6, 15), 8,
    )
    assert request_service.suggested_window(Decimal("100"), Decimal("1"), TODAY)[2] == 12
    assert request_service.suggested_window(Decimal("1"), Decimal("1"), date(2026, 1, 31))[1] == date(2026, 2, 28)
    assert request_service.suggested_window(None, Decimal("1"), TODAY) is None
    assert request_service.suggested_window(Decimal("4"), Decimal("0"), TODAY) is None


# --- the conversation the request replays through -----------------------------------


class TestTheConversation:
    def test_no_suggestion_asks_a_reason_then_records_the_suggestion(self, factory, settings) -> None:
        with factory() as db:
            started = session_module.start(
                db, material_id=OAR_NO_HISTORY, plant=PLANT, requester=ACTOR, today=TODAY, settings=settings
            )
            sid = started.session.id
            turn_module.answer(db, sid, {"choice": "proceed"}, actor=ACTOR, today=TODAY,
                               settings=settings, downstream=False)
            step, _ = turn_module.answer(
                db, sid,
                {"purpose": "x", "planned_quantity": "3", "window_start": "2026-08-01", "window_end": "2026-12-31"},
                actor=ACTOR, today=TODAY, settings=settings, downstream=False,
            )
            assert step.id == script.I13_QUANTITY_JUSTIFICATION
            assert count(db, QuantitySuggestionRecord) == 0  # not final yet
            step, _ = turn_module.answer(db, sid, reason(), actor=ACTOR, today=TODAY,
                                         settings=settings, downstream=False)
            assert step.is_terminal
            [record] = db.execute(select(QuantitySuggestionRecord)).scalars().all()
            assert (record.suggested_quantity, record.accepted_quantity) == (None, Decimal("3"))

    def test_a_finished_conversation_is_never_reopened(self, factory, settings) -> None:
        """A session that ended under the old script (nothing asked when no
        suggestion could be made) must not accept the step the script has
        since gained."""
        with factory() as db:
            started = session_module.start(
                db, material_id=OAR_NO_HISTORY, plant=PLANT, requester=ACTOR, today=TODAY, settings=settings
            )
            db.add(AssistantTurn(session_id=started.session.id, sequence=0, step_id=script.I13_DONE,
                                 step_kind="terminal", question="Plan recorded.", answer=None, actor=ACTOR))
            db.commit()
            with pytest.raises(turn_module.AnswerError, match="already finished"):
                turn_module.answer(db, started.session.id, reason(), actor=ACTOR, today=TODAY,
                                   settings=settings, downstream=False)

    def test_a_stand_alone_session_is_its_own_reference(self, factory, settings) -> None:
        with factory() as db:
            started = session_module.start(
                db, material_id=OAR_NORMAL, plant=PLANT, requester=ACTOR, today=TODAY, settings=settings
            )
            assert started.session.reference == started.session.id
            assert started.session.request_id is None


# --- the routes ----------------------------------------------------------------------


class TestRoutes:
    @pytest.fixture
    def client(self, factory, settings, monkeypatch):
        from app.api.assistant import requests_router
        from app.main import create_app

        refreshed: list = []
        monkeypatch.setattr(requests_router, "_names", lambda materials: {})
        monkeypatch.setattr(request_service, "apply_downstream", lambda keys: refreshed.extend(keys))
        app = create_app()

        def _db():
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_settings] = lambda: settings
        client = TestClient(app)  # no `with`: the lifespan would reach the real database
        client.refreshed = refreshed
        yield client
        app.dependency_overrides.clear()

    def _body(self, key="draft-route-1", lines=None):
        return {
            "idempotencyKey": key,
            "plant": PLANT,
            "requestedFor": "J Miller",
            "department": "10100",
            "lines": lines or [
                {"materialId": OAR_NORMAL, "plant": PLANT, "decision": "proceed",
                 "quantity": "8", "windowStart": "2026-08-01", "windowEnd": "2027-03-31",
                 "purpose": "Mill 3", "seenSuggestedQuantity": "8"},
                {"materialId": REPAIR_NEW, "plant": PLANT, "decision": "proceed_new",
                 "reasonCategory": "URGENT_BREAKDOWN", "freeText": "Line 2 down"},
                {"materialId": CONSUMABLE, "plant": PLANT},
            ],
        }

    def test_off_by_default(self, client, settings) -> None:
        client.app.dependency_overrides[get_settings] = lambda: settings.model_copy(
            update={"assistant_requests_enabled": False}
        )
        assert client.get("/api/assistant/requests").status_code == 404
        assert client.post("/api/assistant/requests", json=self._body()).status_code == 404

    def test_preview(self, client) -> None:
        response = client.post("/api/assistant/requests/preview", json={
            "materials": [{"materialId": OAR_NORMAL, "plant": PLANT}, {"materialId": CONSUMABLE, "plant": PLANT}],
        })
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["maxLines"] == 20
        assert body["reasonCategories"][0] == {"value": "URGENT_BREAKDOWN", "label": "Urgent Breakdown"}
        assert [c["value"] for c in body["reasonCategories"]] == settings_categories()
        oar_line, consumable = body["lines"]
        assert oar_line["kind"] == "i13" and oar_line["suggestion"]["quantity"] == "8"
        assert consumable["kind"] == "out_of_scope"

    def test_create_then_read_back(self, client) -> None:
        response = client.post("/api/assistant/requests", json=self._body())
        assert response.status_code == 201, response.text
        body = response.json()
        request_id = body["requestId"]
        assert [l["reference"] for l in body["lines"]] == [f"{request_id}/1", f"{request_id}/2"]
        assert body["department"] == "10100"
        assert body["departments"] == ["10100"]
        assert body["skipped"][0]["materialId"] == CONSUMABLE
        first = body["lines"][0]
        assert (first["plannedQuantity"], first["suggestedQuantity"], first["acceptedQuantity"]) == ("8", "8", "8")
        assert body["lines"][1]["justification"]["kind"] == "NEW_ACQUISITION"
        assert [k[0] for k in client.refreshed] == [OAR_NORMAL]

        again = client.get(f"/api/assistant/requests/{request_id.lower()}")
        assert again.status_code == 200 and again.json()["lines"] == body["lines"]
        listed = client.get("/api/assistant/requests").json()["items"]
        assert listed[0]["requestId"] == request_id and listed[0]["lineCount"] == 2

        session = client.get(f"/api/assistant/sessions/{first['sessionId']}").json()
        assert (session["requestId"], session["lineNo"], session["reference"]) == (request_id, 1, f"{request_id}/1")

    def test_a_resubmitted_draft_is_the_same_request(self, client) -> None:
        first = client.post("/api/assistant/requests", json=self._body(key="draft-twice")).json()
        again = client.post("/api/assistant/requests", json=self._body(key="draft-twice"))
        assert again.status_code == 201
        assert again.json()["requestId"] == first["requestId"] and again.json()["replayed"] is True

    def test_a_refused_line_is_a_422_naming_it(self, client) -> None:
        body = self._body(lines=[
            {"materialId": REPAIR_NEW, "plant": PLANT, "decision": "proceed_new"},  # no reason
        ])
        response = client.post("/api/assistant/requests", json=body)
        assert response.status_code == 422
        assert response.json()["detail"]["line"] == 1

    def test_a_moved_suggestion_is_a_409_with_the_fresh_figure(self, client) -> None:
        body = self._body(lines=[
            {"materialId": OAR_NORMAL, "plant": PLANT, "decision": "proceed", "quantity": "8",
             "windowStart": "2026-08-01", "windowEnd": "2027-03-31", "purpose": "x",
             "seenSuggestedQuantity": "5"},
        ])
        response = client.post("/api/assistant/requests", json=body)
        assert response.status_code == 409
        assert response.json()["detail"]["changed"][0]["suggestedQuantity"] == "8"

    def test_an_unknown_or_mistyped_reference_is_a_404(self, client) -> None:
        assert client.get("/api/assistant/requests/R00000000X").status_code == 404
        assert client.get("/api/assistant/requests/nonsense").status_code == 404
