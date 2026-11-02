"""The I08 complete-flow switch, OFF (29-Oct-2026).

``I8_COMPLETE_FLOW_ENABLED`` puts everything released on 27/28-Oct-2026 behind
one switch, off until the team lead approves it. Off, every I08 and
reservation-assistant output reads as it did before that release, while the
data layer still builds everything. The rest of the suite runs with the switch
ON (tests/conftest.py); this file turns it off and checks each thing it hides:

1. repair PRs (register rows, detail route, verdict, declaration rows, counts);
2. stock by condition (verdict on the MARD total; keys absent or null);
3. the need-date line;
4. declaring in the assistant (script step, preview lists, request create);
5. R-reference links, session jobs, the declaring session;
6. the exception types raised, and the bought-new check dated as before;
7. the start dates;
8. the notification log;
9. the "Repair requested" timeline stage;
10. the purchase each justification answers;
and the switch itself: its default, ``GET /api/i8/features`` and the snapshot
rules. No database: fakes, and in-memory SQLite where a route writes.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i8.router import attestation_view_dependency, router as i8_router, snapshot_dependency
from app.assistant import requests as request_service
from app.assistant import script
from app.assistant import session as session_module
from app.assistant.models import AssistantSession, Justification
from app.assistant.router import Flow, RoutedFlow
from app.assistant.script import next_step
from app.assistant.steps import StepKind
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i8 import attestation as attestation_module
from app.initiatives.i8 import service as service_module
from app.initiatives.i8.acquisitions import PURCHASE_ORDER, PURCHASE_REQUISITION, NewAcquisition
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.exception_state import assign, state_of
from app.initiatives.i8.exceptions import (
    RAISED_WITHOUT_COMPLETE_FLOW,
    ExceptionItem,
    raised_without_complete_flow,
)
from app.initiatives.i8.models import ExceptionEvent, Notification, RepairAttestation
from app.initiatives.i8.notifications import log_new
from app.initiatives.i8.register import RegisterStats
from app.initiatives.i8.repairable_unit import assess
from app.initiatives.i8.reservation_assistant import build as build_i08
from app.initiatives.i8.session_jobs import UnreferencedLine
from app.initiatives.i8.universe import UniverseStats
from app.shared.material_scope import MaterialScope
from test_i8_declaration_coverage import declared
from test_i8_queue_paging import repair_line, repair_request
from tests.assistant.conftest import TODAY, universe_row
from tests.assistant.conftest import repair_line as assistant_repair_line
from tests.assistant.test_requests import (  # noqa: F401 -- fixtures
    PLANT,
    REPAIR_EXISTING,
    REPAIR_NONE,
    factory,
    settings,
)

SESSION = "S7K2M4P8Q1"
DECLARING_SESSION = "SY8VYQZ4RM"
REFERENCE_DATE = date(2026, 10, 28)
#: The three types raised before the complete flow.
BASELINE_TYPES = sorted(t.value for t in RAISED_WITHOUT_COMPLETE_FLOW)


def switch(monkeypatch, **fields) -> I8Settings:
    """Set the process's I08 settings as the environment would: the cached
    instance every module holds, its fields replaced by what a fresh
    ``I8Settings`` with ``fields`` validates to -- the complete-flow gate
    included. Restored after the test, the same instance throughout."""
    cfg = get_i8_settings()
    wanted = I8Settings(**{**cfg.model_dump(), **fields})
    for name in I8Settings.model_fields:
        monkeypatch.setattr(cfg, name, getattr(wanted, name))
    return cfg


#: What the ``off`` and ``on`` fixtures set, besides the switch itself.
OVERRIDDEN = dict(
    repair_requests_enabled=True,
    need_date_advice_enabled=True,
    attestation_cutover_date="2026-10-01",
    justification_cutover_date="2026-10-01",
    session_check_start_date="2026-10-01",
)


def settings_with(enabled: bool) -> I8Settings:
    """A separate settings object, for a test that compares the two states
    without touching the process's own (the fixtures below share one)."""
    return I8Settings(**{**get_i8_settings().model_dump(), **OVERRIDDEN, "complete_flow_enabled": enabled})


@pytest.fixture
def off(monkeypatch) -> I8Settings:
    """The switch off, with every setting it overrides set the other way."""
    return switch(monkeypatch, complete_flow_enabled=False, **OVERRIDDEN)


@pytest.fixture
def on(monkeypatch) -> I8Settings:
    """The same settings with the switch on -- the contrast."""
    return switch(monkeypatch, complete_flow_enabled=True, **OVERRIDDEN)


# --- the setting ---------------------------------------------------------------


class TestTheSetting:
    def test_off_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("I8_COMPLETE_FLOW_ENABLED", raising=False)
        assert I8Settings(_env_file=None).complete_flow_enabled is False

    def test_read_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("I8_COMPLETE_FLOW_ENABLED", "true")
        assert I8Settings(_env_file=None).complete_flow_enabled is True

    def test_off_reads_the_flows_own_switches_and_cutovers_as_off(self) -> None:
        cfg = I8Settings(
            complete_flow_enabled=False,
            repair_requests_enabled=True,
            need_date_advice_enabled=True,
            attestation_cutover_date="2026-10-01",
            justification_cutover_date="2026-10-01",
            session_check_start_date="2026-10-01",
        )
        assert cfg.repair_requests_enabled is False
        assert cfg.need_date_advice_enabled is False
        assert cfg.attestation_cutover_date_value is None
        assert cfg.justification_cutover_date_value is None
        # A snapshot-build input, left alone: MISSING_SESSION_ID is what is
        # gated, and a stored snapshot must not keep a wrong start date.
        assert cfg.session_check_start_date_value == date(2026, 10, 1)

    def test_on_keeps_them(self) -> None:
        cfg = I8Settings(
            complete_flow_enabled=True,
            repair_requests_enabled=True,
            need_date_advice_enabled=True,
            attestation_cutover_date="2026-10-01",
        )
        assert cfg.repair_requests_enabled and cfg.need_date_advice_enabled
        assert cfg.attestation_cutover_date_value == date(2026, 10, 1)

    def test_documented_off_in_env_example(self) -> None:
        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
        assert "\nI8_COMPLETE_FLOW_ENABLED=false" in text.replace("\r\n", "\n")


# --- the I08 routes, over a fake snapshot and a real attestation view ----------


def _snapshot(*, lines=(), requests=(), universe=(), references=(), jobs=(), acquisitions=(), unreferenced=()):
    lines = tuple(lines)
    requests = tuple(requests)
    return SimpleNamespace(
        reference_date=REFERENCE_DATE,
        built_at=datetime(2026, 10, 28, 5, tzinfo=timezone.utc),
        build_seconds=1.0,
        source_loaded_at=None,
        lines=lines,
        universe=tuple(universe),
        register_stats=RegisterStats(
            total_lines=len(lines), open_lines=len(lines), open_repair_requests=len(requests) or 5
        ),
        universe_stats=UniverseStats(rows=len(universe), materials=len(universe), plants=1),
        repair_requests=requests,
        repair_references=tuple(references),
        session_jobs=tuple(jobs),
        acquisitions=tuple(acquisitions),
        unreferenced_lines=tuple(unreferenced),
        session_check_start=date(2026, 10, 1),
        line=lambda document, item: next((x for x in lines if x.key == (document, item)), None),
        request=lambda banfn, bnfpo: next((r for r in requests if r.key == (banfn, bnfpo)), None),
    )


def _split_row(material: str, **overrides):
    """A universe row whose one unit on the shelf is DAMAGED (MCHB)."""
    fields = dict(
        material_id=material,
        stock_on_hand=Decimal(1),
        usable_stock=Decimal(0),
        damaged_stock=Decimal(1),
        stock_by_condition=(("DAMAGED", Decimal(1)),),
        stock_condition_source="MCHB",
    )
    fields.update(overrides)
    return universe_row(**fields)


@pytest.fixture
def stored(monkeypatch):
    """The declarations the view reads, and no justifications: no database."""
    rows: list = []
    monkeypatch.setattr(attestation_module, "find", lambda db, include_superseded=True: list(rows))
    monkeypatch.setattr(service_module, "find_attestations", lambda db, include_superseded=False: list(rows))
    monkeypatch.setattr(service_module, "load_justifications", lambda db: [])
    return rows


def _scenario(stored: list) -> SimpleNamespace:
    """A snapshot that, with the switch on, raises every check there is.

    * line 1 is bound to a Scrap declaration: sent against its assessment;
    * line 2 has no declaration, was raised before the cutover and is overdue;
    * line 3 is covered only by the R-reference on its repair order;
    * line 4 is bound to a declaration made in an assistant session;
    * repair PR 6 is open and undeclared, 23 days old;
    * material 7 has a Repairable declaration nobody raised a repair for;
    * a job line names no session;
    * material 2 was bought new three ways while line 2 was out: a new-buy
      PR, a direct PO dated by its header, and one dated by EKPO.ERDAT.
    """
    lines = [
        repair_line(1, raised_at=date(2026, 9, 1)),
        repair_line(2, raised_at=date(2026, 9, 1), pr_number=None, pr_item=None, overdue_status="OVERDUE"),
        repair_line(3, raised_at=date(2026, 1, 1), pr_number="10000003", pr_item="10"),
        repair_line(4, raised_at=date(2026, 10, 20)),
    ]
    stored += [
        declared(1, on=date(2026, 10, 2), type="PO", document=lines[0].purchasing_document,
                 item="10", recommendation="SCRAP"),
        declared(3, on=date(2026, 10, 20), session_id=DECLARING_SESSION),
        declared(4, on=date(2026, 10, 21), type="PO", document=lines[3].purchasing_document,
                 item="10", session_id=DECLARING_SESSION),
        declared(7, on=date(2026, 9, 1)),
    ]
    for row in stored:
        row.__dict__.setdefault("session_id", None)
        row.__dict__.setdefault("fault_category", "WEAR")
        row.__dict__.setdefault("attestor_name", None)
    purchase = dict(material_id="8000000002", plant="1300", description="Pump", ordered_qty=Decimal(1))
    acquisitions = [
        NewAcquisition("2000040001", "10", raised_at=date(2026, 9, 10), document_type=PURCHASE_REQUISITION, **purchase),
        NewAcquisition("4100000001", "10", raised_at=date(2026, 9, 12), document_type=PURCHASE_ORDER, **purchase),
        NewAcquisition("4100000002", "10", raised_at=None, document_type=PURCHASE_ORDER,
                       line_created_on=date(2026, 9, 15), **purchase),
    ]
    return _snapshot(
        lines=lines,
        requests=[repair_request(6, requested_on=date(2026, 10, 5))],
        universe=[_split_row(f"80000000{n:02d}") for n in range(1, 8)],
        references=[("10000003", "10", DECLARING_SESSION, DECLARING_SESSION)],
        acquisitions=acquisitions,
        unreferenced=[
            UnreferencedLine("7000001", "1", "4100001", "8000000009", "1300", date(2026, 10, 5), "MISSING_SESSION")
        ],
    )


def _view(snapshot, cfg):
    return service_module.build_attestation_view(None, snapshot, cfg)


class TestTheAttestationView:
    """``build_attestation_view`` takes its settings as an argument, so each
    state is a separate object here."""

    def test_on_raises_every_check(self, stored) -> None:
        view = _view(_scenario(stored), settings_with(True))
        assert {i.type for i in view.exceptions} == {
            "MISSING_ATTESTATION", "OVERDUE_REPAIR", "UNJUSTIFIED_ACQUISITION",
            "REPAIRED_AGAINST_ASSESSMENT", "REPAIR_PR_NOT_DECLARED",
            "DECLARED_NO_REPAIR_PR", "MISSING_SESSION_ID",
        }

    def test_off_raises_only_the_checks_that_existed_before(self, stored) -> None:
        view = _view(_scenario(stored), settings_with(False))
        assert {i.type for i in view.exceptions} <= set(BASELINE_TYPES)
        assert set(view.exception_stats.by_type) <= set(BASELINE_TYPES)
        assert view.exception_stats.session_check_start_date is None

    def test_off_dates_the_bought_new_check_as_before(self, stored) -> None:
        # Off: the direct PO lines only, dated by EKPO.ERDAT -- the new-buy PR
        # and the PO dated by its header are not checked.
        view = _view(_scenario(stored), settings_with(False))
        bought = sorted(i.id for i in view.exceptions if i.type == "UNJUSTIFIED_ACQUISITION")
        assert bought == ["EX-UNJUSTIFIED_ACQUISITION-4100000002-10"]
        assert view.exception_stats.acquisitions_checked == 2

    def test_on_checks_the_new_buy_pr_and_the_header_dated_po(self, stored) -> None:
        view = _view(_scenario(stored), settings_with(True))
        bought = sorted(i.id for i in view.exceptions if i.type == "UNJUSTIFIED_ACQUISITION")
        assert bought == [
            "EX-UNJUSTIFIED_ACQUISITION-4100000001-10",
            "EX-UNJUSTIFIED_ACQUISITION-PR-2000040001-10",
        ]

    def test_off_ignores_the_cutover(self, stored) -> None:
        missing_on = {i.id: i for i in _view(_scenario(stored), settings_with(True)).exceptions}
        assert missing_on["EX-MISSING_ATTESTATION-4500000002-10"].pre_automation
        stored.clear()
        view = _view(_scenario(stored), settings_with(False))
        assert not any(i.pre_automation for i in view.exceptions)
        assert view.exception_stats.attestation_cutover_date is None
        assert {row.status for row in view.declarations} <= {"Required", "Completed", "Flagged"}

    def test_off_has_no_reference_link_and_no_declaring_session(self, stored) -> None:
        view_on = _view(_scenario(stored), settings_with(True))
        assert view_on.coverage.links[("4500000003", "10")] == "REFERENCE"
        assert view_on.declaration_by_line[("4500000004", "10")].declaration_session_id == DECLARING_SESSION
        stored.clear()
        view = _view(_scenario(stored), settings_with(False))
        assert ("4500000003", "10") not in view.coverage.links
        assert "REFERENCE" not in set(view.coverage.links.values())
        assert all(row.declaration_session_id is None for row in view.declarations)
        assert view.declaration_by_line[("4500000003", "10")].status == "Required"

    def test_off_has_no_repair_pr_rows(self, stored) -> None:
        view = _view(_scenario(stored), settings_with(False))
        assert all(row.repair_document_type == "PO" for row in view.declarations)


@pytest.fixture
def history():
    """The two tables the exception routes write: in-memory SQLite."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ExceptionEvent.__table__.create(engine)
    Notification.__table__.create(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def i8(history):
    """The I08 router over ``state.snapshot`` and ``state.view``."""
    app = FastAPI()
    app.include_router(i8_router, prefix="/api")
    state = SimpleNamespace(snapshot=_snapshot(), view=None)

    def _db():
        with history() as session:
            yield session

    app.dependency_overrides[snapshot_dependency] = lambda: state.snapshot
    app.dependency_overrides[attestation_view_dependency] = lambda: state.view
    app.dependency_overrides[get_db] = _db
    state.client = TestClient(app)
    return state


class TestFeatures:
    @pytest.mark.parametrize("enabled", [False, True])
    def test_the_route_reads_the_setting(self, monkeypatch, enabled) -> None:
        switch(monkeypatch, complete_flow_enabled=enabled)
        app = FastAPI()
        app.include_router(i8_router, prefix="/api")
        # No snapshot override: the route must not need one (no 503).
        response = TestClient(app).get("/api/i8/features")
        assert response.status_code == 200
        assert response.json() == {"completeFlowEnabled": enabled}

    def test_mounted_on_the_app(self, off) -> None:
        from app.main import create_app

        response = TestClient(create_app()).get("/api/i8/features")
        assert response.json() == {"completeFlowEnabled": False}

    def test_snapshot_rules_echo_it_and_read_the_rest_as_off(self, off, i8) -> None:
        body = i8.client.get("/api/i8/snapshot").json()
        assert body["rules"]["completeFlowEnabled"] is False
        assert body["rules"]["repairRequestsEnabled"] is False
        assert body["rules"]["needDateAdviceEnabled"] is False
        assert body["rules"]["attestationCutoverDate"] == "(not set)"
        assert body["repairRegister"]["openRepairRequests"] == 0

    def test_snapshot_rules_on(self, on, i8) -> None:
        body = i8.client.get("/api/i8/snapshot").json()
        assert body["rules"]["completeFlowEnabled"] is True
        assert body["repairRegister"]["openRepairRequests"] == 5


class TestRegister:
    @pytest.fixture
    def scenario(self, stored, i8):
        def _load(cfg):
            stored.clear()
            i8.snapshot = _scenario(stored)
            i8.view = _view(i8.snapshot, cfg)
            return i8.client

        return _load

    def test_on_serves_the_pr_row_and_the_split(self, on, scenario) -> None:
        body = scenario(on).get("/api/i8/register").json()
        assert "PR-2000029106-10" in [item["id"] for item in body["items"]]
        assert body["items"][0]["usableStock"] is not None

    def test_off_serves_po_rows_only(self, off, scenario) -> None:
        body = scenario(off).get("/api/i8/register").json()
        assert [item["id"] for item in body["items"]] == [
            "4500000001-10", "4500000002-10", "4500000003-10", "4500000004-10",
        ]
        assert body["viewCounts"]["notOrdered"] == 0
        assert body["meta"]["openRepairRequests"] == 0

    def test_off_sends_the_split_as_null(self, off, scenario) -> None:
        client = scenario(off)
        for item in client.get("/api/i8/register").json()["items"]:
            assert (item["usableStock"], item["damagedStock"], item["stockConditionKnown"]) == (None, None, None)
            assert item["declarationSessionId"] is None
            assert item["declarationLink"] != "REFERENCE"
        for item in client.get("/api/i8/universe").json()["items"]:
            assert (item["usableStock"], item["damagedStock"]) == (None, None)

    def test_off_the_pr_detail_is_404(self, off, scenario) -> None:
        assert scenario(off).get("/api/i8/register/PR-2000029106/10").status_code == 404

    def test_on_the_timeline_starts_at_the_request(self, on, scenario) -> None:
        timeline = scenario(on).get("/api/i8/register/4500000001/10").json()["timeline"]
        assert timeline[0]["stage"] == "pr_raised"

    def test_off_the_timeline_has_no_requested_stage(self, off, scenario) -> None:
        timeline = scenario(off).get("/api/i8/register/4500000001/10").json()["timeline"]
        stages = [s["stage"] for s in timeline]
        assert "pr_raised" not in stages and "removed" not in stages
        assert stages[0] == "attested"

    def test_off_declarations_have_no_pr_rows_or_historical(self, off, scenario) -> None:
        body = scenario(off).get("/api/i8/declarations", params={"pageSize": 100}).json()
        assert {item["repairDocumentType"] for item in body["items"]} == {"PO"}
        assert "Historical" not in {item["status"] for item in body["items"]}


class TestExceptionsRoute:
    @pytest.fixture
    def loaded(self, stored, i8):
        def _load(cfg):
            stored.clear()
            i8.snapshot = _scenario(stored)
            i8.view = _view(i8.snapshot, cfg)
            return i8.client

        return _load

    def test_off_lists_and_names_only_the_older_types(self, off, loaded) -> None:
        body = loaded(off).get("/api/i8/exceptions", params={"pageSize": 100}).json()
        assert {item["type"] for item in body["items"]} <= set(BASELINE_TYPES)
        assert body["meta"]["typesRaised"] == BASELINE_TYPES
        assert body["meta"]["preAutomation"] == 0

    def test_off_a_hidden_finding_is_not_closed_as_gone(self, off, loaded, history) -> None:
        hidden = "EX-REPAIRED_AGAINST_ASSESSMENT-4500000001-10"
        hidden_pr = "EX-UNJUSTIFIED_ACQUISITION-PR-2000040001-10"
        gone = "EX-MISSING_ATTESTATION-4500000099-10"
        with history() as db:
            for exception_id in (hidden, hidden_pr, gone):
                assign(db, exception_id, "priya", actor="tester")
        loaded(off).get("/api/i8/exceptions")
        with history() as db:
            assert state_of(db, hidden).status == "OPEN"
            assert state_of(db, hidden_pr).status == "OPEN"
            assert state_of(db, gone).status == "CLOSED"

    def test_off_logs_no_notification(self, off, loaded, history) -> None:
        loaded(off).get("/api/i8/exceptions")
        with history() as db:
            assert db.execute(select(Notification)).first() is None

    def test_off_the_notification_log_is_404(self, off, i8) -> None:
        assert i8.client.get("/api/i8/notifications").status_code == 404

    def test_on_the_notification_log_is_served(self, on, i8) -> None:
        assert i8.client.get("/api/i8/notifications").status_code == 200


class TestHelpers:
    @pytest.mark.parametrize(
        ("exception_id", "raised"),
        [
            ("EX-MISSING_ATTESTATION-4500000001-10", True),
            ("EX-OVERDUE_REPAIR-4500000001-10", True),
            ("EX-UNJUSTIFIED_ACQUISITION-4100000001-10", True),
            ("EX-UNJUSTIFIED_ACQUISITION-PR-2000040001-10", False),
            ("EX-REPAIRED_AGAINST_ASSESSMENT-4500000001-10", False),
            ("EX-REPAIR_PR_NOT_DECLARED-PR-2000029106-10", False),
            ("EX-DECLARED_NO_REPAIR_PR-att-7", False),
            ("EX-MISSING_SESSION_ID-R-7000001-1", False),
        ],
    )
    def test_raised_without_complete_flow(self, exception_id, raised) -> None:
        assert raised_without_complete_flow(exception_id) is raised

    def test_log_new_writes_nothing_while_off(self, history) -> None:
        item = ExceptionItem(
            id="EX-MISSING_ATTESTATION-4500000001-10", type="MISSING_ATTESTATION", severity="warning",
            material_id="8000000001", description="Pump", plant="1300",
            purchasing_document="4500000001", item="10", title="t", detail="d",
            raised_at=date(2026, 10, 2),
        )
        with history() as db:
            assert log_new(db, [item], settings_with(False)) == 0
            assert log_new(db, [item], settings_with(True)) == 1


class TestSessionJob:
    def test_off_is_404_whatever_the_snapshot_holds(self, off, i8, monkeypatch) -> None:
        from app.initiatives.i13 import session_link

        monkeypatch.setattr(session_link, "load_sessions", lambda db, wanted: {s: object() for s in wanted})
        i8.snapshot = _snapshot(references=[("1", "10", SESSION, SESSION)])
        assert i8.client.get(f"/api/i8/sessions/{SESSION}/job").status_code == 404


# --- the repairable-unit verdict and the assistant -----------------------------


class TestStockByCondition:
    def test_off_a_damaged_only_shelf_counts_as_stock_again(self, off) -> None:
        verdict = assess(
            material_id="8000005632", plant="1300", universe_rows=[_split_row("8000005632")],
            repair_lines=[], today=TODAY,
        )
        assert verdict.exists
        assert verdict.usable_stock is None and verdict.stock_source is None
        assert verdict.headline == "There is 1 in stock at plant 1300."
        assert not any("usable" in note for note in verdict.caveats)

    def test_on_it_does_not(self, on) -> None:
        verdict = assess(
            material_id="8000005632", plant="1300", universe_rows=[_split_row("8000005632")],
            repair_lines=[], today=TODAY,
        )
        assert not verdict.exists and verdict.damaged_stock == 1

    def test_off_the_record_has_no_split_keys(self, off) -> None:
        record = build_i08(
            material_id="8000005632", plant="1300", universe_rows=[_split_row("8000005632")],
            repair_lines=[], today=TODAY,
        ).as_record(TODAY)
        for key in ("usableStock", "damagedStock", "stockConditionKnown", "stockSource", "usableBreakdown"):
            assert key not in record
        assert record["stockOnHand"] == "1"

    def test_off_the_repairable_unit_route_has_no_split_keys(self, off, i8) -> None:
        i8.snapshot = _snapshot(universe=[_split_row("8000005632")], requests=[repair_request(1, material_id="8000005632")])
        body = i8.client.get("/api/i8/repairable-unit", params={"material": "8000005632", "plant": "1300"}).json()
        for key in ("usableStock", "damagedStock", "stockConditionKnown", "stockSource"):
            assert key not in body
        assert body["exists"] is True and body["stockOnHand"] == "1"
        assert (body["openRepairRequestLines"], body["requestEvidence"]) == (0, [])

    def test_on_the_route_serves_them(self, on, i8) -> None:
        i8.snapshot = _snapshot(universe=[_split_row("8000005632")])
        body = i8.client.get("/api/i8/repairable-unit", params={"material": "8000005632", "plant": "1300"}).json()
        assert body["stockConditionKnown"] is True and body["stockSource"] == "MCHB"


class TestTheAssessment:
    """``session._assessment_for``: the repair PRs and the need-date line follow
    the switch whatever their own env vars say."""

    def _record(self, monkeypatch):
        due = assistant_repair_line(due_date=date(2026, 8, 20))  # after TODAY
        snapshot = SimpleNamespace(
            universe=[universe_row()], lines=[due], repair_requests=[repair_request(1, material_id="8000005632")]
        )
        monkeypatch.setattr(session_module, "get_snapshot", lambda db: snapshot)
        routed = RoutedFlow(Flow.I08, "8000005632", "1300", True, MaterialScope.EXCLUDED, "ND")
        assessment = session_module._assessment_for(
            None, routed, requested_quantity=None, today=TODAY, settings=get_settings(), i13_config=None
        )
        return assessment.as_record(TODAY)

    def test_on(self, on, monkeypatch) -> None:
        record = self._record(monkeypatch)
        assert record["openRepairRequestLines"] == 1
        assert record["needDateAdvice"]

    def test_off(self, off, monkeypatch) -> None:
        record = self._record(monkeypatch)
        assert (record["openRepairRequestLines"], record["openRepairRequests"]) == (0, [])
        assert record["needDateAdvice"] is None


CATEGORIES = ["URGENT_BREAKDOWN", "NO_SUITABLE_REPAIRABLE", "OTHER"]


def _step(answers, *, unit=True):
    rows = [universe_row()]
    lines = [assistant_repair_line()] if unit else []
    return next_step(
        flow=Flow.I08,
        session_id=SESSION,
        assessment=build_i08(material_id="8000005632", plant="1300", universe_rows=rows, repair_lines=lines, today=TODAY),
        answers=answers,
        today=TODAY,
        reason_categories=CATEGORIES,
    )


class TestTheScript:
    def test_off_using_the_existing_unit_ends_without_the_declare_question(self, off) -> None:
        step = _step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}})
        assert step.id == script.I08_DONE and step.kind is StepKind.TERMINAL

    def test_off_nothing_to_challenge_ends_at_once(self, off) -> None:
        assert _step({}, unit=False).id == script.I08_DONE

    def test_off_the_reason_prompt_has_no_note(self, off) -> None:
        step = _step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}})
        assert step.id == script.I08_JUSTIFICATION
        assert script.I08_REASON_NOTE not in step.prompt
        assert step.prompt.startswith("Understood. The platform does not block anything.")

    def test_off_a_reason_ends_the_conversation(self, off) -> None:
        step = _step({
            script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW},
            script.I08_JUSTIFICATION: {"reason_category": "OTHER", "free_text": "x"},
        })
        assert step.id == script.I08_DONE

    def test_on_the_question_and_the_note_are_back(self, on) -> None:
        assert _step({script.I08_ASSESSMENT: {"choice": script.USE_EXISTING}}).id == script.I08_DECLARE_CHOICE
        assert script.I08_REASON_NOTE in _step({script.I08_ASSESSMENT: {"choice": script.PROCEED_NEW}}).prompt


# --- the assistant routes ---------------------------------------------------------


@pytest.fixture
def assistant(factory, settings, monkeypatch):  # noqa: F811 -- the imported fixtures
    from app.api.assistant import requests_router
    from app.main import create_app

    monkeypatch.setattr(requests_router, "_names", lambda materials: {})
    monkeypatch.setattr(request_service, "apply_downstream", lambda keys: None)
    app = create_app()

    def _db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app)  # no `with`: the lifespan would reach the real database
    app.dependency_overrides.clear()


def _request_body(key: str, lines: list[dict]) -> dict:
    return {"idempotencyKey": key, "plant": PLANT, "requestedFor": "J Miller", "department": "10100", "lines": lines}


DECLARATION = {
    "quantity": 1, "conditionDescription": "Seal leaking.", "faultCategory": "SEAL_LEAK",
    "recommendation": "REPAIRABLE", "attestorName": "J Miller",
}


class TestTheAssistantRoutes:
    def test_off_the_preview_offers_nothing_to_declare(self, off, assistant) -> None:
        body = assistant.post(
            "/api/assistant/requests/preview", json={"materials": [{"materialId": REPAIR_NONE, "plant": PLANT}]}
        ).json()
        assert (body["faultCategories"], body["recommendations"]) == ([], [])
        assert body["reasonCategories"]

    def test_off_a_line_declaration_is_refused(self, off, assistant, factory) -> None:  # noqa: F811
        response = assistant.post("/api/assistant/requests", json=_request_body("off-declare", [
            {"materialId": REPAIR_EXISTING, "plant": PLANT, "decision": "use_existing"},
            {"materialId": REPAIR_NONE, "plant": PLANT, "declaration": DECLARATION},
        ]))
        assert response.status_code == 422
        assert response.json()["detail"]["line"] == 2
        assert "not available" in response.json()["detail"]["message"]
        with factory() as db:
            assert db.execute(select(AssistantSession)).first() is None
            assert db.execute(select(RepairAttestation)).first() is None

    def test_off_the_session_log_and_trace_leave_the_declaration_keys_out(self, off, assistant) -> None:
        response = assistant.post("/api/assistant/requests", json=_request_body("off-trace", [
            {"materialId": REPAIR_EXISTING, "plant": PLANT, "decision": "use_existing"},
        ]))
        assert response.status_code == 201, response.text
        sid = response.json()["lines"][0]["sessionId"]
        trace = assistant.get(f"/api/assistant/sessions/{sid}").json()
        assert "declarations" not in trace
        assert trace["sessionId"] == sid and "linkageNote" in trace
        (row,) = assistant.get("/api/assistant/sessions").json()["items"]
        assert "declared" not in row and "declarationRecommendation" not in row
        assert row["sessionId"] == sid

    def test_on_they_are_there(self, on, assistant) -> None:
        response = assistant.post("/api/assistant/requests", json=_request_body("on-trace", [
            {"materialId": REPAIR_EXISTING, "plant": PLANT, "decision": "use_existing"},
        ]))
        sid = response.json()["lines"][0]["sessionId"]
        assert assistant.get(f"/api/assistant/sessions/{sid}").json()["declarations"] == []
        (row,) = assistant.get("/api/assistant/sessions").json()["items"]
        assert row["declared"] is False

    def test_off_justifications_keep_the_requester_but_not_the_purchase(self, off, assistant, factory) -> None:  # noqa: F811
        with factory() as db:
            db.add(AssistantSession(
                id=SESSION, flow="i08", material_id="8000005632", plant=PLANT, requested_for="J Miller",
                eighty_series=True, material_scope="EXCLUDED", routing_reason="80-series",
                requester="r", origin="PLATFORM", assessment="{}",
                issued_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                expires_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            ))
            db.add(Justification(
                id="J0001", session_id=SESSION, exception_id=None, kind="NEW_ACQUISITION",
                reason_category="OTHER", free_text="x", material_id="8000005632", plant=PLANT,
                author="r", recorded_at=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
            ))
            db.commit()
        body = assistant.get("/api/justifications").json()
        assert "purchasesChecked" not in body
        (item,) = body["items"]
        assert "purchase" not in item
        assert item["requesterName"] == "J Miller"
