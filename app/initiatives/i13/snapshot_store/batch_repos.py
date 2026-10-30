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

import functools
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.initiatives.i13.snapshot_store.work_tables import WorkTables
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


# --- memory --------------------------------------------------------------------
#
# A batch is bounded by rows, but one material is never split, and a handful of
# materials on the production extract carry half a million movements or a
# quarter of a million reservations each (measured 11 Oct 2026). Held the
# straightforward way -- fetchall(), then a dict per row with its own copy of
# every value -- one such material outgrew the App Service. Three things here
# keep a row's cost down without changing a single value a builder sees:
#
# * rows are converted as the driver hands them over, never all fetched first,
#   so the driver's rows and the dicts are not held at the same time;
# * the values that repeat across rows -- material, plant, movement type,
#   quantities, dates, units -- are kept once per batch and shared. Decimals
#   are pooled by their exact digits and exponent, so ``1`` and ``1.000`` stay
#   distinct values, as they were;
# * rows of one shape share one key table. A row is still a plain, mutable
#   ``dict``; it is built as an object's attribute dict, which CPython stores
#   as values only, the keys held once for every object of the class (PEP 412's
#   key-sharing dictionaries). A 21-key reservation row drops from ~470 to
#   ~280 bytes, a movement row from ~280 to ~150. Should an interpreter not
#   share keys, the rows are ordinary dicts and only the saving is lost.

#: Row keys whose values repeat across rows. Identifiers that are unique per
#: row (reservation, PO and PR numbers, texts) are left alone: pooling those
#: would cost an entry per row and save nothing.
_POOLED = frozenset(
    {
        "Matnr", "Werks", "Bwart", "Menge", "BudatMkpf", "Ebelp", "Bnfpo", "Rspos", "Lgort", "Bdter", "Bdmng",
        "Meins", "Enmng", "Enwrt", "Umwrk", "Umlgo", "Wempf", "Badat",
    }
)


class _Pool:
    """Builds a batch's rows: one shared copy of each distinct ``_POOLED``
    value, and one shared key table per row shape (per converter)."""

    __slots__ = ("_seen", "_shapes")

    def __init__(self) -> None:
        self._seen: dict[tuple, Any] = {}
        self._shapes: dict[Any, type] = {}

    def row(self, convert, record: Any) -> Row:
        """``convert(record)``, rebuilt compactly. Equal to it, key for key and
        in the same key order."""
        shape = self._shapes.get(convert)
        if shape is None:
            shape = self._shapes[convert] = type("BatchRow", (), {})
        holder = shape()
        seen = self._seen
        for key, value in convert(record).items():
            if value is not None and key in _POOLED:
                ident = (Decimal, value.as_tuple()) if value.__class__ is Decimal else (value.__class__, value)
                value = seen.setdefault(ident, value)
            setattr(holder, key, value)
        return holder.__dict__


def _read(db: Session, sql: str, params: dict[str, str], convert, pool: _Pool) -> list[Row]:
    """``convert`` each row of ``sql`` as it arrives, compactly (``_Pool``)."""
    row = pool.row
    return [row(convert, record) for record in db.execute(text(sql), params)]


def _memoized(method):
    """``memoize_per_instance``, keyed by the filters that are actually set.

    Every filter here is ``AND col = :value`` only when the value is truthy
    (``_filters``), so ``get_reservations()``, ``get_reservations(material=None,
    plant=None)`` and ``get_reservations(reservation_number=None)`` are one
    query -- the builders call it all three ways. Keyed on the arguments as
    written, they were three cache entries and three copies of the batch's
    reservations; keyed on what they select, they are one.
    """

    @functools.wraps(method)
    def wrapper(self, **filters):
        key = (method.__name__, tuple(sorted((k, v) for k, v in filters.items() if v)))
        cache = self._cache
        if key not in cache:
            cache[key] = method(self, **filters)
        return cache[key]

    return wrapper


# SQL Server compares strings ignoring trailing spaces, and NULL matches
# nothing: these say what the removed WHERE clauses said, for rows already read.
def _blank(value: str | None) -> bool:
    return value is None or value.rstrip(" ") == ""


def _is_issue(movement_type: str | None) -> bool:
    return movement_type is not None and movement_type.rstrip(" ") in ("201", "261")


def _is_issue_or_reversal(movement_type: str | None) -> bool:
    return movement_type is not None and movement_type.rstrip(" ") in ("201", "261", "202", "262")


@dataclass
class BatchMovements:
    """The batch's goods movements, read ONCE for the three repositories.

    Movement history, issues by reservation and issues by PO are three
    readings of the same work table; on 5000017969 two of them are nearly the
    whole table (222k of 240k rows). Read separately, each was fetched and
    converted on its own. Here one pass yields all three, through one pool, in
    the table's order -- and each list holds exactly the rows its own query
    selected:

        history            every row
        issues by resv.    movement_type IN ('201','261','202','262') AND reservation <> '' AND reservation <> '0'
        issues by PO       movement_type IN ('201','261') AND purchase_order <> '' AND item <> ''

    ``drop_history`` lets the builder release the largest of the three once
    nothing reads it any more.
    """

    db: Session
    work: WorkTables
    batch: Batch
    _pool: _Pool = field(default_factory=_Pool, repr=False, compare=False)
    _history: list[Row] | None = field(default=None, repr=False, compare=False)
    _by_reservation: list[Row] | None = field(default=None, repr=False, compare=False)
    _by_po: list[Row] | None = field(default=None, repr=False, compare=False)

    def _load(self) -> None:
        if self._by_reservation is not None:
            return
        history: list[Row] = []
        by_reservation: list[Row] = []
        by_po: list[Row] = []
        row = self._pool.row
        result = self.db.execute(
            text(
                "SELECT material, plant, movement_type, quantity, posting_date, purchase_order, item, "
                f"reservation, item_no_stock_transfer_reserv FROM {self.work.mov} WHERE {self.batch.predicate()}"
            ),
            self.batch.params(),
        )
        for r in result:
            history.append(row(_to_movement_row, r))
            # Reversals ride along with a reservation's issues, so it nets them out.
            if _is_issue_or_reversal(r.movement_type) and not _blank(r.reservation) and r.reservation.rstrip(" ") != "0":
                by_reservation.append(row(_to_gi_by_reservation_row, r))
            if _is_issue(r.movement_type) and not _blank(r.purchase_order) and not _blank(r.item):
                by_po.append(row(_to_gi_link_row, r))
        self._history, self._by_reservation, self._by_po = history, by_reservation, by_po

    def history(self) -> list[Row]:
        self._load()
        if self._history is None:
            raise RuntimeError("I13 store: the batch's movement history was released and read again")
        return self._history

    def issues_by_reservation(self) -> list[Row]:
        self._load()
        return self._by_reservation

    def issues_by_po(self) -> list[Row]:
        self._load()
        return self._by_po

    def drop_history(self) -> None:
        self._history = None


@dataclass
class BatchMovementRepository:
    db: Session
    work: WorkTables
    batch: Batch
    movements: BatchMovements | None = None
    _cache: dict = field(default_factory=dict, repr=False, compare=False)
    _pool: _Pool = field(default_factory=_Pool, repr=False, compare=False)

    @_memoized
    def get_movement_history(self, *, material: str | None = None, plant: str | None = None) -> list[Row]:
        if self.movements is not None and not material and not plant:
            return self.movements.history()
        extra, params = _filters({"material": ("material", material), "plant": ("plant", plant)})
        return _read(
            self.db,
            "SELECT material, plant, movement_type, quantity, posting_date, purchase_order, item "
            f"FROM {self.work.mov} WHERE {self.batch.predicate()} {extra}",
            {**self.batch.params(), **params},
            _to_movement_row,
            self._pool,
        )

    @_memoized
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
        )
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
    movements: BatchMovements | None = None
    _cache: dict = field(default_factory=dict, repr=False, compare=False)
    _pool: _Pool = field(default_factory=_Pool, repr=False, compare=False)

    @_memoized
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
        return _read(
            self.db,
            "SELECT purchase_requisition, item_of_requisition, material, plant, quantity_requested, "
            f"requisition_date, purchase_order, purchase_order_item, deletion_indicator FROM {self.work.pr} "
            f"WHERE {self.batch.predicate()} {extra}",
            {**self.batch.params(), **params},
            _to_pr_row,
            self._pool,
        )

    @_memoized
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
        return _read(
            self.db,
            "SELECT purchasing_document, item, purchase_requisition, item_of_requisition, material, plant, "
            "order_quantity, delivery_completed, deletion_indicator, doc_type "
            f"FROM {self.work.po} WHERE {self.batch.predicate()} {extra}",
            {**self.batch.params(), **params},
            _to_po_item_row,
            self._pool,
        )

    @_memoized
    def get_goods_receipt_history(self, *, po_number: str | None = None) -> list[Row]:
        extra, params = _filters({"po_number": ("purchasing_document", po_number)})
        # A GR line carries its PO line's material, not a plant: the plant
        # narrowing of a one-material refresh is applied through the PO line.
        batch = Batch(self.batch.lo, self.batch.hi)
        return _read(
            self.db,
            "SELECT purchasing_document, item, movement_type, quantity, posting_date "
            f"FROM {self.work.gr} WHERE {batch.predicate()} {extra}",
            {**batch.params(), **params},
            _to_gr_row,
            self._pool,
        )

    @_memoized
    def get_deterministic_gi_candidates(self, *, po_number: str | None = None) -> list[Row]:
        if self.movements is not None and not po_number:
            return self.movements.issues_by_po()
        extra, params = _filters({"po_number": ("purchase_order", po_number)})
        return _read(
            self.db,
            "SELECT material, plant, movement_type, quantity, posting_date, purchase_order, item "
            f"FROM {self.work.mov} WHERE {self.batch.predicate()} "
            "AND movement_type IN ('201', '261') AND purchase_order <> '' AND item <> '' "
            f"{extra}",
            {**self.batch.params(), **params},
            _to_gi_link_row,
            self._pool,
        )


@dataclass
class BatchReservationRepository:
    db: Session
    work: WorkTables
    batch: Batch
    movements: BatchMovements | None = None
    _cache: dict = field(default_factory=dict, repr=False, compare=False)
    _pool: _Pool = field(default_factory=_Pool, repr=False, compare=False)

    @_memoized
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
        result = _read(
            self.db,
            "SELECT reservation, item_no_stock_transfer_reserv, item_deleted, final_issue, material, plant, "
            "storage_location, requirement_date, requirement_quantity, base_unit_of_measure, "
            'quantity_withdrawn, value_withdrawn, purchase_requisition, item_of_requisition, "order", '
            "movement_type, receiving_plant, receiving_stor_loc, goods_recipient, text "
            f"FROM {self.work.resb} WHERE {self.batch.predicate()} {extra}",
            {**self.batch.params(), **params},
            _to_reservation_row,
            self._pool,
        )

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

    @_memoized
    def get_goods_issue_by_reservation(self, *, reservation_number: str | None = None) -> list[Row]:
        if self.movements is not None and not reservation_number:
            return self.movements.issues_by_reservation()
        extra, params = _filters({"reservation_number": ("reservation", reservation_number)})
        return _read(
            self.db,
            "SELECT reservation, item_no_stock_transfer_reserv, movement_type, quantity, posting_date "
            f"FROM {self.work.mov} WHERE {self.batch.predicate()} "
            "AND movement_type IN ('201', '261', '202', '262') AND reservation <> '' AND reservation <> '0' "
            f"{extra}",
            {**self.batch.params(), **params},
            _to_gi_by_reservation_row,
            self._pool,
        )


def scope_index(db: Session, work: WorkTables, batch: Batch) -> dict[tuple[str, str], str | None]:
    """``fetch_material_scope_index`` for one batch."""
    rows = db.execute(
        text(f"SELECT material, plant, mrp_controller FROM {work.marc} WHERE {batch.predicate()}"),
        batch.params(),
    ).fetchall()
    # Blank stays "", distinct from a missing pair (None) -- as there.
    return {(r.material.strip(), r.plant.strip()): (r.mrp_controller or "").strip() for r in rows}
