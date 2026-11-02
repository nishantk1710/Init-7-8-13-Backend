"""The job an assistant session was for (27-Oct-2026): the reservation line
whose text names the session, its 261 issues and 262 returns, and the route
that serves them with the repairs whose repair order names the same session."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.i8.router import router, snapshot_dependency
from app.assistant import ids
from app.core.db import get_db
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.requisitions import PR_RAISED
from app.initiatives.i8.session_jobs import JobMovement, SessionJob, build_session_jobs, is_job_line
from app.initiatives.i13 import session_link
from app.initiatives.i13.session_link import RequestLine, SessionInfo

CFG = get_i8_settings()
SESSION = ids.mint()
OTHER_SESSION = ids.mint()
REQUEST = ids.mint_request()

SESSIONS = {
    SESSION: SessionInfo(SESSION, "8000005632", "1300", False),
    OTHER_SESSION: SessionInfo(OTHER_SESSION, "8000009999", "1300", False),
}


def _row(text, *, bwart=None, material="8000005632", plant="1300", rsnum="7000001", rspos="0001"):
    return {
        "rsnum": rsnum,
        "rspos": rspos,
        "matnr": material,
        "werks": plant,
        "bwart": bwart or CFG.job_issue_movement_type,
        "aufnr": "4100001",
        "bdmng": 2,
        "bdter": date(2026, 9, 1),
        "sgtxt": text,
    }


def _move(bwart, posted, qty, *, rsnum="7000001", rspos="1"):
    return {"rsnum": rsnum, "rspos": rspos, "bwart": bwart, "budat": posted, "menge": qty}


class TestIsJobLine:
    def test_the_repair_orders_line_is_not_the_job(self):
        assert not is_job_line(_row(SESSION, bwart=CFG.repair_reservation_movement_type), CFG)

    def test_a_line_with_no_text_is_not_read(self):
        assert not is_job_line(_row("  "), CFG)
        assert is_job_line(_row(SESSION), CFG)


class TestBuild:
    def test_a_session_on_the_same_part_gets_its_line_and_movements(self):
        moves = [
            _move(CFG.job_return_movement_type, date(2026, 9, 9), 1),
            _move(CFG.job_issue_movement_type, date(2026, 9, 3), 2),
            _move(CFG.dispatch_movement_type, date(2026, 9, 4), 1),
            _move(CFG.job_issue_movement_type, date(2026, 9, 3), 5, rsnum="7000002"),
        ]
        (job,) = build_session_jobs([_row(f"for {SESSION}")], moves, SESSIONS, {}, CFG)
        assert (job.session_id, job.reservation, job.item, job.order) == (SESSION, "7000001", "1", "4100001")
        assert job.quantity == 2.0 and job.requirement_date == date(2026, 9, 1)
        assert job.issued == (JobMovement(date(2026, 9, 3), 2.0),)
        assert job.returned == (JobMovement(date(2026, 9, 9), 1.0),)

    def test_a_session_for_another_part_is_not_a_link(self):
        assert build_session_jobs([_row(OTHER_SESSION)], [], SESSIONS, {}, CFG) == ()

    def test_an_unknown_or_mistyped_session_is_not_a_link(self):
        assert build_session_jobs([_row(ids.mint()), _row(SESSION[:-1] + ("Y" if SESSION[-1] != "Y" else "Z"))], [], SESSIONS, {}, CFG) == ()

    def test_a_request_reference_links_through_the_line_for_this_part(self):
        lines = {
            REQUEST: [
                RequestLine(REQUEST, 1, OTHER_SESSION, "8000009999", "1300"),
                RequestLine(REQUEST, 2, SESSION, "8000005632", "1300"),
            ]
        }
        (job,) = build_session_jobs([_row(f"{REQUEST}/1")], [], SESSIONS, lines, CFG)
        assert (job.session_id, job.reference) == (SESSION, f"{REQUEST}/2")
        assert job.issued == () and job.returned == ()


def _line(document, item, pr, pr_item):
    return SimpleNamespace(
        purchasing_document=document, item=item, pr_number=pr, pr_item=pr_item, repair_status="AT_VENDOR"
    )


def _request(banfn, item):
    return SimpleNamespace(purchase_requisition=banfn, item=item, row_id=f"PR-{banfn}-{item}")


@pytest.fixture()
def client(monkeypatch):
    snapshot = SimpleNamespace(
        lines=(_line("4500000001", "10", "1000000001", "00010"), _line("4500000002", "10", "1000000009", "10")),
        repair_requests=(_request("1000000002", "10"),),
        repair_references=(
            ("1000000001", "10", SESSION, SESSION),
            ("1000000002", "10", SESSION, SESSION),
            ("1000000009", "10", OTHER_SESSION, OTHER_SESSION),
        ),
        session_jobs=(
            SessionJob(
                session_id=SESSION,
                reference=SESSION,
                reservation="7000001",
                item="1",
                order="4100001",
                material="8000005632",
                plant="1300",
                quantity=1.0,
                requirement_date=date(2026, 9, 1),
                issued=(JobMovement(date(2026, 9, 3), 1.0),),
                returned=(JobMovement(date(2026, 9, 9), 1.0),),
            ),
        ),
    )
    monkeypatch.setattr(
        session_link, "load_sessions", lambda db, wanted: {s: SESSIONS[s] for s in wanted if s in SESSIONS}
    )
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[snapshot_dependency] = lambda: snapshot
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


class TestRoute:
    def test_the_job_and_its_repairs(self, client, monkeypatch):
        monkeypatch.setattr(CFG, "repair_requests_enabled", True)
        body = client.get(f"/api/i8/sessions/{SESSION}/job").json()
        assert body["sessionId"] == SESSION
        (reservation,) = body["reservations"]
        assert reservation["reservation"] == "7000001" and reservation["order"] == "4100001"
        assert reservation["issued"] == [{"postedOn": "2026-09-03", "quantity": 1.0}]
        assert reservation["returned"] == [{"postedOn": "2026-09-09", "quantity": 1.0}]
        assert body["repairs"] == [
            {"rowId": "4500000001-10", "type": "PO", "document": "4500000001", "item": "10", "status": "AT_VENDOR"},
            {"rowId": "PR-1000000002-10", "type": "PR", "document": "1000000002", "item": "10", "status": PR_RAISED},
        ]

    def test_pr_rows_are_left_out_while_the_register_does_not_serve_them(self, client, monkeypatch):
        monkeypatch.setattr(CFG, "repair_requests_enabled", False)
        body = client.get(f"/api/i8/sessions/{SESSION}/job").json()
        assert [r["type"] for r in body["repairs"]] == ["PO"]

    def test_a_known_session_with_nothing_linked_gets_empty_lists(self, client):
        other = client.get(f"/api/i8/sessions/{OTHER_SESSION}/job").json()
        assert other["reservations"] == []
        assert [r["rowId"] for r in other["repairs"]] == ["4500000002-10"]

    def test_an_unknown_session_is_404(self, client):
        assert client.get(f"/api/i8/sessions/{ids.mint()}/job").status_code == 404
