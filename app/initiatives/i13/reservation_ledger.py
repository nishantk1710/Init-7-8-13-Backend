"""W6.2: Reservation -> PR -> PO -> GR -> GI, extending W6.1 -- never
rebuilding it.

``build_reservation_ledger`` calls W6.1's ``build_procurement_chain`` exactly
once (batched, no N+1) and attaches reservation context on top: the PR/PO/GR
fields, ``gr_link_status``, and the reversal-netting/date logic all come
straight from the existing ``PartialLedgerEntry`` this module reads, never
recomputed here. The only new stitching in this file is Reservation -> PR
(``raw_resb.purchase_requisition``/``item_of_requisition``, exact match) and
Reservation -> GI (``raw_mseg.reservation``/``item_no_stock_transfer_reserv``,
exact match) -- both deterministic, neither inferred from material/plant/
quantity/date proximity.

Measured facts about this dataset that shape the design below (see the W6.2
implementation report for the full numbers):
  * ``raw_resb``'s own key (reservation, item_no_stock_transfer_reserv) is
    100% unique (105,848 rows).
  * ``raw_mseg.reservation`` is populated on 91.2% of real 201 rows and 90.9%
    of real 261 rows (vs. 0% carrying a PO reference -- see
    ``postgres_procurement.py``'s GI-attempt query). Reservation is the
    deterministic GI attribution path W6.1 didn't have; this module uses it
    to supersede W6.1's ``UNRESOLVED_PENDING_RESERVATION`` wherever a
    reservation resolves.
  * MRP consolidation (multiple reservations -> one PR) is real but rare:
    exactly 2 PR items in this dataset are each referenced by more than one
    distinct reservation. No approved deterministic allocation rule exists
    anywhere in this repository or its requirements docs, so those cases are
    marked ``CONSOLIDATION_UNRESOLVED`` -- the PR link is shown (it's real
    and known), but PO/GR/quantity fields are left unpopulated rather than
    guessing an allocation split. See §7 of the implementation plan.

W2.4 OAR scope is applied here (the I13 boundary for the *complete* ledger),
via the existing ``classify_material_scope`` -- not reimplemented, and not
applied a second, different way.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from app.initiatives.i13.models import (
    GiLinkStatus,
    GrLinkStatus,
    PartialLedgerEntry,
    ReservationLedgerEntry,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.movements import ISSUE_TYPES, event_dates, net_quantity
from app.initiatives.i13.procurement_chain import build_procurement_chain, derive_lifecycle_status
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository
from app.integrations.sap.postgres_reservation import PostgresReservationRepository
from app.shared.material_scope import MaterialScope, classify_material_scope

Row = dict[str, Any]

# link statuses where the procurement picture is itself genuinely unknown --
# the derived direct-store/procurement issue split must not be computed from
# a received_quantity we don't actually have (see §10 in the module docstring).
_PROCUREMENT_UNKNOWN = frozenset(
    {ReservationPrLinkStatus.PR_REFERENCE_UNRESOLVED, ReservationPrLinkStatus.CONSOLIDATION_UNRESOLVED}
)


def _attach_reservation(
    reservation: Row,
    procurement_index: dict[tuple[str, str], list[PartialLedgerEntry]],
    consolidation_pr_keys: set[tuple[str, str]],
    gi_by_reservation: dict[tuple[str, str], list[Row]],
    material_scope: MaterialScope,
) -> list[ReservationLedgerEntry]:
    rsnum, rspos = reservation["Rsnum"], reservation["Rspos"]
    material, plant = reservation["Matnr"], reservation["Werks"]
    reservation_quantity = reservation.get("Bdmng") or Decimal("0")
    requirement_date = reservation.get("Bdter")
    banfn, bnfpo = reservation.get("Banfn"), reservation.get("Bnfpo")

    # GI -> Reservation: always resolvable at this grain (RSNUM/RSPOS is
    # inherent to the reservation row itself) -- zero rows found is a
    # resolved fact ("not issued yet"), not an unresolved link.
    gi_rows = gi_by_reservation.get((rsnum, rspos), [])
    issued_quantity = net_quantity(gi_rows, ISSUE_TYPES)
    first_issue_date, last_issue_date = event_dates(gi_rows, ISSUE_TYPES)
    gi_link_status = GiLinkStatus.LINKED

    if not banfn or not bnfpo:
        reservation_pr_link_status = ReservationPrLinkStatus.NO_PR_REFERENCE
        matched_procurement: list[PartialLedgerEntry | None] = [None]
    elif (banfn, bnfpo) in consolidation_pr_keys:
        reservation_pr_link_status = ReservationPrLinkStatus.CONSOLIDATION_UNRESOLVED
        matched_procurement = [None]
    else:
        found = procurement_index.get((banfn, bnfpo), [])
        if found:
            reservation_pr_link_status = ReservationPrLinkStatus.LINKED
            matched_procurement = list(found)
        else:
            reservation_pr_link_status = ReservationPrLinkStatus.PR_REFERENCE_UNRESOLVED
            matched_procurement = [None]

    entries: list[ReservationLedgerEntry] = []
    multi = len(matched_procurement) > 1
    for index, proc in enumerate(matched_procurement):
        if proc is not None:
            po_number, po_item = proc.po_number, proc.po_item
            ordered_quantity, received_quantity = proc.ordered_quantity, proc.received_quantity
            first_gr_date, last_gr_date = proc.first_gr_date, proc.last_gr_date
            gr_link_status = proc.gr_link_status
        else:
            po_number = po_item = None
            ordered_quantity = received_quantity = None
            first_gr_date = last_gr_date = None
            gr_link_status = GrLinkStatus.NOT_APPLICABLE

        if reservation_pr_link_status in _PROCUREMENT_UNKNOWN:
            procurement_issued_quantity = None
            direct_store_issued_quantity = None
        else:
            received_for_split = received_quantity or Decimal("0")
            procurement_issued_quantity = min(issued_quantity, received_for_split)
            direct_store_issued_quantity = max(issued_quantity - received_for_split, Decimal("0"))

        lifecycle_status = derive_lifecycle_status(
            has_po=po_number is not None,
            received_quantity=received_quantity or Decimal("0"),
            ordered_quantity=ordered_quantity,
            issued_quantity=issued_quantity,
            gi_link_status=gi_link_status,
            requested_quantity=reservation_quantity,
        )

        entries.append(
            ReservationLedgerEntry(
                ledger_id=f"RESCHAIN-{rsnum}-{rspos}" + (f"-{index}" if multi else ""),
                reservation_number=rsnum,
                reservation_item=rspos,
                material=material,
                plant=plant,
                reservation_quantity=reservation_quantity,
                requirement_date=requirement_date,
                pr_number=banfn,
                pr_item=bnfpo,
                po_number=po_number,
                po_item=po_item,
                ordered_quantity=ordered_quantity,
                received_quantity=received_quantity,
                issued_quantity=issued_quantity,
                first_gr_date=first_gr_date,
                last_gr_date=last_gr_date,
                first_issue_date=first_issue_date,
                last_issue_date=last_issue_date,
                procurement_issued_quantity=procurement_issued_quantity,
                direct_store_issued_quantity=direct_store_issued_quantity,
                lifecycle_status=lifecycle_status,
                reservation_pr_link_status=reservation_pr_link_status,
                gr_link_status=gr_link_status,
                gi_link_status=gi_link_status,
                gi_link_reason=None if gi_rows else "No goods-issue movement carries this reservation's RSNUM/RSPOS yet.",
                material_scope=material_scope,
            )
        )
    return entries


def build_reservation_ledger(
    reservation_repository: PostgresReservationRepository,
    procurement_repository: PostgresProcurementRepository,
    *,
    material_scope_index: dict[tuple[str, str], str | None],
    material: str | None = None,
    plant: str | None = None,
    reservation_number: str | None = None,
    pr_number: str | None = None,
    include_out_of_scope: bool = False,
) -> list[ReservationLedgerEntry]:
    """The W6.2 entry point: Reservation -> PR -> PO -> GR -> GI.

    ``material_scope_index`` is (material, plant) -> raw DISMM string (see
    ``postgres_material.fetch_material_scope_index``) -- passed in rather
    than fetched here, so callers control its scope/filters the same way
    they control the reservation/procurement fetches.
    """
    reservations = reservation_repository.get_reservations(
        reservation_number=reservation_number, pr_number=pr_number, material=material, plant=plant
    )

    pr_key_counts = Counter(
        (r["Banfn"], r["Bnfpo"]) for r in reservations if r.get("Banfn") and r.get("Bnfpo")
    )
    consolidation_pr_keys = {key for key, count in pr_key_counts.items() if count > 1}

    # W6.1, reused unchanged -- one batched call, not one per reservation.
    procurement_entries = build_procurement_chain(
        procurement_repository, material=material, plant=plant, pr_number=pr_number
    )
    procurement_index: dict[tuple[str, str], list[PartialLedgerEntry]] = defaultdict(list)
    for entry in procurement_entries:
        if entry.pr_number and entry.pr_item:
            procurement_index[(entry.pr_number, entry.pr_item)].append(entry)

    gi_rows = reservation_repository.get_goods_issue_by_reservation(reservation_number=reservation_number)
    gi_by_reservation: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in gi_rows:
        gi_by_reservation[(row["Rsnum"], row["Rspos"])].append(row)

    entries: list[ReservationLedgerEntry] = []
    for reservation in reservations:
        scope = classify_material_scope(material_scope_index.get((reservation["Matnr"], reservation["Werks"])))
        if not include_out_of_scope and scope is not MaterialScope.OAR:
            continue
        entries.extend(_attach_reservation(reservation, procurement_index, consolidation_pr_keys, gi_by_reservation, scope))
    return entries
