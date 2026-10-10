"""The build's working copies of the SAP tables I13 reads.

The ``n_*`` views tidy every value on the way out (TRIM, leading zeros, date
shapes), which makes them easy to read and impossible to index: a filter on
``material`` scans all of ``raw_mseg`` -- 7.2M rows on the production extract.
A batched build filters by material dozens of times, so it reads each view
ONCE here, into a plain table holding only plants 1300/1500 and only the
columns the I13 builders use, indexed on (material, plant). Every batch then
seeks instead of scanning.

The names carry the snapshot version (``i13_w<version>_mov``), so a build that
replaces them never touches the copies the version being served still reads
from -- :func:`app.initiatives.i13.snapshot_store.refresh` recomputes one
material from them after a plan is captured. A version's copies are dropped
with the version.

Column names are the views' own, so the existing row converters
(``postgres_movements._to_movement_row`` and friends) read them unchanged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.core.logging import get_logger
from app.shared.plant_scope import sql_predicate

logger = get_logger(__name__)

# Long enough for every key SAP uses here, short enough to index.
_K = "NVARCHAR(40)"
_WORK_NAME = re.compile(r"i13_w(\d+)_[a-z]+")


def _key(column: str, alias: str | None = None) -> str:
    return f"CAST({column} AS {_K}) AS {alias or column.split('.')[-1]}"


@dataclass(frozen=True)
class WorkTables:
    """The names of one version's working copies."""

    version: int

    def name(self, part: str) -> str:
        return f"i13_w{self.version}_{part}"

    @property
    def marc(self) -> str:
        return self.name("marc")

    @property
    def mov(self) -> str:
        return self.name("mov")

    @property
    def stock(self) -> str:
        return self.name("stock")

    @property
    def pr(self) -> str:
        return self.name("pr")

    @property
    def po(self) -> str:
        return self.name("po")

    @property
    def gr(self) -> str:
        return self.name("gr")

    @property
    def resb(self) -> str:
        return self.name("resb")

    @property
    def all(self) -> tuple[str, ...]:
        return (self.marc, self.mov, self.stock, self.pr, self.po, self.gr, self.resb)


def _statements(w: WorkTables) -> list[tuple[str, str, list[str]]]:
    """(table, SELECT ... INTO statement, index column lists), in build order.

    ``gr`` is built from ``po``, so ``po`` comes first.
    """
    plant = sql_predicate("plant")
    plant_m = sql_predicate("m.plant")
    return [
        (
            w.marc,
            (f"SELECT {_key('material')}, {_key('plant')}, mrp_type INTO {w.marc} "
            f"FROM n_marc WHERE material <> '' AND plant <> '' AND {plant}"),
            ["material, plant"],
        ),
        (
            # _MOVEMENT_HISTORY_QUERY's rows (postgres_movements.py), plus the
            # reservation reference _GI_BY_RESERVATION_QUERY reads.
            w.mov,
            (f"SELECT {_key('m.material')}, {_key('m.plant')}, m.movement_type, m.quantity, h.posting_date, "
            f"m.purchase_order, m.item, {_key('m.reservation')}, m.item_no_stock_transfer_reserv "
            f"INTO {w.mov} "
            "FROM n_mseg m JOIN n_mkpf h "
            "ON m.material_document = h.material_document AND m.material_doc_year = h.material_doc_year "
            f"WHERE m.material <> '' AND m.plant <> '' AND {plant_m} AND h.posting_date <> ''"),
            ["material, plant", "reservation"],
        ),
        (
            w.stock,
            (f"SELECT {_key('material')}, {_key('plant')}, unrestricted INTO {w.stock} "
            f"FROM n_mard WHERE material <> '' AND plant <> '' AND {plant} AND unrestricted <> ''"),
            ["material, plant"],
        ),
        (
            w.pr,
            (f"SELECT {_key('purchase_requisition')}, item_of_requisition, {_key('material')}, {_key('plant')}, "
            f"quantity_requested, requisition_date, purchase_order, purchase_order_item INTO {w.pr} "
            f"FROM n_eban WHERE purchase_requisition <> '' AND material <> '' AND plant <> '' AND {plant}"),
            ["material, plant", "purchase_requisition"],
        ),
        (
            w.po,
            (f"SELECT {_key('purchasing_document')}, {_key('item')}, {_key('purchase_requisition')}, "
            f"item_of_requisition, {_key('material')}, {_key('plant')}, order_quantity INTO {w.po} "
            f"FROM n_ekpo WHERE purchasing_document <> '' AND material <> '' AND plant <> '' AND {plant}"),
            ["material, plant", "purchasing_document, item", "purchase_requisition"],
        ),
        (
            # _GR_HISTORY_QUERY's rows, for the in-scope PO lines only -- the
            # only ones the procurement chain ever joins them to -- carrying
            # the line's material so a batch can select them.
            w.gr,
            (f"SELECT {_key('e.purchasing_document', 'purchasing_document')}, {_key('e.item', 'item')}, "
            "e.movement_type, e.quantity, e.posting_date, p.material "
            f"INTO {w.gr} "
            f"FROM n_ekbe e JOIN {w.po} p "
            "ON p.purchasing_document = CAST(e.purchasing_document AS NVARCHAR(40)) "
            "AND p.item = CAST(e.item AS NVARCHAR(40)) "
            "WHERE e.po_history_category = 'E' AND e.purchasing_document <> '' AND e.posting_date <> ''"),
            ["material", "purchasing_document"],
        ),
        (
            w.resb,
            (f"SELECT {_key('reservation')}, item_no_stock_transfer_reserv, item_deleted, final_issue, "
            f"{_key('material')}, {_key('plant')}, storage_location, requirement_date, requirement_quantity, "
            f"base_unit_of_measure, quantity_withdrawn, value_withdrawn, {_key('purchase_requisition')}, "
            'item_of_requisition, "order", movement_type, receiving_plant, receiving_stor_loc, goods_recipient, text '
            f"INTO {w.resb} "
            f"FROM n_resb WHERE material <> '' AND plant <> '' AND {plant}"),
            ["material, plant", "reservation", "purchase_requisition"],
        ),
    ]


def drop(conn: Connection, w: WorkTables) -> None:
    for table in w.all:
        conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


def create(conn: Connection, w: WorkTables, *, log=None) -> dict[str, int]:
    """Build every working copy, indexed. Returns rows per table."""
    drop(conn, w)
    counts: dict[str, int] = {}
    for table, statement, indexes in _statements(w):
        conn.execute(text(statement))
        for n, columns in enumerate(indexes):
            conn.execute(text(f"CREATE INDEX ix_{table}_{n} ON {table} ({columns})"))
        counts[table] = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        (log or logger.info)(f"I13 store: {table} {counts[table]} rows")
    return counts


def drop_versions_except(conn: Connection, keep: set[int]) -> None:
    """Drop every version's working copies but ``keep``'s."""
    rows = conn.execute(text("SELECT name FROM sys.tables WHERE name LIKE 'i13[_]w[0-9]%'")).scalars().all()
    for name in rows:
        # Exactly i13_w<digits>_<part>: never another i13_w... table, such as
        # i13_watch_metric_mart.
        match = _WORK_NAME.fullmatch(name)
        if match and int(match.group(1)) not in keep:
            conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
