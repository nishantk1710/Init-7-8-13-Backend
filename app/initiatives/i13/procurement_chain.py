"""W6.1: deterministic PR -> PO -> GR -> GI stitching, from real Postgres
data, WITHOUT the reservation leg (that's W6.2's job -- see the module note
at the bottom of this file for exactly how W6.2 should extend this).

Deterministic only: every join here uses an explicit SAP document reference
already present in the loaded data (BANFN/BNFPO, EBELN/EBELP). Nothing here
infers a relationship from matching material, plant, quantity or dates --
where a reference doesn't resolve, the record is reported unresolved, never
guessed into a match (see ``PrPoLinkStatus``/``GiLinkStatus`` in models.py).

Reuses, rather than reimplements, W3.5's shared movement rules
(``app.initiatives.i13.movements``): ``RECEIPT_TYPES``, ``ISSUE_TYPES``,
reversal-aware ``net_quantity``, and ``event_dates`` (promoted out of
``ledger.py`` this session so both the CSV-backed W6.1/W6.2 ledger and this
Postgres-backed one share one definition of "first/last event date").

Measured facts about this dataset that shape the design below (see the W6.1
implementation report for the full numbers):
  * EBAN's own key (BANFN, BNFPO) is 100% unique; EKPO's own key
    (EBELN, EBELP) is 100% unique -- no raw duplicate source rows found.
  * A PR item CAN be referenced by multiple PO items -- ~1,844 PR items in
    this dataset resolve to more than one distinct PO document (real
    multi-sourcing, not ambiguity). The PR -> PO join is one-to-many.
  * ~31% of EKPO rows carrying a PR reference reference a PR key that does
    not exist in the currently-loaded EBAN extract (consistent with EBAN's
    documented plant-1300-only coverage). Reported as
    ``PR_REFERENCE_UNRESOLVED``, never dropped or guessed.
  * No issue movement (201/261) in this dataset carries a PO reference
    (0 of 47,635, measured) -- so GI linkage is ``UNRESOLVED_PENDING_RESERVATION``
    for every entry today. This is the expected, honest W6.1 outcome, not a
    bug: it is exactly the gap W6.2's reservation leg exists to close.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal
from typing import Any

from app.initiatives.i13.models import (
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    PartialLedgerEntry,
    PrPoLinkStatus,
    ProcurementChainDiagnostics,
)
from app.initiatives.i13.movements import ISSUE_TYPES, RECEIPT_TYPES, event_dates, net_quantity
from app.integrations.sap.postgres_procurement import PostgresProcurementRepository

Row = dict[str, Any]


def derive_lifecycle_status(
    *,
    has_po: bool,
    received_quantity: Decimal,
    ordered_quantity: Decimal | None,
    issued_quantity: Decimal | None,
    gi_link_status: GiLinkStatus,
    requested_quantity: Decimal | None = None,
) -> LifecycleStatus:
    """Shared by W6.1 (``procurement_chain.py``) and W6.2
    (``reservation_ledger.py``). The ``not has_po`` + deterministic-GI branch
    only ever fires from W6.2: a reservation-anchored entry can have goods
    issued directly from stock with no PR/PO at all (§10), and
    ``gi_link_status`` is never ``LINKED`` with ``has_po=False`` in W6.1
    (a PR-only entry always sets it ``NOT_APPLICABLE``), so this is additive,
    not a behaviour change for existing W6.1 callers.

    ``requested_quantity`` (W6.1 never passes it -- no such quantity exists
    at the PO-item grain) lets the no-PO branch tell PARTIALLY_ISSUED from
    ISSUED against the reservation's own requested quantity, since there is
    no ``received_quantity`` to compare against there.
    """
    has_deterministic_gi = gi_link_status is GiLinkStatus.LINKED and issued_quantity is not None

    if not has_po:
        if has_deterministic_gi and issued_quantity > 0:
            # Fulfilled without procurement (e.g. direct-store issue against
            # a reservation).
            if requested_quantity and issued_quantity < requested_quantity:
                return LifecycleStatus.PARTIALLY_ISSUED
            return LifecycleStatus.ISSUED
        return LifecycleStatus.PR_CREATED
    if received_quantity <= 0:
        return LifecycleStatus.ORDERED
    if ordered_quantity and received_quantity < ordered_quantity:
        return LifecycleStatus.PARTIALLY_RECEIVED
    # received >= ordered (or ordered_quantity unknown/zero) from here.
    # GI only ever promotes the status past RECEIVED when a genuine
    # deterministic link exists -- an unresolved link must never be read as
    # "not issued" (see §11: "do not incorrectly label the row ISSUED/UNISSUED").
    if has_deterministic_gi:
        if issued_quantity <= 0:
            return LifecycleStatus.RECEIVED
        if issued_quantity < received_quantity:
            return LifecycleStatus.PARTIALLY_ISSUED
        return LifecycleStatus.ISSUED
    return LifecycleStatus.RECEIVED


def _build_po_item_entry(
    po: Row,
    pr_by_key: dict[tuple[str, str], Row],
    gr_by_po_item: dict[tuple[str, str], list[Row]],
    gi_by_po_item: dict[tuple[str, str], list[Row]],
) -> PartialLedgerEntry:
    ebeln, ebelp = po["Ebeln"], po["Ebelp"]
    banfn, bnfpo = po.get("Banfn"), po.get("Bnfpo")
    material, plant = po["Matnr"], po["Werks"]
    ordered_quantity = po["Menge"]

    if not banfn or not bnfpo:
        pr_po_link_status = PrPoLinkStatus.NO_PR_REFERENCE
        pr = None
    else:
        pr = pr_by_key.get((banfn, bnfpo))
        pr_po_link_status = PrPoLinkStatus.LINKED if pr is not None else PrPoLinkStatus.PR_REFERENCE_UNRESOLVED

    pr_quantity = pr["Menge"] if pr is not None else None

    gr_rows = gr_by_po_item.get((ebeln, ebelp), [])
    received_quantity = net_quantity(gr_rows, RECEIPT_TYPES)
    first_gr_date, last_gr_date = event_dates(gr_rows, RECEIPT_TYPES)
    gr_link_status = GrLinkStatus.RECEIVED if gr_rows else GrLinkStatus.NO_RECEIPTS

    gi_rows = gi_by_po_item.get((ebeln, ebelp), [])
    if gi_rows:
        issued_quantity: Decimal | None = net_quantity(gi_rows, ISSUE_TYPES)
        first_issue_date, last_issue_date = event_dates(gi_rows, ISSUE_TYPES)
        gi_link_status = GiLinkStatus.LINKED
        gi_link_reason = None
    else:
        issued_quantity = None
        first_issue_date = last_issue_date = None
        gi_link_status = GiLinkStatus.UNRESOLVED_PENDING_RESERVATION
        gi_link_reason = (
            "No goods-issue movement carries an explicit PO reference for this "
            "item. Deterministic GI attribution requires the reservation leg (W6.2)."
        )

    lifecycle_status = derive_lifecycle_status(
        has_po=True,
        received_quantity=received_quantity,
        ordered_quantity=ordered_quantity,
        issued_quantity=issued_quantity,
        gi_link_status=gi_link_status,
    )

    return PartialLedgerEntry(
        ledger_id=f"POCHAIN-{ebeln}-{ebelp}",
        material=material,
        plant=plant,
        pr_number=banfn,
        pr_item=bnfpo,
        po_number=ebeln,
        po_item=ebelp,
        pr_quantity=pr_quantity,
        ordered_quantity=ordered_quantity,
        received_quantity=received_quantity,
        issued_quantity=issued_quantity,
        first_gr_date=first_gr_date,
        last_gr_date=last_gr_date,
        first_issue_date=first_issue_date,
        last_issue_date=last_issue_date,
        lifecycle_status=lifecycle_status,
        pr_po_link_status=pr_po_link_status,
        gr_link_status=gr_link_status,
        gi_link_status=gi_link_status,
        gi_link_reason=gi_link_reason,
    )


def _build_pr_only_entry(pr: Row) -> PartialLedgerEntry:
    return PartialLedgerEntry(
        ledger_id=f"PRCHAIN-{pr['Banfn']}-{pr['Bnfpo']}",
        material=pr["Matnr"],
        plant=pr["Werks"],
        pr_number=pr["Banfn"],
        pr_item=pr["Bnfpo"],
        po_number=None,
        po_item=None,
        pr_quantity=pr["Menge"],
        ordered_quantity=None,
        received_quantity=Decimal("0"),
        issued_quantity=None,
        first_gr_date=None,
        last_gr_date=None,
        first_issue_date=None,
        last_issue_date=None,
        lifecycle_status=LifecycleStatus.PR_CREATED,
        pr_po_link_status=PrPoLinkStatus.NO_PO_YET,
        gr_link_status=GrLinkStatus.NOT_APPLICABLE,
        gi_link_status=GiLinkStatus.NOT_APPLICABLE,
        gi_link_reason=None,
    )


def build_procurement_chain(
    repository: PostgresProcurementRepository,
    *,
    material: str | None = None,
    plant: str | None = None,
    pr_number: str | None = None,
    po_number: str | None = None,
) -> list[PartialLedgerEntry]:
    """The W6.1 entry point. One entry per PO item, plus one PR-only entry
    for every requisition line no PO yet references (in scope).

    ``po_number`` narrows to one PO's own items only -- PR-only rows outside
    that PO are not relevant to "show me this PO" and are omitted. Any other
    filter combination also returns PR-only rows in scope, since a PR with no
    PO is a real, reportable lifecycle state (``PR_CREATED``), not noise.

    This is the seam W6.2 extends: ``attach_reservation(entries)`` (or
    equivalent) can wrap this function's output and layer Reservation ->
    PR linkage on top without changing anything here -- the PR/PO/GR/GI
    fields and link-status vocabulary are already final.
    """
    if po_number:
        po_items = repository.get_purchase_order_items(po_number=po_number)
        pr_keys = {(po["Banfn"], po["Bnfpo"]) for po in po_items if po.get("Banfn") and po.get("Bnfpo")}
        prs: list[Row] = []
        for banfn, _ in {key for key in pr_keys}:
            prs.extend(repository.get_purchase_requisitions(pr_number=banfn))
        include_pr_only_rows = False
    else:
        po_items = repository.get_purchase_order_items(pr_number=pr_number, material=material, plant=plant)
        prs = repository.get_purchase_requisitions(pr_number=pr_number, material=material, plant=plant)
        include_pr_only_rows = True

    pr_by_key: dict[tuple[str, str], Row] = {(pr["Banfn"], pr["Bnfpo"]): pr for pr in prs}

    gr_rows = repository.get_goods_receipt_history(po_number=po_number)
    gr_by_po_item: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in gr_rows:
        gr_by_po_item[(row["Ebeln"], row["Ebelp"])].append(row)

    gi_candidates = repository.get_deterministic_gi_candidates(po_number=po_number)
    gi_by_po_item: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for row in gi_candidates:
        gi_by_po_item[(row["Ebeln"], row["Ebelp"])].append(row)

    entries: list[PartialLedgerEntry] = []
    linked_pr_keys: set[tuple[str, str]] = set()
    for po in po_items:
        entry = _build_po_item_entry(po, pr_by_key, gr_by_po_item, gi_by_po_item)
        entries.append(entry)
        if entry.pr_number and entry.pr_item:
            linked_pr_keys.add((entry.pr_number, entry.pr_item))

    if include_pr_only_rows:
        for pr in prs:
            key = (pr["Banfn"], pr["Bnfpo"])
            if key in linked_pr_keys:
                continue
            entries.append(_build_pr_only_entry(pr))

    return entries


def _find_duplicate_keys(rows: list[Row], key_fields: tuple[str, str]) -> list[tuple[str, str]]:
    counts = Counter((row.get(key_fields[0]), row.get(key_fields[1])) for row in rows)
    return [key for key, count in counts.items() if count > 1 and key[0] and key[1]]


def compute_chain_diagnostics(
    repository: PostgresProcurementRepository, *, material: str | None = None, plant: str | None = None
) -> ProcurementChainDiagnostics:
    """Visibility into unmatched/ambiguous source records -- FRS §13/§19.15.
    A separate, explicitly-called function rather than folded into every
    ``build_procurement_chain`` call, since it scans the full PR/PO
    population rather than pre-aggregating like the ledger builder does.
    """
    prs = repository.get_purchase_requisitions(material=material, plant=plant)
    po_items = repository.get_purchase_order_items(material=material, plant=plant)

    duplicate_pr_keys = _find_duplicate_keys(prs, ("Banfn", "Bnfpo"))
    duplicate_po_keys = _find_duplicate_keys(po_items, ("Ebeln", "Ebelp"))

    pr_keys = {(pr["Banfn"], pr["Bnfpo"]) for pr in prs}
    po_documents_by_pr: dict[tuple[str, str], set[str]] = defaultdict(set)
    po_with_no_pr_reference = 0
    po_with_unresolved_pr_reference = 0
    for po in po_items:
        banfn, bnfpo = po.get("Banfn"), po.get("Bnfpo")
        if not banfn or not bnfpo:
            po_with_no_pr_reference += 1
            continue
        key = (banfn, bnfpo)
        po_documents_by_pr[key].add(po["Ebeln"])
        if key not in pr_keys:
            po_with_unresolved_pr_reference += 1

    # Bucket only over pr_keys (real, loaded PR rows) -- po_documents_by_pr
    # also contains keys referenced by a PO but never loaded as a PR (the
    # PR_REFERENCE_UNRESOLVED cases, already counted separately above).
    # Summing over po_documents_by_pr.values() directly here double-counted
    # those as if they were real PR items too, and the three buckets no
    # longer summed to pr_items_total -- caught by real-data validation,
    # not by the (too-narrow) unit test.
    pr_with_no_po = sum(1 for key in pr_keys if key not in po_documents_by_pr)
    pr_with_single_po = sum(1 for key in pr_keys if len(po_documents_by_pr.get(key, ())) == 1)
    pr_with_multiple_po = sum(1 for key in pr_keys if len(po_documents_by_pr.get(key, ())) > 1)

    return ProcurementChainDiagnostics(
        pr_items_total=len(prs),
        pr_items_with_no_po=pr_with_no_po,
        pr_items_with_single_po=pr_with_single_po,
        pr_items_with_multiple_po=pr_with_multiple_po,
        po_items_total=len(po_items),
        po_items_with_no_pr_reference=po_with_no_pr_reference,
        po_items_with_unresolved_pr_reference=po_with_unresolved_pr_reference,
        duplicate_pr_keys=duplicate_pr_keys,
        duplicate_po_keys=duplicate_po_keys,
    )
