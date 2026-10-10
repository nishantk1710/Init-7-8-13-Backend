"""Repositories over one batch of materials, read from the build's work tables.

Same methods, arguments and row shapes as ``PostgresMovementRepository``,
``PostgresProcurementRepository`` and ``PostgresReservationRepository`` -- the
I13 builders take any of them -- but every read is limited to the materials
from ``lo`` to ``hi`` inclusive and comes from the indexed copies in
``work_tables.py`` rather than the ``n_*`` views. A builder handed these sees
exactly the slice of the tenant one batch covers, and computes that slice as
it would have computed it inside the whole.

Rows are converted by the existing modules' own converters, so a value means
the same thing whichever repository read it.

Three reads behave differently from the view-backed ones, all because the work
tables hold plants 1300/1500 only:

* goods receipts (EKBE) are the in-scope PO lines' only -- the only ones the
  procurement chain joins to;
* goods issues by reservation (MSEG) are in-scope plants' only -- the only
  ones an in-scope reservation is issued from;
* issues by PO reference likewise.

``tests/i13/test_snapshot_store.py`` builds a tenant both ways and compares.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.initiatives.i13.snapshot_store.work_tables import WorkTables
from app.integrations.sap._request_cache import memoize_per_instance
from app.integrations.sap.postgres_movements import _decimal, _to_movement_row
from app.integrations.sap.postgres_procurement import (
    _to_gi_link_row,
    _to_gr_row,
    _to_po_item_row,
    _to_pr_row,
)
from app.integrations.sap.postgres_reservation import (
    _to_gi_by_reservation_row,
    _to_reservation_row,
    _with_uat_overlay,
)

Row = dict[str, Any]


@dataclass(frozen=True)
class Batch:
    """Materials ``lo`` to ``hi`` inclusive, as SQL orders them. ``plant``
    narrows further (a one-material refresh)."""

    lo: str
    hi: str
    plant: str | None = None

    def predicate(self, column: str = "material", plant_column: str = "plant") -> str:
        clause = f"{column} >= :b_lo AND {column} <= :b_hi"
        if self.plant:
            clause += f" AND {plant_column} = :b_plant"
        return clause

    def params(self) -> dict[str, str]:
        params = {"b_lo": self.lo, "b_hi": self.hi}
        if self.plant:
            params["b_plant"] = self.plant
        return params

    def holds(self, material: str | None, plant: str | None = None) -> bool:
        if material is None or not (self.lo <= material <= self.hi):
            return False
        return not self.plant or plant is None or plant == self.plant


def _filters(spec: dict[str, tuple[str, str | None]]) -> tuple[str, dict[str, str]]:
    """``AND col = :p`` for every spec entry that has a value."""
    clauses, params = [], {}
    for param, (column, value) in spec.items():
        if value:
            clauses.append(f"AND {column} = :{param}")
            params[param] = value
    return " ".join(clauses), params


@dataclass
class BatchMovementRepository:
    db: Session
    work: WorkTables
    batch: Batch
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    @memoize_per_instance
    def get_movement_history(self, *, material: str | None = None, plant: str | None = None) -> list[Row]:
        extra, params = _filters({"material": ("material", material), "plant": ("plant", plant)})
        rows = self.db.execute(
            text(
                "SELECT material, plant, movement_type, quantity, posting_date, purchase_order, item "
                f"FROM {self.work.mov} WHERE {self.batch.predicate()} {extra}"
            ),
            {**self.batch.params(), **params},
        ).fetchall()
        return [_to_movement_row(r) for r in rows]

    @memoize_per_instance
    def get_current_stock(
        self, *, material: str | None = None, plant: str | None = None
    ) -> dict[tuple[str, str], Decimal]:
        extra, params = _filters({"material": ("material", material), "plant": ("plant", plant)})
        rows = self.db.execute(
            text(
                f"SELECT material, plant, unrestricted FROM {self.work.stock} "
                f"WHERE {self.batch.predicate()} {extra}"
            ),
            {**self.batch.params(), **params},
        ).fetchall()
        totals: dict[tuple[str, str], Decimal] = {}
        for r in rows:
            key = (r.material.strip(), r.plant.strip())
            totals[key] = totals.get(key, Decimal(0)) + _decimal(r.unrestricted)
        return totals


@dataclass
class BatchProcurementRepository:
    db: Session
    work: WorkTables
    batch: Batch
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    @memoize_per_instance
    def get_purchase_requisitions(
        self, *, pr_number: str | None = None, material: str | None = None, plant: str | None = None
    ) -> list[Row]:
        extra, params = _filters(
            {
                "pr_number": ("purchase_requisition", pr_number),
                "material": ("material", material),
                "plant": ("plant", plant),
            }
        )
        rows = self.db.execute(
            text(
                "SELECT purchase_requisition, item_of_requisition, material, plant, quantity_requested, "
                f"requisition_date, purchase_order, purchase_order_item FROM {self.work.pr} "
                f"WHERE {self.batch.predicate()} {extra}"
            ),
            {**self.batch.params(), **params},
        ).fetchall()
        return [_to_pr_row(r) for r in rows]

    @memoize_per_instance
    def get_purchase_order_items(
        self,
        *,
        po_number: str | None = None,
        pr_number: str | None = None,
        material: str | None = None,
        plant: str | None = None,
    ) -> list[Row]:
        extra, params = _filters(
            {
                "po_number": ("purchasing_document", po_number),
                "pr_number": ("purchase_requisition", pr_number),
                "material": ("material", material),
                "plant": ("plant", plant),
            }
        )
        rows = self.db.execute(
            text(
                "SELECT purchasing_document, item, purchase_requisition, item_of_requisition, material, plant, "
                f"order_quantity FROM {self.work.po} WHERE {self.batch.predicate()} {extra}"
            ),
            {**self.batch.params(), **params},
        ).fetchall()
        return [_to_po_item_row(r) for r in rows]

    @memoize_per_instance
    def get_goods_receipt_history(self, *, po_number: str | None = None) -> list[Row]:
        extra, params = _filters({"po_number": ("purchasing_document", po_number)})
        # A GR line carries its PO line's material, not a plant: the plant
        # narrowing of a one-material refresh is applied through the PO line.
        batch = Batch(self.batch.lo, self.batch.hi)
        rows = self.db.execute(
            text(
                "SELECT purchasing_document, item, movement_type, quantity, posting_date "
                f"FROM {self.work.gr} WHERE {batch.predicate()} {extra}"
            ),
            {**batch.params(), **params},
        ).fetchall()
        return [_to_gr_row(r) for r in rows]

    @memoize_per_instance
    def get_deterministic_gi_candidates(self, *, po_number: str | None = None) -> list[Row]:
        extra, params = _filters({"po_number": ("purchase_order", po_number)})
        rows = self.db.execute(
            text(
                "SELECT material, plant, movement_type, quantity, posting_date, purchase_order, item "
                f"FROM {self.work.mov} WHERE {self.batch.predicate()} "
                "AND movement_type IN ('201', '261') AND purchase_order <> '' AND item <> '' "
                f"{extra}"
            ),
            {**self.batch.params(), **params},
        ).fetchall()
        return [_to_gi_link_row(r) for r in rows]


@dataclass
class BatchReservationRepository:
    db: Session
    work: WorkTables
    batch: Batch
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    @memoize_per_instance
    def get_reservations(
        self,
        *,
        reservation_number: str | None = None,
        pr_number: str | None = None,
        material: str | None = None,
        plant: str | None = None,
    ) -> list[Row]:
        extra, params = _filters(
            {
                "reservation_number": ("reservation", reservation_number),
                "pr_number": ("purchase_requisition", pr_number),
                "material": ("material", material),
                "plant": ("plant", plant),
            }
        )
        rows = self.db.execute(
            text(
                "SELECT reservation, item_no_stock_transfer_reserv, item_deleted, final_issue, material, plant, "
                "storage_location, requirement_date, requirement_quantity, base_unit_of_measure, "
                'quantity_withdrawn, value_withdrawn, purchase_requisition, item_of_requisition, "order", '
                "movement_type, receiving_plant, receiving_stor_loc, goods_recipient, text "
                f"FROM {self.work.resb} WHERE {self.batch.predicate()} {extra}"
            ),
            {**self.batch.params(), **params},
        ).fetchall()
        result = [_to_reservation_row(r) for r in rows]

        from app.core.config import get_settings

        if get_settings().i13_uat_simulation_enabled:
            result = _with_uat_overlay(
                self.db,
                result,
                reservation_number=reservation_number,
                pr_number=pr_number,
                material=material,
                plant=plant,
            )
            # The overlay appends simulated reservations for every material;
            # this batch keeps its own.
            result = [r for r in result if not r.get("UatSimulated") or self.batch.holds(r["Matnr"], r["Werks"])]
        return result

    @memoize_per_instance
    def get_goods_issue_by_reservation(self, *, reservation_number: str | None = None) -> list[Row]:
        extra, params = _filters({"reservation_number": ("reservation", reservation_number)})
        rows = self.db.execute(
            text(
                "SELECT reservation, item_no_stock_transfer_reserv, movement_type, quantity, posting_date "
                f"FROM {self.work.mov} WHERE {self.batch.predicate()} "
                "AND movement_type IN ('201', '261') AND reservation <> '' AND reservation <> '0' "
                f"{extra}"
            ),
            {**self.batch.params(), **params},
        ).fetchall()
        return [_to_gi_by_reservation_row(r) for r in rows]


def scope_index(db: Session, work: WorkTables, batch: Batch) -> dict[tuple[str, str], str | None]:
    """``fetch_material_scope_index`` for one batch."""
    rows = db.execute(
        text(f"SELECT material, plant, mrp_type FROM {work.marc} WHERE {batch.predicate()}"),
        batch.params(),
    ).fetchall()
    return {(r.material.strip(), r.plant.strip()): (r.mrp_type or "").strip() or None for r in rows}
