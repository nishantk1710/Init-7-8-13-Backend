"""Linking a SAP reservation item to a multi-material request line.

The requester types ``<request>/<n>`` into the item text (SGTXT). The link is
made to **the line's own session**, by request + material + plant; the ``/n``
is only a cross-check. Pure parts first (reading the text, choosing the line,
the FR-4 status), then on SQLite: ``sync_links`` over a real recorded request,
the UAT stand-in for one SAP reservation per request, and the note the
request read-back puts on a link.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.assistant import ids
from app.assistant.models import AssistantSession
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i13 import uat as uat_service
from app.initiatives.i13.session_link import (
    COVERED,
    INVALID_SESSION,
    MISSING_SESSION,
    SESSION_WITHOUT_PLAN,
    RequestLine,
    SessionInfo,
    _links_wanted,
    line_number_note,
    request_refs_in,
    session_ids_in,
    session_status,
    sync_links,
)
from app.models.base import Base
from app.models.i13_session_link import SessionReservationLink, UatReservationSgtxt
from tests.assistant.test_requests import (  # noqa: F401 -- fixtures
    CONSUMABLE_LINE,
    OAR_ABOVE,
    OAR_NORMAL,
    OAR_NOT_NEEDED,
    PLANT,
    REPAIR_EXISTING,
    REPAIR_NEW,
    create,
    factory,
    oar,
    repair,
    settings,
)

REQUEST = ids.mint_request()


def _mistyped(reference: str) -> str:
    return reference[:5] + ("A" if reference[5] != "A" else "B") + reference[6:]


# --- reading the item text ----------------------------------------------------------


class TestReadingTheReference:
    @pytest.mark.parametrize(
        "text, line",
        [
            (f"{REQUEST}/2", 2),
            (f"{REQUEST}-2", 2),
            (f"Mill 3 shutdown {REQUEST} / 2 urgent", 2),
            (f"{REQUEST.lower()}/12", 12),
            (f"{REQUEST[:4]} {REQUEST[4:]}/2", 2),
            (REQUEST, None),
        ],
    )
    def test_every_way_of_typing_it(self, text, line) -> None:
        found = request_refs_in(text)
        assert [(r.request_id, r.line_no) for r in found.valid] == [(REQUEST, line)]
        assert found.invalid == ()

    def test_a_mistyped_reference_is_invalid_not_ignored(self) -> None:
        found = request_refs_in(f"{_mistyped(REQUEST)}/1")
        assert found.valid == () and len(found.invalid) == 1

    @pytest.mark.parametrize("text", ["REPAIRABLE", "RECONDITION pump", "R 12", "", None])
    def test_ordinary_text_is_neither(self, text) -> None:
        found = request_refs_in(text)
        assert found.valid == () and found.invalid == ()

    def test_a_session_id_is_not_a_request_and_a_request_is_not_a_session(self) -> None:
        session_id = ids.mint()
        assert request_refs_in(session_id).valid == ()
        assert session_ids_in(f"{REQUEST}/2") == session_ids_in("")


# --- choosing the line ----------------------------------------------------------------


LINES = {
    REQUEST: [
        RequestLine(REQUEST, 1, "S-LINE-1", "MAT-A", "1300"),
        RequestLine(REQUEST, 2, "S-LINE-2", "MAT-B", "1300"),
    ]
}


def _row(rsnum, rspos, material, sgtxt, plant="1300"):
    return {"Rsnum": rsnum, "Rspos": rspos, "Matnr": material, "Werks": plant, "Sgtxt": sgtxt}


class TestChoosingTheLine:
    def test_each_item_links_to_its_own_material_line(self) -> None:
        rows = [_row("9", "1", "MAT-A", f"{REQUEST}/1"), _row("9", "2", "MAT-B", f"{REQUEST}/2")]
        assert set(_links_wanted(rows, {}, LINES)) == {("S-LINE-1", "9", "1"), ("S-LINE-2", "9", "2")}

    def test_a_wrong_or_missing_line_number_still_links_by_material(self) -> None:
        rows = [_row("9", "1", "MAT-A", f"{REQUEST}/2"), _row("9", "2", "MAT-B", REQUEST)]
        assert set(_links_wanted(rows, {}, LINES)) == {("S-LINE-1", "9", "1"), ("S-LINE-2", "9", "2")}

    def test_a_material_the_request_never_assessed_is_no_link(self) -> None:
        assert _links_wanted([_row("9", "1", "MAT-C", f"{REQUEST}/1")], {}, LINES) == {}

    def test_the_same_material_at_another_plant_is_no_link(self) -> None:
        assert _links_wanted([_row("9", "1", "MAT-A", f"{REQUEST}/1", plant="1500")], {}, LINES) == {}

    def test_an_unknown_request_is_no_link(self) -> None:
        other = ids.mint_request()
        assert _links_wanted([_row("9", "1", "MAT-A", f"{other}/1")], {}, LINES) == {}


class TestTheLineNumberNote:
    def test_quiet_when_the_number_is_right(self) -> None:
        assert line_number_note(f"{REQUEST}/2", REQUEST, 2) is None

    def test_says_so_when_it_is_wrong_or_missing(self) -> None:
        assert line_number_note(f"{REQUEST}/1", REQUEST, 2) == "Typed /1; matched to /2 by material and plant."
        assert "No line number typed" in line_number_note(REQUEST, REQUEST, 2)


class TestStatus:
    def test_a_linked_line_is_covered_or_without_plan_like_any_session(self) -> None:
        sessions = {
            "S-PLAN": SessionInfo("S-PLAN", "M", "P", True),
            "S-NONE": SessionInfo("S-NONE", "M", "P", False),
        }
        assert session_status(f"{REQUEST}/1", "S-PLAN", sessions) == COVERED
        assert session_status(f"{REQUEST}/1", "S-NONE", sessions) == SESSION_WITHOUT_PLAN

    def test_a_request_reference_that_links_nothing_is_invalid_not_missing(self) -> None:
        """The requester used the assistant -- what they typed just does not
        lead back to this part. That is not "never used it"."""
        assert session_status(f"{REQUEST}/1", None, {}) == INVALID_SESSION
        assert session_status(f"{_mistyped(REQUEST)}/1", None, {}) == INVALID_SESSION

    def test_ordinary_text_is_still_missing(self) -> None:
        assert session_status("urgent for mill 3", None, {}) == MISSING_SESSION


# --- on a recorded request ---------------------------------------------------------------


@pytest.fixture
def recorded(factory, settings):  # noqa: F811
    engine = factory.kw["bind"]
    Base.metadata.create_all(engine, tables=[UatReservationSgtxt.__table__])
    lines = [
        oar(OAR_NORMAL, "8"),
        oar(OAR_ABOVE, "10", reason_category="OTHER", free_text="Two spares"),
        oar(OAR_NOT_NEEDED, "", decision="not_needed"),
        repair(REPAIR_EXISTING, "use_existing"),
        repair(REPAIR_NEW, "proceed_new", reason_category="URGENT_BREAKDOWN", free_text="Down"),
        CONSUMABLE_LINE,
    ]
    with factory() as db:
        request_id = create(db, lines, settings).request.id
    return request_id


class TestSyncOnARecordedRequest:
    def test_links_each_item_to_its_line_and_unlinks_when_the_text_changes(self, factory, recorded) -> None:
        rid = recorded
        rows = [
            _row("8100001", "1", OAR_NORMAL, f"{rid}/1"),
            _row("8100001", "2", OAR_ABOVE, f"Mill 3 {rid}-3"),  # wrong /n
            _row("8100001", "3", REPAIR_NEW, rid.lower()),  # no /n
            _row("8100001", "4", "9999999999", f"{rid}/9"),  # not on the request
        ]
        with factory() as db:
            result = sync_links(db, rows)
            db.commit()
            assert result.added == 3
            links = {(l.reservation_item, l.session_id) for l in db.execute(select(SessionReservationLink)).scalars()}
            by_material = {s.material_id: s.id for s in db.execute(select(AssistantSession)).scalars()}
            assert links == {
                ("1", by_material[OAR_NORMAL]),
                ("2", by_material[OAR_ABOVE]),
                ("3", by_material[REPAIR_NEW]),
            }

        with factory() as db:
            rows[0] = _row("8100001", "1", OAR_NORMAL, "changed my mind")
            result = sync_links(db, rows)
            db.commit()
            assert (result.added, result.removed) == (0, 1)


class TestUatSimulation:
    @pytest.fixture(autouse=True)
    def _no_refresh(self, monkeypatch):
        applied: list = []
        monkeypatch.setattr(uat_service, "_apply", lambda db, material, plant: applied.append((material, plant)))
        self.applied = applied

    def test_one_reservation_an_item_per_line_to_be_reserved(self, factory, recorded) -> None:
        with factory() as db:
            rows = uat_service.simulate_request(db, recorded, actor="tester")
            assert len({r.reservation_number for r in rows}) == 1
            assert int(rows[0].reservation_number) >= uat_service.SIMULATED_RANGE_START
            assert [(r.reservation_item, r.material, r.sgtxt) for r in rows] == [
                ("1", OAR_NORMAL, f"{recorded}/1"),
                ("2", OAR_ABOVE, f"{recorded}/2"),
                ("3", REPAIR_NEW, f"{recorded}/5"),
            ]
            # Declined lines (not needed, existing unit used) are not reserved.
            assert OAR_NOT_NEEDED not in {r.material for r in rows}
            assert REPAIR_EXISTING not in {r.material for r in rows}
            assert self.applied == [(OAR_NORMAL, PLANT), (OAR_ABOVE, PLANT), (REPAIR_NEW, PLANT)]

    def test_twice_is_a_conflict(self, factory, recorded) -> None:
        with factory() as db:
            uat_service.simulate_request(db, recorded, actor="tester")
        with factory() as db, pytest.raises(uat_service.UatConflict):
            uat_service.simulate_request(db, recorded, actor="tester")

    def test_an_unknown_request_is_refused(self, factory) -> None:
        engine = factory.kw["bind"]
        Base.metadata.create_all(engine, tables=[UatReservationSgtxt.__table__])
        with factory() as db, pytest.raises(uat_service.UatError):
            uat_service.simulate_request(db, ids.mint_request(), actor="tester")

    def test_a_single_line_is_simulated_with_its_request_reference(self, factory, recorded) -> None:
        from app.assistant import requests as request_service

        with factory() as db:
            line = request_service.lines_of(db, recorded)[0]
            row = uat_service.simulate(db, line.id, actor="tester")
            assert row.sgtxt == f"{recorded}/1"


def test_the_request_simulation_route_is_hidden_when_uat_is_off(monkeypatch) -> None:
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "i13_uat_simulation_enabled", False)
    response = TestClient(create_app()).post("/api/i13/uat/requests/simulate", json={"requestId": REQUEST})
    assert response.status_code == 404


class TestTheReadBackNote:
    def test_a_link_with_a_wrong_number_or_a_declined_line_carries_a_note(
        self, factory, settings, recorded, monkeypatch  # noqa: F811
    ) -> None:
        from app.api.assistant import requests_router
        from app.assistant import requests as request_service
        from app.main import create_app

        with factory() as db:
            lines = request_service.lines_of(db, recorded)
            first, declined = lines[0], lines[2]
            db.add_all([
                SessionReservationLink(session_id=first.id, reservation_number="8100001", reservation_item="1",
                                       material=first.material_id, plant=PLANT, source="SGTXT",
                                       sgtxt=f"{recorded}/4"),
                SessionReservationLink(session_id=declined.id, reservation_number="8100001", reservation_item="2",
                                       material=declined.material_id, plant=PLANT, source="SGTXT",
                                       sgtxt=f"{recorded}/3"),
            ])
            db.commit()

        monkeypatch.setattr(requests_router, "_names", lambda materials: {})
        app = create_app()

        def _db():
            with factory() as session:
                yield session

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_settings] = lambda: settings
        body = TestClient(app).get(f"/api/assistant/requests/{recorded}").json()
        app.dependency_overrides.clear()

        by_line = {l["lineNo"]: l for l in body["lines"]}
        assert by_line[1]["linkedReservations"][0]["note"] == "Typed /4; matched to /1 by material and plant."
        assert "not needed" in by_line[3]["linkedReservations"][0]["note"]
