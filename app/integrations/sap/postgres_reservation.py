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
Reservation -> PR join against the *ledger's* PR set will not resolve for
these rows today -- that is the correct, honest ``UNMATCHED``/pending
behaviour (see ``ledger.py``), not a bug. The real RESB -> EBAN join *does*
resolve within Postgres itself; see ``tests/i13/test_reservation_postgres.py``
for the direct proof, and ``app.seed.manifest`` / this tenant's raw layer for
when the PR/PO/GR/GI legs themselves move onto the same Postgres extract.

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
        "Banfn": _text_or_none(record.purchase_requisition),
        "Bnfpo": _text_or_none(record.item_of_requisition),
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
