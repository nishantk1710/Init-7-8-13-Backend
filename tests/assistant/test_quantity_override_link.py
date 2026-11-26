"""An assistant quantity override is an exception only once SAP has its reservation (25-Nov-2026).

The decision record carries the reservation SAP links to the request line through its item text
(``session_reservation_link``) and is keyed on the line's session either way, so the exception ID
does not change when the link appears. Built on the real request from ``test_requests`` (OAR_ABOVE:
the requester keeps 10 against a suggested 8), on SQLite.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.assistant import requests as request_service
from app.initiatives.i13.act.domain import ExceptionStatus, ExceptionType
from app.initiatives.i13.act.service import detect_exceptions
from app.initiatives.i13.quantity_suggestion_store import (
    build_assistant_quantity_decision_records,
)
from app.models.i13_session_link import SessionReservationLink
from tests.assistant.test_requests import (  # noqa: F401 -- pytest fixtures
    OAR_ABOVE,
    OAR_NORMAL,
    PLANT,
    TODAY,
    every_case,
    factory,
    settings,
)
from tests.i13.conftest import FakeExceptionRepository, FakeNotificationPort

AS_OF = datetime(2026, 11, 25, tzinfo=timezone.utc)


def make_request(db, settings) -> dict[str, str]:
    """Material -> the session ID of its line."""
    SessionReservationLink.__table__.create(bind=db.get_bind(), checkfirst=True)
    result = request_service.create(
        db,
        plant=PLANT,
        lines=every_case(),
        idempotency_key="draft-link",
        requester="00000000-0000-0000-0000-000000000001",
        department="10100",
        today=TODAY,
        settings=settings,
    )
    db.commit()
    return {s.material_id: s.id for s in request_service.lines_of(db, result.request.id)}


def link(db, session_id: str, material: str, number: str, item: str) -> None:
    db.add(
        SessionReservationLink(
            session_id=session_id,
            reservation_number=number,
            reservation_item=item,
            material=material,
            plant=PLANT,
            source="SGTXT",
            sgtxt="R0001",
        )
    )
    db.commit()


def records_by_material(db):
    return {r.material: r for r in build_assistant_quantity_decision_records(db)}


def detect(records, repository=None):
    repository = repository or FakeExceptionRepository()
    detect_exceptions(
        AS_OF,
        ledger_entries=[],
        plans=[],
        repository=repository,
        notification_port=FakeNotificationPort(),
        plan_breach_grace_days=14,
        requester_response_days=5,
        quantity_decision_records=records,
    )
    return repository


def test_before_the_link_the_record_has_no_reservation_and_raises_nothing(factory, settings) -> None:
    with factory() as db:
        sessions = make_request(db, settings)
        record = records_by_material(db)[OAR_ABOVE]

        assert (record.reservation_number, record.reservation_item) == (None, None)
        assert record.exception_key == f"SESSION-{sessions[OAR_ABOVE]}"
        assert detect(list(records_by_material(db).values())).list() == []


def test_once_linked_the_override_is_raised_on_the_same_id_with_the_reservation(factory, settings) -> None:
    with factory() as db:
        sessions = make_request(db, settings)
        link(db, sessions[OAR_ABOVE], OAR_ABOVE, "1000000050", "0002")
        link(db, sessions[OAR_ABOVE], OAR_ABOVE, "1000000050", "0001")
        record = records_by_material(db)[OAR_ABOVE]

        assert (record.reservation_number, record.reservation_item) == ("1000000050", "0001")
        raised = detect(list(records_by_material(db).values())).list(exception_type=ExceptionType.QUANTITY_OVERRIDE)
        assert [e.exception_id for e in raised] == [f"ACT-QUANTITY_OVERRIDE-SESSION-{sessions[OAR_ABOVE]}"]
        assert raised[0].status is ExceptionStatus.CONFIRMED
        assert (raised[0].reservation_number, raised[0].reservation_item) == ("1000000050", "0001")


def test_on_a_multi_material_request_only_the_linked_overriding_line_counts(factory, settings) -> None:
    with factory() as db:
        sessions = make_request(db, settings)
        # The line that took the suggestion is linked too: still no override.
        link(db, sessions[OAR_NORMAL], OAR_NORMAL, "1000000050", "0001")
        link(db, sessions[OAR_ABOVE], OAR_ABOVE, "1000000050", "0002")

        raised = detect(list(records_by_material(db).values())).list()
        assert [e.material for e in raised] == [OAR_ABOVE]
