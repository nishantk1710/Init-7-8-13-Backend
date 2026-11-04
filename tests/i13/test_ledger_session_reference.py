"""The Reservations tab names a linked session the way the requester knows it.

A line of a multi-material request is ``R.../n`` -- the request reference the
requester typed into SAP plus the line -- not the line's internal session ID.
A single-material session is its session ID, which is what they typed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session

from app.api.i13.reservation_ledger import _to_response
from app.assistant.models import AssistantRequest, AssistantSession
from app.initiatives.i13.models import (
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.session_link import session_references
from app.models.base import Base
from app.shared.material_scope import MaterialScope

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[AssistantRequest.__table__, AssistantSession.__table__])
    with Session(engine) as session:
        session.execute(
            insert(AssistantSession.__table__),
            [
                _session("SLINE00003", material="4000002301", request_id="R1AVPNWMDW", line_no=3),
                _session("SSINGLE001", material="5000041463"),
            ],
        )
        yield session


def _session(sid: str, *, material: str, request_id: str | None = None, line_no: int | None = None) -> dict:
    return dict(
        id=sid, flow="i13", material_id=material, plant="1300", eighty_series=False, material_scope="OAR",
        routing_reason="test", requester="tester", origin="WEB", expires_at=NOW, assessment="{}",
        request_id=request_id, line_no=line_no,
    )


def test_a_request_line_reads_as_its_request_reference_and_a_single_session_as_its_id(db):
    assert session_references(db, {"SLINE00003", "SSINGLE001", "SUNKNOWN01"}) == {
        "SLINE00003": "R1AVPNWMDW/3",
        "SSINGLE001": "SSINGLE001",
    }
    assert session_references(db, set()) == {}


def _entry(number: str, item: str) -> ReservationLedgerEntry:
    return ReservationLedgerEntry(
        ledger_id=f"RESCHAIN-{number}-{item}", reservation_number=number, reservation_item=item,
        material="4000002301", plant="1300", reservation_quantity=Decimal(4), requirement_date=None,
        pr_number=None, pr_item=None, po_number=None, po_item=None, ordered_quantity=None,
        received_quantity=None, issued_quantity=Decimal(0), first_gr_date=None, last_gr_date=None,
        first_issue_date=None, last_issue_date=None, procurement_issued_quantity=Decimal(0),
        direct_store_issued_quantity=Decimal(0), lifecycle_status=LifecycleStatus.RESERVED,
        reservation_pr_link_status=ReservationPrLinkStatus.NO_PR_REFERENCE,
        gr_link_status=GrLinkStatus.NOT_APPLICABLE, gi_link_status=GiLinkStatus.LINKED, gi_link_reason=None,
        material_scope=MaterialScope.OAR,
    )


def test_a_ledger_row_carries_the_reference_of_its_own_linked_session():
    sessions = {("2039763", "7"): "SLINE00003", ("2039999", "1"): "SSINGLE001", ("2040000", "1"): "SGONE00001"}
    references = {"SLINE00003": "R1AVPNWMDW/3", "SSINGLE001": "SSINGLE001"}

    def reference_of(number, item):
        return _to_response(_entry(number, item), sessions, references=references).session_reference

    assert reference_of("2039763", "7") == "R1AVPNWMDW/3"
    assert reference_of("2039999", "1") == "SSINGLE001"
    assert reference_of("2040000", "1") == "SGONE00001"  # a link whose session is not found keeps its ID
    assert reference_of("2041111", "1") is None  # not linked
