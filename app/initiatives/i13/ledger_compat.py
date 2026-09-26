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

New capability the old CSV-backed version never had: because the reservation
lookup here goes through ``postgres_reservation.py``'s real RSNUM/RSPOS ->
goods-issue query, ``issued_quantity`` actually resolves for real data where
a matching reservation exists, instead of always mocking the same synthetic
chain.

Do not add new fields or behaviour here. This module exists to stop being
necessary, not to grow -- new work belongs in ``procurement_chain.py``/
``reservation_ledger.py`` and the ``/utilisation-ledger`` endpoints.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

from app.initiatives.i13.models import AttributionStatus, PartialLedgerEntry
from app.initiatives.i13.movements import ISSUE_TYPES, event_dates, net_quantity
from app.initiatives.i13.procurement_chain import build_procurement_chain
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.shared.material_scope import MaterialScope, classify_material_scope

Row = dict[str, Any]


class ProcurementStatus(str, Enum):
    OPEN = "OPEN"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"


class LedgerUtilisationStatus(str, Enum):
    NOT_ISSUED = "NOT_ISSUED"
    PARTIALLY_ISSUED = "PARTIALLY_ISSUED"
    FULLY_ISSUED = "FULLY_ISSUED"


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


def _procurement_status(received_qty: Decimal, ordered_qty: Decimal | None) -> ProcurementStatus:
    if received_qty <= 0:
        return ProcurementStatus.OPEN
    if ordered_qty and received_qty < ordered_qty:
        return ProcurementStatus.PARTIALLY_RECEIVED
    return ProcurementStatus.RECEIVED


def _utilisation_status(issued_qty: Decimal, received_qty: Decimal) -> LedgerUtilisationStatus:
    if issued_qty <= 0:
        return LedgerUtilisationStatus.NOT_ISSUED
    if issued_qty < received_qty:
        return LedgerUtilisationStatus.PARTIALLY_ISSUED
    return LedgerUtilisationStatus.FULLY_ISSUED


def _to_legacy_entry(
    entry: PartialLedgerEntry,
    reservation_by_pr: dict[tuple[str, str], Row],
    gi_by_reservation: dict[tuple[str, str], list[Row]],
) -> LegacyLedgerEntry:
    reservation = reservation_by_pr.get((entry.pr_number, entry.pr_item)) if entry.pr_number and entry.pr_item else None

    if reservation is not None:
        reservation_number, reservation_item = reservation["Rsnum"], reservation["Rspos"]
        gi_rows = gi_by_reservation.get((reservation_number, reservation_item), [])
        issued_qty = net_quantity(gi_rows, ISSUE_TYPES)
        first_gi_date, latest_gi_date = event_dates(gi_rows, ISSUE_TYPES)
        reservation_source = "LIVE"
    else:
        reservation_number = reservation_item = None
        issued_qty = Decimal("0")
        first_gi_date = latest_gi_date = None
        reservation_source = "UNAVAILABLE"

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
        procurement_status=_procurement_status(entry.received_quantity, entry.ordered_quantity),
        utilisation_status=_utilisation_status(issued_qty, entry.received_quantity),
        linkage_status=linkage_status,
        data_source=f"reservation:{reservation_source},pr:LIVE,po:LIVE,gr:LIVE,gi:LIVE",
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
) -> list[LegacyLedgerEntry]:
    procurement_entries = build_procurement_chain(procurement_repository, material=material, plant=plant)

    reservations = reservation_repository.get_reservations(material=material, plant=plant)
    reservation_by_pr: dict[tuple[str, str], Row] = {
        (r["Banfn"], r["Bnfpo"]): r for r in reservations if r.get("Banfn") and r.get("Bnfpo")
    }

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
        entries.append(_to_legacy_entry(entry, reservation_by_pr, gi_by_reservation))
    return entries
