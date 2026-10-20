"""Server-side paging over the stored snapshot (20 Oct 2026).

Every I13 list pages, filters and counts in SQL over the whole snapshot rather
than over a first batch of rows: the procurement view's status filters and
newest-first order, the reservation ledger's newest-first order, WATCH's
"most recent activity first", and the totals the dashboard's bars read.

The reader's real SQL runs here against SQLite, on records written by the same
``kinds`` functions the build uses.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i13.ledger_compat import (
    LedgerUtilisationStatus,
    LegacyLedgerEntry,
    LinkageStatus,
    ProcurementStatus,
    delivery_state,
)
from app.initiatives.i13.models import (
    AcquiredVsPlanStatus,
    AgingBand,
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.ordering import newest_first
from app.initiatives.i13.snapshot_store import kinds
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.models.i13_snapshot_store import I13SnapshotRecord
from app.shared.material_scope import MaterialScope
from tests.assistant.conftest import watch_metric


def _legacy(ledger_id: str, **overrides) -> LegacyLedgerEntry:
    defaults = dict(
        ledger_id=ledger_id, material="M1", plant="1300", reservation_number=None, reservation_item=None,
        pr_number=ledger_id, pr_item="10", po_number="PO" + ledger_id, po_item="10",
        received_quantity=Decimal("0"), issued_quantity=Decimal("0"), open_quantity=Decimal("0"),
        first_gr_date=None, latest_gr_date=None, first_gi_date=None, latest_gi_date=None,
        procurement_status=ProcurementStatus.OPEN, utilisation_status=LedgerUtilisationStatus.NOT_ISSUED,
        linkage_status=LinkageStatus.PR_ONLY, data_source="test",
    )
    defaults.update(overrides)
    return LegacyLedgerEntry(**defaults)


def _reservation(number: str, requirement: date | None, **overrides) -> ReservationLedgerEntry:
    defaults = dict(
        ledger_id=f"RESCHAIN-{number}-1", reservation_number=number, reservation_item="1", material="M1",
        plant="1300", reservation_quantity=Decimal("1"), requirement_date=requirement, pr_number=None,
        pr_item=None, po_number=None, po_item=None, ordered_quantity=None, received_quantity=None,
        issued_quantity=Decimal("0"), first_gr_date=None, last_gr_date=None, first_issue_date=None,
        last_issue_date=None, procurement_issued_quantity=Decimal("0"), direct_store_issued_quantity=Decimal("0"),
        lifecycle_status=LifecycleStatus.RESERVED, reservation_pr_link_status=ReservationPrLinkStatus.NO_PR_REFERENCE,
        gr_link_status=GrLinkStatus.NOT_APPLICABLE, gi_link_status=GiLinkStatus.NOT_APPLICABLE,
        gi_link_reason=None, material_scope=MaterialScope.OAR,
    )
    defaults.update(overrides)
    return ReservationLedgerEntry(**defaults)


LEDGER = [
    # (entry, PR date) in build order.
    (_legacy("P1"), date(2019, 5, 1)),
    (_legacy("P2", procurement_status=ProcurementStatus.RECEIVED, linkage_status=LinkageStatus.FULL_CHAIN,
             received_quantity=Decimal("2"), first_gr_date=date(2026, 3, 1)), date(2026, 2, 1)),
    (_legacy("P3", po_number=None, po_item=None, linkage_status=LinkageStatus.UNMATCHED), None),
    (_legacy("P4", procurement_status=ProcurementStatus.PARTIALLY_RECEIVED, first_gr_date=date(2024, 7, 1)), None),
    (_legacy("P5", plant="1500", procurement_status=ProcurementStatus.RECEIVED,
             linkage_status=LinkageStatus.FULL_CHAIN), date(2025, 1, 1)),
]

WATCH = [
    watch_metric(material="W1", last_issue_date=date(2024, 1, 1)),
    # Never issued, though stock was received last week: a receipt does not
    # make it recent (SOP 3.15 measures from the last issue).
    watch_metric(material="W2", last_issue_date=None, last_movement_date=date(2026, 10, 14),
                 aging_band=AgingBand.NON_MOVING),
    watch_metric(material="W3", last_issue_date=date(2026, 9, 1),
                 acquired_vs_plan_status=AcquiredVsPlanStatus.ON_PLAN),
    watch_metric(material="W4", last_issue_date=date(2025, 5, 1), aging_band=AgingBand.NON_MOVING),
    watch_metric(material="W9", material_scope=MaterialScope.EXCLUDED, last_issue_date=date(2026, 10, 1)),
]

RESERVATIONS = [
    _reservation("R1", date(2016, 1, 1)),
    _reservation("R2", None),
    _reservation("R3", date(2026, 7, 1)),
    _reservation("R4", date(2021, 6, 1)),
]


@pytest.fixture
def stored() -> SqlSnapshot:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    I13SnapshotRecord.__table__.create(engine)
    records = (
        [kinds.lledger(e, True, d) for e, d in LEDGER]
        + [kinds.watch(m, m.material_scope is MaterialScope.OAR) for m in WATCH]
        + [kinds.rledger(e, None) for e in RESERVATIONS]
    )
    session = Session(engine)
    for seq, (record_id, r) in enumerate(enumerate(records, start=1), start=1):
        session.add(
            I13SnapshotRecord(
                id=record_id, version=1, kind=r.kind, seq=seq, material=r.material, plant=r.plant,
                rec_key=r.rec_key, oar=r.oar, f1=r.f1, f2=r.f2, f3=r.f3, n1=r.n1, d1=r.d1, text1=r.text1,
                payload=r.payload,
            )
        )
    session.commit()
    yield SqlSnapshot(session, SimpleNamespace(version=1))
    session.close()


# --- procurement ledger -----------------------------------------------------------


def test_procurement_lines_come_newest_first_with_undated_last(stored):
    rows, total = stored.legacy_ledger()
    # PR date, else first goods receipt (P4: 2024-07); P3 has neither.
    assert [r.ledger_id for r in rows] == ["P2", "P5", "P4", "P1", "P3"]
    assert total == 5


def test_procurement_status_filters_run_over_every_line_and_page(stored):
    rows, total = stored.legacy_ledger(procurement_status="received", limit=1)
    assert total == 2 and [r.ledger_id for r in rows] == ["P2"]
    rows, total = stored.legacy_ledger(procurement_status="RECEIVED", limit=1, offset=1)
    assert total == 2 and [r.ledger_id for r in rows] == ["P5"]


def test_delivery_filter_matches_the_frontend_rule(stored):
    assert [r.ledger_id for r in stored.legacy_ledger(delivery_status="NO_PO")[0]] == ["P3"]
    assert [r.ledger_id for r in stored.legacy_ledger(delivery_status="PARTIAL")[0]] == ["P4"]
    assert {r.ledger_id for r in stored.legacy_ledger(delivery_status="NOT_DELIVERED")[0]} == {"P1"}
    assert {delivery_state(e) for e, _ in LEDGER} == {"NOT_DELIVERED", "DELIVERED", "NO_PO", "PARTIAL"}


def test_ledger_counts_are_over_the_whole_ledger(stored):
    assert stored.legacy_ledger_counts() == {
        "total": 5,
        "by_linkage_status": {"PR_ONLY": 2, "FULL_CHAIN": 2, "UNMATCHED": 1},
    }
    assert stored.legacy_ledger_counts(plant="1500") == {"total": 1, "by_linkage_status": {"FULL_CHAIN": 1}}


# --- reservation ledger -------------------------------------------------------------


def test_reservations_come_newest_requirement_first(stored):
    rows, total = stored.reservation_ledger(limit=2)
    assert total == 4
    assert [e.reservation_number for e, _ in rows] == ["R3", "R4"]
    rows, _ = stored.reservation_ledger(limit=2, offset=2)
    assert [e.reservation_number for e, _ in rows] == ["R1", "R2"]


def test_the_session_filter_keeps_the_same_order(stored):
    rows, total = stored.reservation_ledger(reservation_keys={("R1", "1"), ("R3", "1"), ("R2", "1")})
    assert total == 3 and [e.reservation_number for e, _ in rows] == ["R3", "R1", "R2"]


# --- WATCH ---------------------------------------------------------------------------


def test_watch_most_recent_issue_first_over_oar_only(stored):
    rows, total = stored.watch(oar_only=True, recent=True)
    assert total == 4
    assert [m.material for m in rows] == ["W3", "W4", "W1", "W2"]


def test_watch_material_order_is_still_available(stored):
    rows, _ = stored.watch(oar_only=True)
    assert [m.material for m in rows] == ["W1", "W2", "W3", "W4"]


def test_non_movers_page_in_sql(stored):
    rows, total = stored.watch(oar_only=True, recent=True, aging_band="non_moving", limit=1)
    assert total == 2 and [m.material for m in rows] == ["W4"]


def test_watch_counts_cover_every_row(stored):
    counts = stored.watch_counts(oar_only=True)
    assert counts["total"] == 4
    assert counts["by_aging_band"]["NON_MOVING"] == 2
    assert counts["by_acquired_vs_plan_status"]["ON_PLAN"] == 1
    assert sum(counts["by_acquired_vs_plan_status"].values()) == 4


# --- the Python-side order -------------------------------------------------------------


def test_newest_first_is_stable_and_puts_undated_last():
    items = [("a", date(2020, 1, 1)), ("b", None), ("c", date(2024, 1, 1)), ("d", date(2020, 1, 1)), ("e", None)]
    assert [k for k, _ in newest_first(items, lambda i: i[1])] == ["c", "a", "d", "b", "e"]
