"""PR / PO-item / PO-history data from Postgres, for W6.1 (partial
utilisation ledger: PR -> PO -> GR, no reservation leg yet).

Reads three real, already-loaded extract tables -- read-only, no writes, no
generated data:

  * ``raw_eban``  (EBAN-equivalent)  -- purchase requisition items
  * ``raw_ekpo``  (EKPO-equivalent)  -- purchase order items, carrying the
    PR reference (``purchase_requisition``/``item_of_requisition``) that
    ``procurement_chain.py`` uses for the deterministic PR -> PO join
  * ``raw_ekbe``  (EKBE-equivalent)  -- PO history. ``po_history_category='E'``
    is SAP's own "goods receipt" category (movement types 101/102/122 in
    this dataset) -- a more reliable PO-item join than filtering
    ``raw_mseg`` directly, since ``raw_ekbe`` IS the purpose-built PO-history
    view and its (purchasing_document, item) resolves to a real ``raw_ekpo``
    row 99.2% of the time (measured).

Row shapes are normalized to what ``app.initiatives.i13.movements`` and
``procurement_chain.py`` already expect (``Ebeln``/``Ebelp``/``Banfn``/
``Bnfpo``/``Matnr``/``Werks``/``Menge``/``Bwart``/``BudatMkpf``), so the
existing reversal-netting/date logic applies unchanged.

Known data-shape facts, measured against this dataset, not assumed (see the
W6.1 implementation report for the full numbers):
  * ``raw_eban``'s own key (purchase_requisition, item_of_requisition) is
    100% unique -- no duplicate PR-item rows in this extract.
  * ``raw_ekpo``'s own key (purchasing_document, item) is 100% unique.
  * A PR item can legitimately be referenced by MULTIPLE PO items (real
    multi-sourcing/split procurement, not a data defect) -- ~1,844 PR items
    in this dataset resolve to more than one distinct PO document. The
    PR -> PO join is therefore one-to-many, not one-to-one.
  * ~31% of ``raw_ekpo`` rows that DO carry a PR reference reference a PR key
    that does not exist in the currently-loaded ``raw_eban`` (consistent
    with EBAN's documented plant-1300-only extract coverage -- see
    ``app/seed/manifest.py``). This is reported as ``PR_REFERENCE_UNRESOLVED``,
    never silently dropped or guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.integrations.sap._request_cache import memoize_per_instance
from app.shared.plant_scope import sql_predicate

Row = dict[str, Any]


def _decimal(raw: str | None) -> Decimal:
    if raw in (None, ""):
        return Decimal("0")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal("0")


def _text_or_none(raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _date_or_none(raw: str | None) -> date | None:
    if raw in (None, ""):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None

# Plants 1300 and 1500 only -- the team lead's ruling of 2026-09-21. Applied in
# the WHERE clause rather than over the returned rows so counts, sums and aging
# bands are computed on the scoped population to begin with; filtering after
# aggregation is where an out-of-scope quantity leaks into a total. Built from
# app/shared/plant_scope.py -- never write the codes out here.
_PLANT_SCOPE = sql_predicate("plant")
_PLANT_SCOPE_M = sql_predicate("m.plant")




# --- Purchase requisitions (EBAN-equivalent) --------------------------------

_PR_QUERY = """
    SELECT purchase_requisition, item_of_requisition, material, plant,
           quantity_requested, requisition_date, purchase_order, purchase_order_item
    FROM raw_eban
    WHERE purchase_requisition <> '' AND material <> '' AND plant <> '' AND {plant_scope}
      {pr_filter}
      {material_filter}
      {plant_filter}
"""


def _to_pr_row(record: Any) -> Row:
    # Same "0" zero-default sentinel as EKPO's Bnfpo (see _to_po_item_row):
    # purchase_order_item reads "0" whenever purchase_order itself is NULL,
    # never otherwise (measured: 5,518/5,518 co-occurrences, 0 counter-
    # examples) -- guarded the same way.
    ebeln = _text_or_none(record.purchase_order)
    ebelp = _text_or_none(record.purchase_order_item) if ebeln else None
    return {
        "Banfn": record.purchase_requisition.strip(),
        "Bnfpo": record.item_of_requisition.strip(),
        "Matnr": record.material.strip(),
        "Werks": record.plant.strip(),
        "Menge": _decimal(record.quantity_requested),
        "Badat": _date_or_none(record.requisition_date),
        # EBAN's own PO reference -- a single-valued denormalized field that
        # cannot represent a PR split across multiple POs (see module
        # docstring). Kept for diagnostics only; procurement_chain.py's PR ->
        # PO join is driven from the EKPO side, never from this field.
        "Ebeln": ebeln,
        "Ebelp": ebelp,
    }


def fetch_purchase_requisitions(
    db: Session, *, pr_number: str | None = None, material: str | None = None, plant: str | None = None
) -> list[Row]:
    params: dict[str, str] = {}
    pr_filter = ""
    if pr_number:
        pr_filter = "AND purchase_requisition = :pr_number"
        params["pr_number"] = pr_number
    material_filter = ""
    if material:
        material_filter = "AND material = :material"
        params["material"] = material
    plant_filter = ""
    if plant:
        plant_filter = "AND plant = :plant"
        params["plant"] = plant

    query = text(
        _PR_QUERY.format(
            pr_filter=pr_filter,
            material_filter=material_filter,
            plant_filter=plant_filter,
            plant_scope=_PLANT_SCOPE,
        )
    )
    return [_to_pr_row(r) for r in db.execute(query, params).fetchall()]


# --- Purchase order items (EKPO-equivalent) ---------------------------------

_PO_ITEM_QUERY = """
    SELECT purchasing_document, item, purchase_requisition, item_of_requisition,
           material, plant, order_quantity
    FROM raw_ekpo
    WHERE purchasing_document <> '' AND material <> '' AND plant <> '' AND {plant_scope}
      {po_filter}
      {pr_filter}
      {material_filter}
      {plant_filter}
"""


def _to_po_item_row(record: Any) -> Row:
    # BANFN is genuinely NULL (not '') when a PO item carries no PR
    # reference -- but BNFPO on those same rows is the literal string "0",
    # SAP's zero-default for a numeric-like item counter, not an empty
    # string. Measured: "0" occurs ONLY alongside a NULL BANFN (0 counter-
    # examples), so it's a safe, confirmed sentinel here, not a guess --
    # without this, a "no PR" row would misreport pr_item="0" instead of
    # None even though pr_number is correctly None.
    banfn = _text_or_none(record.purchase_requisition)
    bnfpo = _text_or_none(record.item_of_requisition) if banfn else None
    return {
        "Ebeln": record.purchasing_document.strip(),
        "Ebelp": record.item.strip(),
        "Banfn": banfn,
        "Bnfpo": bnfpo,
        "Matnr": record.material.strip(),
        "Werks": record.plant.strip(),
        "Menge": _decimal(record.order_quantity),
    }


def fetch_purchase_order_items(
    db: Session,
    *,
    po_number: str | None = None,
    pr_number: str | None = None,
    material: str | None = None,
    plant: str | None = None,
) -> list[Row]:
    params: dict[str, str] = {}
    po_filter = ""
    if po_number:
        po_filter = "AND purchasing_document = :po_number"
        params["po_number"] = po_number
    pr_filter = ""
    if pr_number:
        pr_filter = "AND purchase_requisition = :pr_number"
        params["pr_number"] = pr_number
    material_filter = ""
    if material:
        material_filter = "AND material = :material"
        params["material"] = material
    plant_filter = ""
    if plant:
        plant_filter = "AND plant = :plant"
        params["plant"] = plant

    query = text(
        _PO_ITEM_QUERY.format(
            po_filter=po_filter,
            pr_filter=pr_filter,
            material_filter=material_filter,
            plant_filter=plant_filter,
            plant_scope=_PLANT_SCOPE,
        )
    )
    return [_to_po_item_row(r) for r in db.execute(query, params).fetchall()]


# --- Goods-receipt history (EKBE-equivalent, category 'E') ------------------

_GR_HISTORY_QUERY = """
    SELECT purchasing_document, item, movement_type, quantity, posting_date
    FROM raw_ekbe
    WHERE po_history_category = 'E' AND purchasing_document <> '' AND posting_date <> ''
      {po_filter}
"""


def _to_gr_row(record: Any) -> Row:
    return {
        "Ebeln": record.purchasing_document.strip(),
        "Ebelp": record.item.strip(),
        "Bwart": (record.movement_type or "").strip(),
        "Menge": _decimal(record.quantity),
        "BudatMkpf": _date_or_none(record.posting_date),
    }


def fetch_goods_receipt_history(db: Session, *, po_number: str | None = None) -> list[Row]:
    params: dict[str, str] = {}
    po_filter = ""
    if po_number:
        po_filter = "AND purchasing_document = :po_number"
        params["po_number"] = po_number

    query = text(_GR_HISTORY_QUERY.format(po_filter=po_filter))
    return [_to_gr_row(r) for r in db.execute(query, params).fetchall()]


# --- Goods-issue linkage attempt (W6.1's one deterministic check) ----------
#
# The ONLY hard reference an issue movement (201/261) could carry back to a
# procurement line without the reservation leg is its own PO reference
# (Ebeln/Ebelp) -- SAP populates this for consignment/direct-PO-consumption
# scenarios, though not for ordinary cost-center or order-based issues.
# Measured against this dataset: 0 of 47,635 real 201/261 rows carry one
# (raw_mseg.purchase_order is populated on 100% of receipts (101) but 0% of
# issues (201/261) -- see the W6.1 implementation report). This query is a
# genuine, always-current check, not a hard-coded "always unresolved" --
# if SAP data with this field populated is ever loaded, it starts resolving.
_GI_LINK_ATTEMPT_QUERY = """
    SELECT m.material, m.plant, m.movement_type, m.quantity, h.posting_date,
           m.purchase_order, m.item
    FROM raw_mseg m
    JOIN raw_mkpf h
      ON m.material_document = h.material_document
     AND m.material_doc_year = h.material_doc_year
    WHERE m.movement_type IN ('201', '261')
      AND m.purchase_order <> '' AND m.item <> ''
      AND {plant_scope}
      AND h.posting_date <> ''
      {po_filter}
"""


def _to_gi_link_row(record: Any) -> Row:
    return {
        "Ebeln": record.purchase_order.strip(),
        "Ebelp": record.item.strip(),
        "Bwart": (record.movement_type or "").strip(),
        "Menge": _decimal(record.quantity),
        "BudatMkpf": _date_or_none(record.posting_date),
    }


def fetch_deterministic_gi_candidates(db: Session, *, po_number: str | None = None) -> list[Row]:
    """Issue movements carrying an explicit PO reference -- see the block
    comment above. Deliberately narrow in scope to W6.1; W6.2 replaces this
    with reservation-based GI attribution rather than extending it."""
    params: dict[str, str] = {}
    po_filter = ""
    if po_number:
        po_filter = "AND m.purchase_order = :po_number"
        params["po_number"] = po_number

    query = text(_GI_LINK_ATTEMPT_QUERY.format(po_filter=po_filter, plant_scope=_PLANT_SCOPE_M))
    return [_to_gi_link_row(r) for r in db.execute(query, params).fetchall()]


@dataclass
class PostgresProcurementRepository:
    """Thin repository wrapper -- one object, one held session, four reads.

    Memoized per instance (see ``_request_cache.py``) -- construct one per
    request, never share across requests. ``build_reservation_ledger`` calls
    ``build_procurement_chain`` internally, and W6.2's composed services
    (``build_exception_queue``, ``compute_watch_metrics``, ``build_summary``)
    each build a reservation ledger of their own -- without this cache, one
    ``GET /api/i13/summary`` re-ran every fetch here multiple times over the
    full tenant (measured: 50+ seconds; see ``_request_cache.py``'s docstring
    for the exact call-graph duplication).
    """

    db: Session
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    @memoize_per_instance
    def get_purchase_requisitions(
        self, *, pr_number: str | None = None, material: str | None = None, plant: str | None = None
    ) -> list[Row]:
        return fetch_purchase_requisitions(self.db, pr_number=pr_number, material=material, plant=plant)

    @memoize_per_instance
    def get_purchase_order_items(
        self,
        *,
        po_number: str | None = None,
        pr_number: str | None = None,
        material: str | None = None,
        plant: str | None = None,
    ) -> list[Row]:
        return fetch_purchase_order_items(
            self.db, po_number=po_number, pr_number=pr_number, material=material, plant=plant
        )

    @memoize_per_instance
    def get_goods_receipt_history(self, *, po_number: str | None = None) -> list[Row]:
        return fetch_goods_receipt_history(self.db, po_number=po_number)

    @memoize_per_instance
    def get_deterministic_gi_candidates(self, *, po_number: str | None = None) -> list[Row]:
        return fetch_deterministic_gi_candidates(self.db, po_number=po_number)
