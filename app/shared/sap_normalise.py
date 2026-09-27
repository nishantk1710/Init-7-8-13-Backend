"""The normalise layer: one view per raw SAP table, in ONE vocabulary.

Why this exists
---------------
The raw layer is filled by two loaders that disagree about everything but the
data:

    app.seed          July XLSX workbooks  -> raw_<table>  business labels
                                                           (``material``, ``plant``),
                                                           keys unpadded, ISO dates
    app.ingest (CSV)  SAP CSV extract      -> raw_<table>  SAP field names
                                                           (``MATNR``, ``WERKS``),
                                                           keys zero-padded,
                                                           ``DD.MM.YYYY`` dates

Initiatives 08 and 13 were written against the first shape. On Azure the CSV
extract has replaced it, so every I13 read failed with ``Invalid column name
'material'`` and I08's views could not be built at all.

``app/seed/manifest.py`` always planned a "normalise" step between the raw layer
and the initiatives (translation map + MATNR padding, "NOT YET BUILT"). This is
it, for the tables I07, I08 and I13 read:

    raw_<table>  --(either vocabulary)-->  n_<table>  --> I07 staging, I08 views, I13 queries

``n_<table>`` exposes the WORKBOOK labels -- the names the existing queries
already use -- so no query logic changes; only ``FROM raw_x`` becomes
``FROM n_x``. Which physical column feeds each label is decided when the view is
built, by looking at the table: the label itself if present (workbook load),
else the first SAP field name that exists (CSV load), else NULL. A column SAP
did not deliver is a visible gap, never an outage.

What each kind of column gets, identically for both sources:

    key   leading zeros stripped   ``0000219937`` -> ``219937`` (the workbook form,
                                   and what every app-side id already uses)
    date  ISO ``YYYY-MM-DD`` text  from ``YYYY-MM-DD``, ``DD.MM.YYYY`` or ``YYYYMMDD``;
                                   SAP's zero date becomes NULL; anything else passes
                                   through unchanged
    num   plain decimal text       from the CSV's German format: ``1.234,5`` -> ``1234.5``,
                                   ``0,989`` -> ``0.989``; SAP's trailing minus moved
                                   to the front (``12,000-`` -> ``-12.000``). A workbook
                                   column is already plain and only has its minus moved.
                                   Which format applies is declared by the vocabulary
                                   the column resolved through, never read off a value.
    text  as loaded

Workbook data is already in that shape, so for it every transform is a no-op.

Initiative 07 reads this layer too, through its staging adapter
(``app/initiatives/i7/adapters/extract.py``) -- the one I07 module that touches
SAP data. The columns only I07 reads are marked where they are declared. MARC's
MRP fields are the exception: the live CSV MARC does not carry them at all, so
I07 takes them from ``odata_material_plant`` (the OData MaterialPlantSet), which
this layer does not read.

Rebuilding
----------
``rebuild_read_layers()`` runs at application start-up (``app.main``, gated by
``NORMALISE_VIEWS_ON_STARTUP``) and, through ``refresh_after_load()``, after
every CSV or workbook load of a table this layer covers -- both loaders drop
and recreate ``raw_<table>``, and a load can change which vocabulary it is in.
The OData delta loader writes ``odata_<table>``, which nothing here reads.
By hand::

    python -m app.shared.sap_normalise create    # build or rebuild
    python -m app.shared.sap_normalise check     # which label reads which column
    python -m app.shared.sap_normalise drop

Both raw loaders are SQL Server only (``app.seed.sqlserver``), where
``DROP TABLE`` under a view is allowed and ``CREATE OR ALTER VIEW`` re-points
it. On Postgres a view blocks ``DROP TABLE`` of the table it reads, so drop
these (and I08's) first -- ``python -m app.initiatives.i8.views drop`` and
``drop`` here -- and ``create`` again after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import Engine, inspect, text

from app.core.db import get_engine
from app.core.logging import get_logger

logger = get_logger(__name__)

Kind = Literal["key", "date", "num", "text"]


@dataclass(frozen=True)
class Col:
    """One column of a normalise view."""

    label: str
    """The workbook label -- the name the view exposes and every query uses."""

    sap: tuple[str, ...]
    """SAP field names that can carry it, in preference order (CSV headers)."""

    kind: Kind = "text"


def _c(label: str, *sap: str, kind: Kind = "text") -> Col:
    return Col(label, tuple(sap), kind)


# The mapping. Labels are exactly the ones the I08 and I13 queries read; the SAP
# field for each is the standard DDIC field behind the export's column header.
# Where S/4HANA and ECC name a field differently (MSEG's posting date is
# BUDAT_MKPF on S/4), both are listed and whichever the extract carries wins.
TABLES: dict[str, tuple[Col, ...]] = {
    "marc": (
        _c("material", "MATNR", kind="key"),
        _c("plant", "WERKS"),
        _c("mrp_type", "DISMM"),
        _c("planned_deliv_time", "PLIFZ", kind="num"),
        _c("procurement_type", "BESKZ"),
        _c("reorder_point", "MINBE", kind="num"),
        # Read by I07's staging. The live CSV MARC carries none of the MRP
        # fields (DISMM, PLIFZ, MINBE, MABST); I07 takes those from
        # odata_material_plant when raw_marc lacks them.
        _c("maximum_stock_level", "MABST", kind="num"),
        _c("df_at_plant_level", "LVORM"),
    ),
    "mard": (
        _c("material", "MATNR", kind="key"),
        _c("plant", "WERKS"),
        _c("storage_location", "LGORT"),
        _c("unrestricted", "LABST", kind="num"),
        _c("in_quality_insp", "INSME", kind="num"),
        _c("blocked", "SPEME", kind="num"),
        _c("returns", "RETME", kind="num"),
        _c("reorder_point", "LMINB", kind="num"),
        _c("created_on", "ERSDA", kind="date"),
        # Read by I07's staging.
        _c("stock_in_transfer", "UMLME", kind="num"),
        _c("restricted_use_stock", "EINME", kind="num"),
    ),
    "mseg": (
        _c("material_document", "MBLNR", kind="key"),
        _c("material_doc_year", "MJAHR"),
        _c("material_doc_item", "ZEILE", kind="key"),
        _c("identification", "LINE_ID", kind="key"),
        _c("movement_type", "BWART"),
        _c("material", "MATNR", kind="key"),
        _c("plant", "WERKS"),
        _c("storage_location", "LGORT"),
        _c("batch", "CHARG"),
        _c("quantity", "MENGE", kind="num"),
        _c("base_unit_of_measure", "MEINS"),
        _c("order", "AUFNR", kind="key"),
        _c("posting_date", "BUDAT_MKPF", "BUDAT", kind="date"),
        _c("reference", "XBLNR_MKPF", "XBLNR"),
        _c("special_stock", "SOBKZ"),
        _c("supplier", "LIFNR", kind="key"),
        _c("vendor", "LIFNR", kind="key"),
        _c("debit_credit_ind", "SHKZG"),
        _c("currency", "WAERS"),
        _c("purchase_order", "EBELN", kind="key"),
        _c("item", "EBELP", kind="key"),
        _c("delivery_completed", "ELIKZ"),
        _c("text", "SGTXT"),
        _c("goods_recipient", "WEMPF"),
        _c("company_code", "BUKRS"),
        _c("reservation", "RSNUM", kind="key"),
        _c("item_no_stock_transfer_reserv", "RSPOS", kind="key"),
        _c("final_issue", "KZEAR"),
        _c("receiving_plant", "UMWRK"),
        _c("receiving_stor_loc", "UMLGO"),
        _c("consumption", "KZVBR"),
        _c("cost_center", "KOSTL", kind="key"),
        _c("amount_in_lc", "DMBTR", kind="num"),
    ),
    "mkpf": (
        _c("material_document", "MBLNR", kind="key"),
        _c("material_doc_year", "MJAHR"),
        _c("document_date", "BLDAT", kind="date"),
        _c("posting_date", "BUDAT", kind="date"),
        _c("reference", "XBLNR"),
    ),
    "resb": (
        _c("reservation", "RSNUM", kind="key"),
        _c("item_no_stock_transfer_reserv", "RSPOS", kind="key"),
        _c("item_deleted", "XLOEK"),
        _c("final_issue", "KZEAR"),
        _c("material", "MATNR", kind="key"),
        _c("plant", "WERKS"),
        _c("storage_location", "LGORT"),
        _c("batch", "CHARG"),
        _c("special_stock", "SOBKZ"),
        _c("requirement_date", "BDTER", kind="date"),
        _c("requirement_quantity", "BDMNG", kind="num"),
        _c("base_unit_of_measure", "MEINS"),
        _c("debit_credit_ind", "SHKZG"),
        _c("quantity_withdrawn", "ENMNG", kind="num"),
        _c("value_withdrawn", "ENWRT", kind="num"),
        _c("currency", "WAERS"),
        _c("purchase_requisition", "BANFN", kind="key"),
        _c("item_of_requisition", "BNFPO", kind="key"),
        _c("order", "AUFNR", kind="key"),
        _c("movement_type", "BWART"),
        _c("receiving_plant", "UMWRK"),
        _c("receiving_stor_loc", "UMLGO"),
        _c("item_category", "POSTP"),
        _c("text", "SGTXT"),
        _c("purchasing_document", "EBELN", kind="key"),
        _c("item", "EBELP", kind="key"),
        _c("purchasing_group", "EKGRP"),
        _c("goods_recipient", "WEMPF"),
        _c("material_group", "MATKL"),
        _c("vendor", "LIFNR", kind="key"),
        _c("cost_center", "KOSTL", kind="key"),
    ),
    "ekpo": (
        _c("purchasing_document", "EBELN", kind="key"),
        _c("item", "EBELP", kind="key"),
        _c("deletion_indicator", "LOEKZ"),
        _c("last_changed_on", "AEDAT", kind="date"),
        _c("short_text", "TXZ01"),
        _c("material", "MATNR", kind="key"),
        _c("company_code", "BUKRS"),
        _c("plant", "WERKS"),
        _c("storage_location", "LGORT"),
        _c("material_group", "MATKL"),
        _c("order_quantity", "MENGE", kind="num"),
        _c("net_order_price", "NETPR", kind="num"),
        _c("price_unit", "PEINH", kind="num"),
        _c("delivery_completed", "ELIKZ"),
        _c("item_category", "PSTYP"),
        _c("consumption", "KZVBR"),
        _c("base_unit_of_measure", "MEINS"),
        _c("planned_deliv_time", "PLIFZ", kind="num"),
        _c("special_stock", "SOBKZ"),
        _c("purchase_requisition", "BANFN", kind="key"),
        _c("item_of_requisition", "BNFPO", kind="key"),
        _c("material_type", "MTART"),
        _c("requisitioner", "AFNAM"),
        _c("creation_date", "CREATIONDATE", kind="date"),
    ),
    "ekbe": (
        _c("purchasing_document", "EBELN", kind="key"),
        _c("item", "EBELP", kind="key"),
        _c("material_doc_year", "GJAHR"),
        _c("material_document", "BELNR", kind="key"),
        _c("material_doc_item", "BUZEI", kind="key"),
        _c("po_history_category", "BEWTP"),
        _c("movement_type", "BWART"),
        _c("posting_date", "BUDAT", kind="date"),
        _c("quantity", "MENGE", kind="num"),
        _c("currency", "WAERS"),
        _c("debit_credit_ind", "SHKZG"),
        _c("delivery_completed", "ELIKZ"),
        _c("reference", "XBLNR"),
        _c("material", "MATNR", kind="key"),
        _c("plant", "WERKS"),
        _c("batch", "CHARG"),
        _c("document_date", "BLDAT", kind="date"),
        _c("created_by", "ERNAM"),
    ),
    "eban": (
        _c("purchase_requisition", "BANFN", kind="key"),
        _c("item_of_requisition", "BNFPO", kind="key"),
        _c("deletion_indicator", "LOEKZ"),
        _c("purchasing_group", "EKGRP"),
        _c("created_by", "ERNAM"),
        _c("short_text", "TXZ01"),
        _c("material", "MATNR", kind="key"),
        _c("plant", "WERKS"),
        _c("storage_location", "LGORT"),
        _c("material_group", "MATKL"),
        _c("quantity_requested", "MENGE", kind="num"),
        _c("requisition_date", "BADAT", kind="date"),
        _c("delivery_date", "LFDAT", kind="date"),
        _c("item_category", "PSTYP"),
        _c("consumption", "KZVBR"),
        _c("purchase_order", "EBELN", kind="key"),
        _c("purchase_order_item", "EBELP", kind="key"),
        _c("reservation", "RSNUM", kind="key"),
        _c("special_stock", "SOBKZ"),
        _c("vendor", "LIFNR", kind="key"),
        _c("currency", "WAERS"),
        _c("planned_deliv_time", "PLIFZ", kind="num"),
    ),
    "mara": (
        _c("material", "MATNR", kind="key"),
        _c("created_on", "ERSDA", kind="date"),
        _c("created_by", "ERNAM"),
        _c("material_type", "MTART"),
        _c("material_group", "MATKL"),
        _c("base_unit_of_measure", "MEINS"),
        _c("division", "SPART"),
        _c("ext_material_group", "EXTWG"),
        # Not a MARA field; the July MARA export carried it joined in. Over CSV
        # it comes from MAKT instead, so here it is NULL there.
        _c("material_description"),
        # Read by I07's staging.
        _c("x_plant_matl_status", "MSTAE"),
        _c("manufacturer", "MFRNR", kind="key"),
        _c("df_at_client_level", "LVORM"),
    ),
    # Read by I07's staging (unit price). The live CSV MBEW carries no VERPR,
    # so over CSV moving_price is NULL -- a visible gap, not a zero.
    "mbew": (
        _c("material", "MATNR", kind="key"),
        _c("valuation_area", "BWKEY"),
        _c("moving_price", "VERPR", kind="num"),
    ),
    "makt": (
        _c("material", "MATNR", kind="key"),
        _c("language_key", "SPRAS"),
        _c("material_description", "MAKTX"),
    ),
    "lfa1": (
        _c("vendor", "LIFNR", kind="key"),
        _c("country", "LAND1"),
        _c("name_1", "NAME1"),
        _c("city", "ORT01"),
    ),
    "ekko": (
        _c("purchasing_document", "EBELN", kind="key"),
        _c("company_code", "BUKRS"),
        _c("purchasing_doc_type", "BSART"),
        _c("deletion_indicator", "LOEKZ"),
        _c("created_on", "AEDAT", kind="date"),
        _c("created_by", "ERNAM"),
        _c("supplier", "LIFNR", kind="key"),
        _c("vendor", "LIFNR", kind="key"),
        _c("purchasing_group", "EKGRP"),
        _c("currency", "WAERS"),
        _c("document_date", "BEDAT", kind="date"),
    ),
    "eket": (
        _c("purchasing_document", "EBELN", kind="key"),
        _c("item", "EBELP", kind="key"),
        _c("schedule_line", "ETENR", kind="key"),
        _c("delivery_date", "EINDT", kind="date"),
        _c("scheduled_quantity", "MENGE", kind="num"),
        _c("qty_delivered", "WEMNG", kind="num"),
        _c("issued_quantity", "WAMNG", kind="num"),
        _c("purchase_requisition", "BANFN", kind="key"),
        _c("item_of_requisition", "BNFPO", kind="key"),
        _c("reservation", "RSNUM", kind="key"),
        _c("batch", "CHARG"),
    ),
}

VIEWS: tuple[str, ...] = tuple(f"n_{table}" for table in TABLES)


# --- SQL generation ---------------------------------------------------------


@dataclass(frozen=True)
class Resolved:
    """Where one label comes from in one database."""

    label: str
    source: str | None
    """The physical column, or None when neither vocabulary has it."""


def resolve(table: str, present: list[str]) -> list[Resolved]:
    """Pick the physical column for every label, given the table's columns.

    Matching is case-insensitive (SQL Server's CSV tables are upper case,
    Postgres folds unquoted names to lower); the returned name is the column's
    real spelling, so it can be quoted exactly.
    """
    by_lower = {name.lower(): name for name in present}
    resolved: list[Resolved] = []
    for col in TABLES[table]:
        source = by_lower.get(col.label.lower())
        if source is None:
            source = next((by_lower[s.lower()] for s in col.sap if s.lower() in by_lower), None)
        resolved.append(Resolved(col.label, source))
    return resolved


class _Dialect:
    """The handful of expressions that differ between Postgres and SQL Server."""

    def __init__(self, name: str, quote) -> None:
        self.mssql = name == "mssql"
        self.quote = quote
        self.text_type = "nvarchar(4000)" if self.mssql else "text"

    def create_view(self, name: str) -> str:
        return f"CREATE OR ALTER VIEW {name} AS" if self.mssql else f"CREATE VIEW {name} AS"

    def concat(self, *parts: str) -> str:
        return " + ".join(parts) if self.mssql else " || ".join(parts)

    def key(self, c: str) -> str:
        t = f"TRIM({c})"
        if self.mssql:
            return f"SUBSTRING({t}, PATINDEX('%[^0]%', {t} + '.'), 4000)"
        return f"LTRIM({t}, '0')"

    def date(self, c: str) -> str:
        t = f"TRIM({c})"
        iso, dmy, dats = (
            ("'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'", "'[0-9][0-9].[0-9][0-9].[0-9][0-9][0-9][0-9]'",
             "'[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'")
            if self.mssql
            else ("'^[0-9]{4}-[0-9]{2}-[0-9]{2}$'", "'^[0-9]{2}\\.[0-9]{2}\\.[0-9]{4}$'", "'^[0-9]{8}$'")
        )
        match = "LIKE" if self.mssql else "~"
        dash = "'-'"

        def part(start: int, length: int) -> str:
            return f"SUBSTRING({t}, {start}, {length})"

        from_dmy = self.concat(part(7, 4), dash, part(4, 2), dash, part(1, 2))
        from_dats = self.concat(part(1, 4), dash, part(5, 2), dash, part(7, 2))
        return (
            f"CASE WHEN {t} {match} {iso} THEN {t} "
            f"WHEN {t} {match} {dmy} AND {t} <> '00.00.0000' THEN {from_dmy} "
            f"WHEN {t} {match} {dats} AND {t} <> '00000000' THEN {from_dats} "
            f"WHEN {t} IN ('00000000', '00.00.0000') THEN NULL "
            f"ELSE {t} END"
        )

    def num(self, c: str, *, german: bool = False) -> str:
        t = f"TRIM({c})"
        if german:
            # SAP CSV: "1.234,5" -> "1234.5". Grouping dots out first, then the
            # decimal comma becomes a point -- the order is what keeps "0,989"
            # from turning into 989.
            t = f"REPLACE(REPLACE({t}, '.', ''), ',', '.')"
        length = "LEN" if self.mssql else "LENGTH"
        moved = self.concat("'-'", f"LEFT({t}, {length}({t}) - 1)")
        return f"CASE WHEN {t} LIKE '%-' THEN {moved} ELSE {t} END"


def _dialect(engine: Engine) -> _Dialect:
    return _Dialect(engine.dialect.name, engine.dialect.identifier_preparer.quote)


def view_sql(table: str, present: list[str] | None, dialect: _Dialect) -> str:
    """The CREATE VIEW statement for ``n_<table>``.

    ``present`` is the raw table's columns, or None when the table does not
    exist -- then the view has every label, as NULL, and no rows, so a query
    over an extract SAP never delivered (EKKO, EKET) returns nothing instead of
    failing.
    """
    name = f"n_{table}"
    cols = TABLES[table]
    if present is None:
        body = ", ".join(f"CAST(NULL AS {dialect.text_type}) AS {dialect.quote(c.label)}" for c in cols)
        return f"{dialect.create_view(name)} SELECT {body} WHERE 1 = 0"

    kinds = {c.label: c.kind for c in cols}
    select: list[str] = []
    for r in resolve(table, present):
        alias = dialect.quote(r.label)
        if r.source is None:
            select.append(f"CAST(NULL AS {dialect.text_type}) AS {alias}")
            continue
        column = dialect.quote(r.source)
        kind = kinds[r.label]
        # The vocabulary a column resolved through IS the declaration of its
        # number format: the workbook label means the July extract ("1,000" is
        # one thousand), a SAP field name means the CSV extract, written in SAP's
        # German settings ("1,000" is one). Never decided by looking at values.
        from_csv = r.source.lower() != r.label.lower()
        expression = {
            "key": dialect.key,
            "date": dialect.date,
            "num": lambda c: dialect.num(c, german=from_csv),
            "text": lambda c: c,
        }[kind](column)
        select.append(f"{expression} AS {alias}")
    return f"{dialect.create_view(name)} SELECT {', '.join(select)} FROM raw_{table}"


# --- DDL --------------------------------------------------------------------


def _present(engine: Engine) -> dict[str, list[str] | None]:
    inspector = inspect(engine)
    existing = {name.lower(): name for name in inspector.get_table_names()}
    columns: dict[str, list[str] | None] = {}
    for table in TABLES:
        real = existing.get(f"raw_{table}")
        columns[table] = [c["name"] for c in inspector.get_columns(real)] if real else None
    return columns


def ensure_views(engine: Engine | None = None, tables: list[str] | None = None) -> list[str]:
    """Build (or rebuild) the normalise views. Returns the view names built.

    On Postgres, ``CREATE OR REPLACE VIEW`` cannot change a view's columns, and
    I08's views read these -- so I08's are dropped first and rebuilt after, by
    the caller (``rebuild_read_layers``). SQL Server's ``CREATE OR ALTER`` has
    neither limit.
    """
    engine = engine or get_engine()
    dialect = _dialect(engine)
    present = _present(engine)
    targets = tables or list(TABLES)
    with engine.begin() as connection:
        for table in targets:
            if not dialect.mssql:
                connection.execute(text(f"DROP VIEW IF EXISTS n_{table}"))
            connection.execute(text(view_sql(table, present[table], dialect)))
    missing = [t for t in targets if present[t] is None]
    logger.info(
        "normalise: %d views ready%s",
        len(targets),
        f" (no raw table, built empty: {', '.join(missing)})" if missing else "",
    )
    return [f"n_{t}" for t in targets]


def drop_views(engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    with engine.begin() as connection:
        for name in reversed(VIEWS):
            connection.execute(text(f"DROP VIEW IF EXISTS {name}"))


def check(engine: Engine | None = None) -> dict[str, list[Resolved] | None]:
    """Which physical column feeds each label, per table. Touches no DDL."""
    engine = engine or get_engine()
    return {
        table: (resolve(table, cols) if cols is not None else None)
        for table, cols in _present(engine).items()
    }


def rebuild_read_layers(engine: Engine | None = None, *, raise_errors: bool = False) -> bool:
    """Normalise views, then I08's views on top of them, in dependency order.

    What start-up and every CSV load call. By default never raises: a failure
    here must not take the API down -- the affected reads fail on their own,
    loudly, and ``/api/ready`` reports it. The CLI passes ``raise_errors``.
    Returns whether it succeeded.
    """
    from app.initiatives.i8 import views as i8_views

    engine = engine or get_engine()
    try:
        # Postgres cannot rebuild a view another view reads; drop I08's first.
        if engine.dialect.name != "mssql":
            i8_views.drop_views(engine, drop_functions=False)
        ensure_views(engine)
        i8_views.ensure_views(engine)
        return True
    except Exception:
        if raise_errors:
            raise
        logger.exception("normalise: rebuilding the read layers failed")
        return False


def refresh_after_load(table: str) -> bool:
    """Rebuild the read layers after ``raw_<table>`` was (re)created.

    What the CSV and workbook loaders call on success. A table this layer does
    not cover changes nothing here, so it is a no-op that reports success.
    Never raises: the rows are in, and a view that could not be rebuilt is
    logged loudly and retried by the next load or start-up.
    """
    if table.lower() not in TABLES:
        return True
    return rebuild_read_layers()


def main(argv: list[str] | None = None) -> int:
    """``python -m app.shared.sap_normalise create|check|drop``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m app.shared.sap_normalise")
    parser.add_argument("command", choices=("create", "check", "drop"))
    arguments = parser.parse_args(argv)

    if arguments.command == "create":
        rebuild_read_layers(raise_errors=True)
        print(f"{len(VIEWS)} normalise views and the I08 views rebuilt.")
        return 0
    if arguments.command == "drop":
        drop_views()
        print(f"{len(VIEWS)} normalise views dropped.")
        return 0

    status = 0
    for table, resolved in check().items():
        if resolved is None:
            print(f"n_{table:<6} raw_{table} does not exist -- view is built empty")
            continue
        gaps = [r.label for r in resolved if r.source is None]
        mapped = ", ".join(f"{r.label}<-{r.source}" for r in resolved if r.source and r.source != r.label)
        vocabulary = "SAP field names" if mapped else "workbook labels"
        print(f"n_{table:<6} {vocabulary}; {len(resolved) - len(gaps)}/{len(resolved)} columns")
        if mapped:
            print(f"         {mapped}")
        if gaps:
            status = 1
            print(f"         NOT DELIVERED (NULL): {', '.join(gaps)}")
    return status


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
