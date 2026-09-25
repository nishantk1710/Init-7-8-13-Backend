"""Reservation source backed by the real RESB extract already in Postgres.

``ReservationItemSet`` over live CPI OData returns 0 rows in this tenant, but
the RESB *extract* -- loaded separately into ``raw_resb`` by
``app.seed.loader`` from ``RESB.XLSX`` (see ``app/seed/manifest.py``) -- is
NOT empty: 105,848 real rows, ~1,274 of them carrying a real purchase
requisition reference. This module is the one place the raw-extract ->
normalized-row translation happens for RESB; ``reservation_ledger.py`` (W6.2)
stitches these rows against ``postgres_procurement.py``'s PR/PO/GR data, the
same real Postgres extract, so Reservation -> PR joins genuinely resolve.

The raw layer is untyped text and carries the extract's business-label
column names, not SAP field codes (see ``app.seed.loader``'s table comment).
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
    ``ReservationItemSet`` model the rest of I13 consumes."""
    # "0" zero-default sentinel: item_of_requisition reads "0" whenever
    # purchase_requisition is NULL, never otherwise (measured: 104,574/104,574
    # co-occurrences, 0 counter-examples) -- guarded so a "no PR" reservation
    # reports Bnfpo=None, not the misleading literal "0".
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
        # RESB.SGTXT, the item text -- loaded by the extract as `text`. The
        # requester types the assistant's session ID here (the carrier agreed
        # while BEDNR is not in the extract, blocker B2). Free text: people also
        # write names and notes in it, so it is read, never trusted --
        # app.initiatives.i13.session_link finds and validates the ID.
        "Sgtxt": _text_or_none(record.text),
        # A dedicated session field that never arrived; kept for shape.
        "Zzaisession": None,
    }

# Plants 1300 and 1500 only -- the team lead's ruling of 2026-09-21. Applied in
# the WHERE clause rather than over the returned rows so counts, sums and aging
# bands are computed on the scoped population to begin with; filtering after
# aggregation is where an out-of-scope quantity leaks into a total. Built from
# app/shared/plant_scope.py -- never write the codes out here.
_PLANT_SCOPE = sql_predicate("plant")




_RESERVATION_FETCH_QUERY = """
    SELECT
        reservation, item_no_stock_transfer_reserv, item_deleted, final_issue,
        material, plant, storage_location, requirement_date, requirement_quantity,
        base_unit_of_measure, quantity_withdrawn, value_withdrawn,
        purchase_requisition, item_of_requisition, "order", movement_type,
        receiving_plant, receiving_stor_loc, goods_recipient, text
    FROM raw_resb
    WHERE material <> '' AND plant <> '' AND {plant_scope}
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
            plant_scope=_PLANT_SCOPE,
        )
    )
    rows = [_to_reservation_row(r) for r in db.execute(query, params).fetchall()]

    from app.core.config import get_settings

    if get_settings().i13_uat_simulation_enabled:
        rows = _with_uat_overlay(
            db, rows, reservation_number=reservation_number, pr_number=pr_number, material=material, plant=plant
        )
    return rows


def _with_uat_overlay(
    db: Session,
    rows: list[Row],
    *,
    reservation_number: str | None,
    pr_number: str | None,
    material: str | None,
    plant: str | None,
) -> list[Row]:
    """UAT only: what the extract would contain had the requester done it in SAP.

    ``uat_reservation_sgtxt`` rows either replace a real reservation's SGTXT
    (a *stamp*) or add a reservation that exists nowhere else (a *simulation*).
    Applied to the rows fetched above, never to ``raw_resb`` itself, and only
    while ``I13_UAT_SIMULATION_ENABLED`` is on -- see app/models/i13_session_link.py.
    """
    from sqlalchemy import select

    from app.models.i13_session_link import UatReservationSgtxt

    overlay = db.execute(select(UatReservationSgtxt)).scalars().all()
    if not overlay:
        return rows

    stamps = {(u.reservation_number, u.reservation_item): u for u in overlay if not u.simulated}
    if stamps:
        for row in rows:
            stamp = stamps.get((row["Rsnum"], row["Rspos"]))
            if stamp is not None:
                row["Sgtxt"] = stamp.sgtxt
                row["UatStamped"] = True

    if pr_number:
        # A simulated reservation has no purchase requisition.
        return rows
    for u in overlay:
        if not u.simulated:
            continue
        if reservation_number and u.reservation_number != reservation_number:
            continue
        if material and u.material != material:
            continue
        if plant and u.plant != plant:
            continue
        rows.append(
            {
                "Rsnum": u.reservation_number,
                "Rspos": u.reservation_item,
                "Xloek": False,
                "Kzear": False,
                "Matnr": u.material,
                "Werks": u.plant,
                "Lgort": None,
                "Bdter": u.requirement_date,
                "Bdmng": u.requirement_quantity,
                "Meins": None,
                "Enmng": None,
                "Enwrt": None,
                "Banfn": None,
                "Bnfpo": None,
                "Aufnr": None,
                "Bwart": None,
                "Umwrk": None,
                "Umlgo": None,
                "Wempf": None,
                "Sgtxt": u.sgtxt,
                "Zzaisession": None,
                "UatSimulated": True,
            }
        )
    return rows


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
    deterministic GI linkage.

    Memoized per instance (see ``_request_cache.py``) -- construct one per
    request, never share across requests.
    """

    db: Session
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
        return fetch_reservations(
            self.db, reservation_number=reservation_number, pr_number=pr_number, material=material, plant=plant
        )

    @memoize_per_instance
    def get_goods_issue_by_reservation(self, *, reservation_number: str | None = None) -> list[Row]:
        return fetch_goods_issue_by_reservation(self.db, reservation_number=reservation_number)
