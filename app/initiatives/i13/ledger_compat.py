"""Compatibility shim for the retired ``GET /api/i13/ledger`` contract.

The frontend calls ``/api/i13/ledger`` today. That route (and its backing
CSV-based ``UtilisationLedgerEntry``/``build_utilisation_ledger``) was
removed when I13 fully migrated onto Postgres. Rather than break that
contract immediately, this module reproduces its exact response shape --
grain, field names, enum values -- from real Postgres data instead, so the
existing frontend keeps working unchanged while a real frontend migration to
the newer ``/utilisation-ledger`` endpoints (W6.1/W6.2, richer and more
honest about unresolved links) happens on its own schedule.

Grain matches the OLD endpoint, not the new W6.2 one: one entry per
**PR item** (from ``build_procurement_chain``, W6.1), with a reservation
attached only when one deterministically references that PR -- exactly
``ledger.py``'s old behaviour. This is deliberately NOT built from
``build_reservation_ledger`` (W6.2), which is reservation-anchored and would
silently drop every PR that has no reservation pointing to it -- a real
grain change the old contract's callers do not expect.

``issued_quantity`` on a PO line comes from ``stock_issue_matching``: the
issues of the reservation that raised its PR (RESB-BANFN/BNFPO -> MSEG-RSNUM/
RSPOS) where one exists, else the stock issued out of the material's
deliveries, oldest delivery first -- the only way to see issues against stock
material (OAR), whose PRs no reservation raises. ``issue_match`` says which.

Do not add new fields or behaviour here. This module exists to stop being
necessary, not to grow -- new work belongs in ``procurement_chain.py``/
``reservation_ledger.py`` and the ``/utilisation-ledger`` endpoints.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from app.initiatives.i13.models import AttributionStatus, PartialLedgerEntry
from app.initiatives.i13.movements import ISSUE_TYPES, event_dates, net_standing_quantity
from app.initiatives.i13.po_lines import PoLineState, po_line_states
from app.initiatives.i13.procurement_chain import build_procurement_chain
from app.initiatives.i13.stock_issue_matching import IssueMatch, PoKey, PoLineIssues, po_line_issues
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.shared.material_scope import MaterialScope, classify_material_scope

Row = dict[str, Any]


class ProcurementStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    # Nothing more will arrive (app/initiatives/i13/po_lines.py), though not
    # all of it did: SAP closed the line, or it was never an order.
    CLOSED = "CLOSED"
    NOT_AN_ORDER = "NOT_AN_ORDER"


class LedgerUtilisationStatus(str, Enum):
    NOT_ISSUED = "NOT_ISSUED"
    PARTIALLY_ISSUED = "PARTIALLY_ISSUED"
    FULLY_ISSUED = "FULLY_ISSUED"
    # Delivered before the earliest goods movement the extract holds: what was
    # issued out of it then cannot be seen (stock_issue_matching).
    BEFORE_ISSUE_HISTORY = "BEFORE_ISSUE_HISTORY"


class LinkageStatus(str, Enum):
    RESERVATION_LINKED = "RESERVATION_LINKED"
    PR_ONLY = "PR_ONLY"
    FULL_CHAIN = "FULL_CHAIN"
    UNMATCHED = "UNMATCHED"


@dataclass(frozen=True)
class LegacyLedgerEntry:
    """The exact shape ``UtilisationLedgerEntry`` used to have."""

    ledger_id: str
    material: str
    plant: str

    reservation_number: str | None
    reservation_item: str | None

    pr_number: str | None
    pr_item: str | None

    po_number: str | None
    po_item: str | None

    received_quantity: Decimal
    issued_quantity: Decimal
    open_quantity: Decimal

    first_gr_date: date | None
    latest_gr_date: date | None
    first_gi_date: date | None
    latest_gi_date: date | None

    procurement_status: ProcurementStatus
    utilisation_status: LedgerUtilisationStatus
    linkage_status: LinkageStatus

    data_source: str

    attribution_status: AttributionStatus | None = None
    attribution_evidence: str | None = None

    #: How the issued quantity was found (stock_issue_matching.IssueMatch);
    #: None for a line with nothing received and no reservation behind it.
    issue_match: IssueMatch | None = None
    delivery_count: int = 0
    next_planned_issue_date: date | None = None
    next_planned_issue_quantity: Decimal | None = None


DELIVERY_STATES = ("DELIVERED", "PARTIAL", "NOT_DELIVERED", "CLOSED", "NOT_AN_ORDER", "NO_PO")
"""The procurement view's delivery filter -- the frontend's
``deliveryStatusFromProcurement``, decided here so it can be filtered on in
SQL over every line rather than over the lines a page happened to load."""


def delivery_state(entry: LegacyLedgerEntry) -> str:
    """``NO_PO`` without a purchase order, else the procurement status read as
    delivery: OPEN is not delivered, PARTIALLY_RECEIVED partial, RECEIVED
    delivered; CLOSED and NOT_AN_ORDER keep their own names, so neither is
    ever counted as waiting for a delivery."""
    if not entry.po_number:
        return "NO_PO"
    return {
        ProcurementStatus.OPEN: "NOT_DELIVERED",
        ProcurementStatus.PARTIALLY_RECEIVED: "PARTIAL",
        ProcurementStatus.RECEIVED: "DELIVERED",
        ProcurementStatus.CLOSED: "CLOSED",
        ProcurementStatus.NOT_AN_ORDER: "NOT_AN_ORDER",
    }[entry.procurement_status]


def _procurement_status(
    received_qty: Decimal, ordered_qty: Decimal | None, line_state: PoLineState | None = None
) -> ProcurementStatus:
    """From the quantities, unless SAP says nothing more will arrive on the
    line (``line_state``): then it is closed, or not an order -- except a line
    received in full, which is simply received."""
    if line_state is not None and not (ordered_qty and received_qty >= ordered_qty):
        return ProcurementStatus(line_state.value)
    if received_qty <= 0:
        return ProcurementStatus.OPEN
    if ordered_qty and received_qty < ordered_qty:
        return ProcurementStatus.PARTIALLY_RECEIVED
    return ProcurementStatus.RECEIVED


def _utilisation_status(
    issued_qty: Decimal, received_qty: Decimal, issue_match: IssueMatch | None = None
) -> LedgerUtilisationStatus:
    if issue_match is IssueMatch.BEFORE_ISSUE_HISTORY:
        return LedgerUtilisationStatus.BEFORE_ISSUE_HISTORY
    if issued_qty <= 0:
        return LedgerUtilisationStatus.NOT_ISSUED
    if issued_qty < received_qty:
        return LedgerUtilisationStatus.PARTIALLY_ISSUED
    return LedgerUtilisationStatus.FULLY_ISSUED


def _to_legacy_entry(
    entry: PartialLedgerEntry,
    reservation_by_pr: dict[tuple[str, str], Row],
    gi_by_reservation: dict[tuple[str, str], list[Row]],
    line_states: dict[tuple[str, str], PoLineState] | None = None,
    line_issues: Mapping[PoKey, PoLineIssues] | None = None,
) -> LegacyLedgerEntry:
    reservation = reservation_by_pr.get((entry.pr_number, entry.pr_item)) if entry.pr_number and entry.pr_item else None
    line = (line_issues or {}).get((entry.po_number, entry.po_item)) if entry.po_number else None

    if reservation is not None:
        reservation_number, reservation_item = reservation["Rsnum"], reservation["Rspos"]
        gi_rows = gi_by_reservation.get((reservation_number, reservation_item), [])
        issued_qty = net_standing_quantity(gi_rows, ISSUE_TYPES)
        first_gi_date, latest_gi_date = event_dates(gi_rows, ISSUE_TYPES)
        reservation_source = "LIVE"
    else:
        reservation_number = reservation_item = None
        issued_qty = Decimal("0")
        first_gi_date = latest_gi_date = None
        reservation_source = "UNAVAILABLE"
    if line is not None:
        # A PO line's issues: its reservation's, or the stock it delivered.
        issued_qty = line.issued_quantity
        first_gi_date, latest_gi_date = line.first_issue_date, line.last_issue_date

    if reservation is not None:
        linkage_status = LinkageStatus.RESERVATION_LINKED
    elif entry.po_number and entry.received_quantity > 0:
        linkage_status = LinkageStatus.FULL_CHAIN
    elif entry.po_number:
        linkage_status = LinkageStatus.PR_ONLY
    else:
        linkage_status = LinkageStatus.UNMATCHED

    legacy = LegacyLedgerEntry(
        ledger_id=f"LEDG-{entry.pr_number}-{entry.pr_item}" if entry.pr_number else entry.ledger_id,
        material=entry.material,
        plant=entry.plant,
        reservation_number=reservation_number,
        reservation_item=reservation_item,
        pr_number=entry.pr_number,
        pr_item=entry.pr_item,
        po_number=entry.po_number,
        po_item=entry.po_item,
        received_quantity=entry.received_quantity,
        issued_quantity=issued_qty,
        open_quantity=entry.received_quantity - issued_qty,
        first_gr_date=entry.first_gr_date,
        latest_gr_date=entry.last_gr_date,
        first_gi_date=first_gi_date,
        latest_gi_date=latest_gi_date,
        procurement_status=_procurement_status(
            entry.received_quantity,
            entry.ordered_quantity,
            (line_states or {}).get((entry.po_number, entry.po_item)) if entry.po_number else None,
        ),
        utilisation_status=_utilisation_status(
            issued_qty, entry.received_quantity, line.issue_match if line is not None else None
        ),
        linkage_status=linkage_status,
        data_source=f"reservation:{reservation_source},pr:LIVE,po:LIVE,gr:LIVE,gi:LIVE",
        issue_match=line.issue_match if line is not None else None,
        delivery_count=line.delivery_count if line is not None else 0,
        next_planned_issue_date=line.next_planned_issue_date if line is not None else None,
        next_planned_issue_quantity=line.next_planned_issue_quantity if line is not None else None,
    )
    attribution = attribute_consumption_legacy(legacy)
    return replace(legacy, attribution_status=attribution.status, attribution_evidence=attribution.evidence)


@dataclass(frozen=True)
class _AttributionResult:
    status: AttributionStatus
    evidence: str


def attribute_consumption_legacy(entry: LegacyLedgerEntry) -> _AttributionResult:
    """Same precedence as ``attribution.attribute_consumption``, inlined here
    rather than shared -- that function's signature is for
    ``ReservationLedgerEntry``, not this legacy shape, and this module is
    meant to shrink and disappear, not gain a shared dependency surface."""
    if entry.issued_quantity > 0 and entry.reservation_item is not None:
        return _AttributionResult(
            AttributionStatus.RESERVATION_LINK,
            f"Rsnum {entry.reservation_number}/{entry.reservation_item} matched to goods issue",
        )
    if entry.issued_quantity > 0 and entry.issue_match is IssueMatch.OLDEST_DELIVERY_FIRST:
        return _AttributionResult(
            AttributionStatus.PROCUREMENT_LINK,
            f"PO {entry.po_number}/{entry.po_item} received; {entry.issued_quantity} issued from stock, "
            "counted against the oldest delivery first",
        )
    if entry.issue_match is IssueMatch.BEFORE_ISSUE_HISTORY:
        return _AttributionResult(
            AttributionStatus.PROCUREMENT_LINK,
            f"PO {entry.po_number}/{entry.po_item} received before the goods-movement history starts",
        )
    if entry.received_quantity > 0:
        return _AttributionResult(
            AttributionStatus.PROCUREMENT_LINK, f"PO {entry.po_number}/{entry.po_item} received, not yet issued"
        )
    return _AttributionResult(
        AttributionStatus.UNATTRIBUTED, "No reservation or procurement reference resolved evidence of consumption"
    )


def build_legacy_ledger(
    procurement_repository: PostgresProcurementRepository,
    reservation_repository: PostgresReservationRepository,
    material_scope_index: dict[tuple[str, str], str | None],
    *,
    material: str | None = None,
    plant: str | None = None,
    include_out_of_scope: bool = False,
    movement_repository: Any = None,
    line_issues: Mapping[PoKey, PoLineIssues] | None = None,
    history_start: date | None = None,
) -> list[LegacyLedgerEntry]:
    """``line_issues`` (``stock_issue_matching.po_line_issues``) when a caller
    already has them; else they are matched here from ``movement_repository``.
    With neither, a PO line's issues come from a reservation only, as before
    stock matching existed."""
    if line_issues is None and movement_repository is not None:
        line_issues = po_line_issues(
            procurement_repository, reservation_repository, movement_repository,
            material=material, plant=plant, history_start=history_start,
        )
    procurement_entries = build_procurement_chain(procurement_repository, material=material, plant=plant)

    reservations = reservation_repository.get_reservations(material=material, plant=plant)
    reservation_by_pr: dict[tuple[str, str], Row] = {
        (r["Banfn"], r["Bnfpo"]): r for r in reservations if r.get("Banfn") and r.get("Bnfpo")
    }

    line_states = po_line_states(procurement_repository, material=material, plant=plant)

    gi_rows = reservation_repository.get_goods_issue_by_reservation()
    gi_by_reservation: dict[tuple[str, str], list[Row]] = {}
    for row in gi_rows:
        gi_by_reservation.setdefault((row["Rsnum"], row["Rspos"]), []).append(row)

    entries: list[LegacyLedgerEntry] = []
    for entry in procurement_entries:
        if not include_out_of_scope:
            scope = classify_material_scope(material_scope_index.get((entry.material, entry.plant)))
            if scope is not MaterialScope.OAR:
                continue
        entries.append(_to_legacy_entry(entry, reservation_by_pr, gi_by_reservation, line_states, line_issues))
    return entries


def grni_lines(
    entries: Iterable[LegacyLedgerEntry], *, as_of: date, threshold_days: int
) -> Iterator[tuple[LegacyLedgerEntry, Decimal, int]]:
    """(line, still in stock, days since its last goods receipt) for each PO
    line received at least ``threshold_days`` ago with stock not yet issued
    -- FRS FR-6, per PO line. A line delivered before the goods-movement
    history is left out: whether it was issued cannot be seen."""
    for entry in entries:
        if not entry.po_number or entry.latest_gr_date is None:
            continue
        if entry.issue_match is IssueMatch.BEFORE_ISSUE_HISTORY:
            continue
        outstanding = entry.received_quantity - entry.issued_quantity
        age = (as_of - entry.latest_gr_date).days
        if outstanding > 0 and age >= threshold_days:
            yield entry, outstanding, age
