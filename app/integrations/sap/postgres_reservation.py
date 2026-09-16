"""Reservation source backed by the real RESB extract already in Postgres.

``ReservationItemSet`` over live CPI OData returns 0 rows in this tenant (see
``mock_gateway.py``), which is why W6.2 needs a reservation source boundary at
all. But the RESB *extract* -- loaded separately into ``raw_resb`` by
``app.seed.loader`` from ``RESB.XLSX`` (see ``app/seed/manifest.py``) -- is
NOT empty: 105,848 real rows, ~1,274 of them carrying a real purchase
requisition reference. This module reads that real data, so the reservation
leg does not have to be fabricated to be tested (see the implementation
plan's W6.2 requirement that any mock "must be built using real values
already present in PostgreSQL").

Tagged ``SourceMode.LIVE``: these are genuine SAP RESB rows, unlike
``ReducedMockSapGateway``'s synthetic ``ReservationItemSet.csv``.

Known gap, documented rather than hidden: the PR/PO/GR/GI legs read by
``LiveSapGateway`` in this build are still the synthetic CSV dataset (see
``csv_source.py``), which uses a different document-number space than the
real ``raw_eban``/``raw_ekpo``/``raw_mseg`` extract this module reads. So a
Reservation -> PR join against *that CSV-backed ledger's* PR set will not
resolve for these rows -- that is the correct, honest ``UNMATCHED``/pending
behaviour (see ``ledger.py``), not a bug. **W6.2's own ledger
(``app.initiatives.i13.reservation_ledger``) does not have this problem** --
it stitches this module's reservations against ``postgres_procurement.py``'s
PR/PO/GR data, the same real Postgres extract, so the join genuinely resolves
there. See that module for the current, real-data-backed Reservation ->
PR -> PO -> GR -> GI ledger.

The raw layer is untyped text and carries the extract's business-label
column names, not SAP field codes (see ``app.seed.loader``'s table comment) --
this module is the one place that translation happens for RESB.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.integrations.sap.source_mode import SapResult, SourceMode, make_status

Row = dict[str, Any]

# Rows with a purchase requisition reference first (the ones a Reservation ->
# PR join can actually use), then the remainder up to LIMIT -- so both "has a
# PR" and "no PR yet" cases (see W6.2 test requirements) are represented
# without loading all 105,848 rows into memory.
_QUERY = text(
    """
    SELECT
        reservation, item_no_stock_transfer_reserv, item_deleted, final_issue,
        material, plant, storage_location, requirement_date, requirement_quantity,
        base_unit_of_measure, quantity_withdrawn, value_withdrawn,
        purchase_requisition, item_of_requisition, "order", movement_type,
        receiving_plant, receiving_stor_loc, goods_recipient
    FROM raw_resb
    WHERE material <> '' AND plant <> ''
    ORDER BY (purchase_requisition <> '') DESC, reservation, item_no_stock_transfer_reserv
    LIMIT :limit
    """
)

DEFAULT_LIMIT = 2000


def _text_or_none(raw: str | None) -> str | None:
    return raw if raw not in (None, "") else None


def _decimal(raw: str | None) -> Decimal | None:
    if raw in (None, ""):
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


def _date(raw: str | None) -> date | None:
    if raw in (None, ""):
        return None
    return date.fromisoformat(raw)


def _flag(raw: str | None) -> bool:
    """SAP raw-extract checkbox convention: ``"X"`` true, blank/empty false."""
    return (raw or "").strip().upper() == "X"


def _to_reservation_row(record: Any) -> Row:
    """One ``raw_resb`` record -> a row shaped like the normalized
    ``ReservationItemSet`` model the rest of I13 already consumes (see
    ``mock_gateway.py``'s ``_RESERVATION_ITEM`` schema)."""
    # Same zero-default sentinel as EKPO's Bnfpo / EBAN's Ebelp (see
    # postgres_procurement.py): item_of_requisition reads "0" whenever
    # purchase_requisition is NULL, never otherwise (measured: 104,574/104,574
    # co-occurrences, 0 counter-examples) -- guarded the same way so a "no
    # PR" reservation reports Bnfpo=None, not the misleading literal "0".
    banfn = _text_or_none(record.purchase_requisition)
    bnfpo = _text_or_none(record.item_of_requisition) if banfn else None
    return {
        "Rsnum": _text_or_none(record.reservation),
        "Rspos": _text_or_none(record.item_no_stock_transfer_reserv),
        "Xloek": _flag(record.item_deleted),
        "Kzear": _flag(record.final_issue),
        "Matnr": _text_or_none(record.material),
        "Werks": _text_or_none(record.plant),
        "Lgort": _text_or_none(record.storage_location),
        "Bdter": _date(record.requirement_date),
        "Bdmng": _decimal(record.requirement_quantity),
        "Meins": _text_or_none(record.base_unit_of_measure),
        "Enmng": _decimal(record.quantity_withdrawn),
        "Enwrt": _decimal(record.value_withdrawn),
        "Banfn": banfn,
        "Bnfpo": bnfpo,
        "Aufnr": _text_or_none(record.order),
        "Bwart": _text_or_none(record.movement_type),
        "Umwrk": _text_or_none(record.receiving_plant),
        "Umlgo": _text_or_none(record.receiving_stor_loc),
        "Wempf": _text_or_none(record.goods_recipient),
        # Not present in the raw RESB extract. The designated session/tracking
        # field is a later BAdI-contract addition (see the implementation
        # plan, W6.2 §6.2) -- not invented here.
        "Zzaisession": None,
    }


# --- W6.2: filtered reservation fetch for ledger stitching ------------------
#
# Distinct from the ``_QUERY``/``PostgresReservationProvider`` above (which
# serves the SapGateway ``ReservationItemSet`` contract, LIMIT-sampled for
# that CSV-shaped integration). W6.2's own ledger wants every reservation in
# scope, filterable, no artificial sample cap -- 105,848 rows is the same
# order of magnitude as raw_eban/raw_ekpo, already handled unsampled
# elsewhere in this codebase (see postgres_procurement.py).
_RESERVATION_FETCH_QUERY = """
    SELECT
        reservation, item_no_stock_transfer_reserv, item_deleted, final_issue,
        material, plant, storage_location, requirement_date, requirement_quantity,
        base_unit_of_measure, quantity_withdrawn, value_withdrawn,
        purchase_requisition, item_of_requisition, "order", movement_type,
        receiving_plant, receiving_stor_loc, goods_recipient
    FROM raw_resb
    WHERE material <> '' AND plant <> ''
      {reservation_filter}
      {pr_filter}
      {material_filter}
      {plant_filter}
"""


def fetch_reservations(
    db: Session,
    *,
    reservation_number: str | None = None,
    pr_number: str | None = None,
    material: str | None = None,
    plant: str | None = None,
) -> list[Row]:
    params: dict[str, str] = {}
    reservation_filter = ""
    if reservation_number:
        reservation_filter = "AND reservation = :reservation_number"
        params["reservation_number"] = reservation_number
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
        _RESERVATION_FETCH_QUERY.format(
            reservation_filter=reservation_filter,
            pr_filter=pr_filter,
            material_filter=material_filter,
            plant_filter=plant_filter,
        )
    )
    return [_to_reservation_row(r) for r in db.execute(query, params).fetchall()]


# --- W6.2 §9: goods issue -> Reservation, the strong GI link ----------------
#
# Measured against this dataset: raw_mseg.reservation is populated on 91.2%
# of real 201 rows and 90.9% of real 261 rows (vs. 0% carrying a PO
# reference, per postgres_procurement.py's GI-attempt query) -- reservation
# is the deterministic GI attribution path W6.1 didn't have.
_GI_BY_RESERVATION_QUERY = """
    SELECT m.reservation, m.item_no_stock_transfer_reserv, m.movement_type,
           m.quantity, h.posting_date
    FROM raw_mseg m
    JOIN raw_mkpf h
      ON m.material_document = h.material_document
     AND m.material_doc_year = h.material_doc_year
    WHERE m.movement_type IN ('201', '261')
      AND m.reservation <> '' AND m.reservation <> '0'
      AND h.posting_date <> ''
      {reservation_filter}
"""


def _to_gi_by_reservation_row(record: Any) -> Row:
    return {
        "Rsnum": record.reservation.strip(),
        "Rspos": (record.item_no_stock_transfer_reserv or "").strip(),
        "Bwart": (record.movement_type or "").strip(),
        "Menge": _decimal(record.quantity) or Decimal("0"),
        "BudatMkpf": _date(record.posting_date),
    }


def fetch_goods_issue_by_reservation(db: Session, *, reservation_number: str | None = None) -> list[Row]:
    """Issue-type (201/261) movements carrying a real reservation reference --
    the deterministic GI -> Reservation link W6.2 adds on top of W6.1."""
    params: dict[str, str] = {}
    reservation_filter = ""
    if reservation_number:
        reservation_filter = "AND m.reservation = :reservation_number"
        params["reservation_number"] = reservation_number

    query = text(_GI_BY_RESERVATION_QUERY.format(reservation_filter=reservation_filter))
    return [_to_gi_by_reservation_row(r) for r in db.execute(query, params).fetchall()]


@dataclass
class PostgresReservationRepository:
    """W6.2's reservation repository -- reservations plus their
    deterministic GI linkage. Distinct from ``PostgresReservationProvider``
    below, which serves the older SapGateway/CSV-ledger integration seam."""

    db: Session

    def get_reservations(
        self,
        *,
        reservation_number: str | None = None,
        pr_number: str | None = None,
        material: str | None = None,
        plant: str | None = None,
    ) -> list[Row]:
        return fetch_reservations(
            self.db, reservation_number=reservation_number, pr_number=pr_number, material=material, plant=plant
        )

    def get_goods_issue_by_reservation(self, *, reservation_number: str | None = None) -> list[Row]:
        return fetch_goods_issue_by_reservation(self.db, reservation_number=reservation_number)


@dataclass
class PostgresReservationProvider:
    """``ReservationProvider`` backed by the real ``raw_resb`` Postgres table.

    Takes a ``sessionmaker`` rather than opening its own engine/session, so it
    composes with ``app.core.db`` and stays lazy -- constructing this class
    does not connect to anything; only ``get_reservations()`` does.
    """

    session_factory: sessionmaker[Session]
    limit: int = DEFAULT_LIMIT

    def get_reservations(self) -> SapResult:
        with self.session_factory() as session:
            records = session.execute(_QUERY, {"limit": self.limit}).fetchall()
        rows = [_to_reservation_row(record) for record in records]
        return SapResult(rows=rows, status=make_status("ReservationItemSet", SourceMode.LIVE, len(rows)))
