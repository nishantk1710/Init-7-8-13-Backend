"""The two SAP reports I13 reconciles against (FR-6), from the serving database.

ZMM065 (the aging report, one workbook per site) and the 30-Day GR Report are
SAP *reports*, not tables: no CSV-extract or OData route carries them, so they
reach the database as workbook exports, through ``app.ingest.fallback`` --

    raw_zmm065_bmm   Black Mountain (1300)   mat_code, plant, stock_type, last_gi_dt, days
    raw_zmm065_gb    Gamsberg (1500)         same columns (the workbooks differ elsewhere)
    raw_gr_30day     one plant per export    post_date, mat_code, po_no, item, del_qty

Column names are the seed loader's sanitised workbook headers
(``app.seed.reader.column_name``). Read directly rather than through an
``n_<table>`` view, the same way the criticality module already reads ZMM065.

A report that has not been loaded is ``None``, never an empty list: "the
reference is missing" and "the reference has no rows" are different answers,
and the validation endpoint reports them differently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.shared.plant_scope import sql_predicate

logger = get_logger(__name__)

ZMM065_TABLES = ("raw_zmm065_bmm", "raw_zmm065_gb")
GR_30DAY_TABLE = "raw_gr_30day"


@dataclass(frozen=True)
class Zmm065Row:
    material: str
    plant: str
    #: The report's own class: Fast Moving / Slow Moving / Non Moving, or a
    #: non-aging status (OBSOLETE, INSURANCE).
    stock_type: str
    last_gi_date: date | None
    #: Days since the last goods issue, as of the report's run date.
    days: int | None


@dataclass(frozen=True)
class Gr30DayRow:
    post_date: date
    material: str
    po_number: str
    po_item: str
    delivered_quantity: Decimal | None


def key(raw: str | None) -> str:
    """SAP keys arrive zero-padded in some exports and not in others; the
    normalise views strip them, so the reports are compared stripped too."""
    value = (raw or "").strip()
    return value.lstrip("0") or value


def parse_date(raw: str | None) -> date | None:
    value = (raw or "").strip()[:10]
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def parse_int(raw: str | None) -> int | None:
    try:
        return int(Decimal((raw or "").strip()))
    except (InvalidOperation, ValueError):
        return None


def parse_decimal(raw: str | None) -> Decimal | None:
    try:
        return Decimal((raw or "").strip())
    except (InvalidOperation, ValueError):
        return None


def fetch_zmm065_rows(db: Session) -> list[Zmm065Row] | None:
    """Both sites' ZMM065 rows for the in-scope plants. ``None`` if neither
    table is loaded."""
    rows: list[Zmm065Row] = []
    loaded = False
    for table in ZMM065_TABLES:
        # Table names come from the module constant above, never from input.
        query = text(
            f"SELECT mat_code, plant, stock_type, last_gi_dt, days FROM {table} "
            f"WHERE mat_code <> '' AND plant <> '' AND {sql_predicate('plant')}"
        )
        try:
            records = db.execute(query).fetchall()
        except DBAPIError:
            db.rollback()
            logger.info("%s is not loaded; ZMM065 validation skips it", table)
            continue
        loaded = True
        rows.extend(
            Zmm065Row(
                material=key(r.mat_code),
                plant=(r.plant or "").strip(),
                stock_type=(r.stock_type or "").strip(),
                last_gi_date=parse_date(r.last_gi_dt),
                days=parse_int(r.days),
            )
            for r in records
        )
    return rows if loaded else None


def fetch_gr_30day_rows(db: Session) -> list[Gr30DayRow] | None:
    """The 30-Day GR Report's rows. ``None`` if the table is not loaded.

    The report has no plant column; the plant comes from the PO line it names,
    which is the caller's job (see ``report_validation``)."""
    query = text(f"SELECT post_date, mat_code, po_no, item, del_qty FROM {GR_30DAY_TABLE}")
    try:
        records = db.execute(query).fetchall()
    except DBAPIError:
        db.rollback()
        logger.info("%s is not loaded; 30-Day GR validation is unavailable", GR_30DAY_TABLE)
        return None
    rows: list[Gr30DayRow] = []
    for r in records:
        post_date = parse_date(r.post_date)
        if post_date is None or not (r.po_no or "").strip():
            continue  # a blank or footer row in the workbook, not a receipt
        rows.append(
            Gr30DayRow(
                post_date=post_date,
                material=key(r.mat_code),
                po_number=key(r.po_no),
                po_item=key(r.item),
                delivered_quantity=parse_decimal(r.del_qty),
            )
        )
    return rows
