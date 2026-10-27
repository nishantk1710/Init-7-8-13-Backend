"""The repair timeline starts at the request, and MISSING_SESSION_ID is raised
(27-Oct-2026): an 80-series job reservation line needed on or after the check
start whose text carries no R-reference leading back to an assistant session
for its part and plant -- told apart from one that was mistyped."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.api.i8.mappers import exception_item, exception_meta, request_timeline, timeline
from app.assistant import ids
from app.initiatives.i8.attestation import LINK_BOUND, LINK_REFERENCE, LINK_WINDOW, AttestationCoverage
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.exceptions import ExceptionType, build_exceptions, missing_session_ids
from app.initiatives.i8.session_jobs import UnreferencedLine, is_consumption_line, unreferenced_lines
from app.initiatives.i13.session_link import INVALID_SESSION, MISSING_SESSION, SessionInfo

CFG = get_i8_settings()
START = date(2026, 9, 1)
SESSION = ids.mint()
SESSIONS = {SESSION: SessionInfo(SESSION, "8000005632", "1300", False)}


def _row(text=None, **over):
    row = {
        "rsnum": "7000001",
        "rspos": "0001",
        "matnr": "8000005632",
        "werks": "1300",
        "bwart": CFG.job_issue_movement_type,
        "aufnr": "4100001",
        "bdter": date(2026, 9, 10),
        "sgtxt": text,
        "xloek": None,
    }
    row.update(over)
    return row


def _judge(*rows):
    return unreferenced_lines(rows, SESSIONS, {}, CFG, START)


class TestUnreferencedLines:
    def test_a_line_with_no_text_is_missing(self):
        (line,) = _judge(_row(None))
        assert (line.reservation, line.item, line.order, line.finding, line.typed) == (
            "7000001",
            "1",
            "4100001",
            MISSING_SESSION,
            None,
        )

    def test_ordinary_text_is_missing_not_invalid(self):
        (line,) = _judge(_row("pump bearing change"))
        assert line.finding == MISSING_SESSION

    def test_a_mistyped_id_is_invalid_and_keeps_what_was_typed(self):
        typo = SESSION[:-1] + ("Y" if SESSION[-1] != "Y" else "Z")
        (line,) = _judge(_row(f"for {typo}"))
        assert line.finding == INVALID_SESSION
        assert line.typed == f"for {typo}"

    def test_a_valid_id_for_another_part_is_invalid(self):
        (line,) = _judge(_row(SESSION, matnr="8000009999"))
        assert line.finding == INVALID_SESSION

    def test_the_sessions_own_reference_is_not_raised(self):
        assert _judge(_row(f"job {SESSION}")) == ()

    def test_lines_outside_the_check_are_not_judged(self):
        assert _judge(
            _row(None, bdter=date(2026, 8, 31)),
            _row(None, bdter=None),
            _row(None, xloek=CFG.reservation_deleted_indicator),
            _row(None, matnr="1000000001"),
            _row(None, bwart=CFG.repair_reservation_movement_type),
        ) == ()

    def test_the_start_day_itself_is_checked(self):
        assert len(_judge(_row(None, bdter=START))) == 1

    def test_the_movement_type_is_a_setting(self):
        other = I8Settings(job_issue_movement_type="201")
        assert is_consumption_line(_row(None, bwart="201"), other)
        assert not is_consumption_line(_row(None), other)


def _unref(finding=MISSING_SESSION, typed=None, order="4100001"):
    return UnreferencedLine(
        reservation="7000001",
        item="1",
        order=order,
        material="8000005632",
        plant="1300",
        requirement_date=date(2026, 9, 10),
        finding=finding,
        typed=typed,
    )


class TestMissingSessionId:
    def test_the_item_names_the_reservation_line(self):
        (item,) = missing_session_ids([_unref()], START)
        assert item.id == "EX-MISSING_SESSION_ID-R-7000001-1"
        assert item.type == ExceptionType.MISSING_SESSION_ID.value
        assert item.severity == "warning"
        assert (item.reservation_document, item.reservation_item) == ("7000001", "1")
        assert (item.purchasing_document, item.item) == ("", "")
        assert item.raised_at == date(2026, 9, 10)
        assert "Reservation 7000001/1 (order 4100001)" in item.detail
        assert "no assistant reference at all" in item.detail
        assert "since 01 Sep 2026" in item.detail

    def test_a_mistyped_reference_says_what_was_typed(self):
        (item,) = missing_session_ids([_unref(INVALID_SESSION, typed="SEXX")], START)
        assert item.title == "Reservation reference does not lead to a session"
        assert '"SEXX"' in item.detail

    def test_build_exceptions_raises_and_echoes_the_start(self):
        items, stats = build_exceptions(
            [], AttestationCoverage(covered={}, uncovered=set(), window_days=30), unreferenced_lines=[_unref()], session_check_start=START
        )
        assert [i.type for i in items] == ["MISSING_SESSION_ID"]
        assert stats.by_type == {"MISSING_SESSION_ID": 1}
        assert stats.session_check_start_date == START
        assert exception_meta(stats).session_check_start_date == START

    def test_the_wire_item_carries_the_reservation_line(self):
        (item,) = missing_session_ids([_unref()], START)
        wire = exception_item(item, CFG).model_dump(by_alias=True)
        assert wire["reservationLine"] == {"type": "RESERVATION", "documentNumber": "7000001", "line": "1"}
        assert wire["repairLine"]["documentNumber"] == ""


def _line(**over):
    values = dict(
        purchasing_document="4500000001",
        item="10",
        item_category="3",
        doc_type="ZMNP",
        raised_at=date(2026, 9, 5),
        dispatched_at=None,
        due_date=None,
        days_at_vendor=None,
        pr_number="1000001",
        pr_item="10",
        pr_requested_on=date(2026, 9, 1),
        pr_department=None,
    )
    values.update(over)
    return SimpleNamespace(**values)


def _attestation():
    return SimpleNamespace(
        id="ATT-1",
        recommendation="REPAIRABLE",
        fault_category="WEAR",
        attestor="UNAUTHENTICATED-1",
        attestor_name="A Planner",
        attested_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )


class TestTimeline:
    def test_a_po_with_a_pr_starts_at_the_request(self):
        stages = timeline(_line(), date(2026, 9, 11))
        assert [s.stage for s in stages] == ["pr_raised", "attested", "po_raised", "dispatched", "at_vendor"]
        assert stages[0].label == "Repair requested"
        assert stages[0].occurred_at == date(2026, 9, 1)
        assert stages[0].evidence == "Repair PR 1000001/10."
        assert stages[0].days_since == 10
        assert stages[1].evidence == "Not declared yet."

    def test_a_po_with_no_pr_has_no_request_stage(self):
        stages = timeline(_line(pr_number=None, pr_item=None), date(2026, 9, 11))
        assert stages[0].stage == "attested"

    def test_the_declaration_says_how_it_is_linked(self):
        today = date(2026, 9, 11)
        bound = timeline(_line(), today, _attestation(), LINK_BOUND)[1].evidence
        ref = timeline(_line(), today, _attestation(), LINK_REFERENCE, "RABC/1")[1].evidence
        window = timeline(_line(), today, _attestation(), LINK_WINDOW)[1].evidence
        assert bound.startswith("Declaration ATT-1: Repairable (wear), by A Planner.")
        assert bound.endswith("It names this repair.")
        assert ref.endswith("Linked by the R-reference RABC/1 typed on the repair order.")
        assert window.endswith("Matched by material and plant within the declaration window.")

    def test_a_declared_pr_row_shows_its_declaration(self):
        request = SimpleNamespace(
            purchase_requisition="1000001", item="10", department=None, requested_on=date(2026, 9, 1)
        )
        stages = request_timeline(request, date(2026, 9, 11), _attestation(), LINK_BOUND)
        assert [s.stage for s in stages] == ["pr_raised", "attested", "po_raised"]
        assert stages[1].occurred_at == date(2026, 9, 2)
        assert stages[2].evidence == "No PO yet."
