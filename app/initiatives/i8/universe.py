"""W5.1 -- the repairable-universe read model.

One row per repairable **material + plant**: what it is, how much is on hand,
what the planning parameters are, how critical it is, and whether it is out for
repair right now.

This is the foundation the rest of I08 stands on. W5.2's register filters to it,
W5.3's attestation form looks up against it, and W5.5 defines a coding candidate
as repair language on an item that is *not* in it.

How the population is decided
-----------------------------
Not by a lookup. :func:`is_eighty_series` is applied to every candidate row, and
the candidates are drawn from every source that names a material -- MARD, MARC,
EKPO, ZMM065 and MARA -- rather than from the material master alone.

That matters more than the task plan anticipated. Measured on 11-Sep:

    v_mara     362 distinct 80-series materials   (8000005632 .. 8000006059)
    v_mard   3,602                                (8000000000 .. 8000006054)
    v_marc   2,350
    v_zmm065 1,300
    v_ekpo   1,123   of which 371 on repair PO lines
    ------------------------------------------------------------------
    union    3,605 materials / 3,801 material+plant rows

The material master is not merely truncated at the top: it holds about a tenth
of the series. Gating the universe on MARA -- or reporting 362 as its size --
understates it by an order of magnitude. The per-source counts are served
alongside the rows (:class:`UniverseStats`) so any figure quoted downstream can
carry its source, which is exactly what section 6 of the task plan asks for.

Partial sources are the norm, not the exception
-----------------------------------------------
Stock, reorder point and criticality each cover a different subset, and MARC
holds no Gamsberg rows at all. Every one of these fields is therefore nullable,
and a null is a correct answer meaning "this source does not know". None of them
is ever defaulted -- an unknown criticality displayed as NORMAL is worse than
one displayed as unknown.

Usable and damaged stock (27-Oct-2026)
--------------------------------------
MARD's total counts every unit on the shelf, damaged ones included, so a part
holding only a DAMAGED unit read as "1 in stock". SAP records each unit's
condition beside it: in MCHB's batch field (the default source) or in MBEW's
valuation type (``I8_STOCK_CONDITION_SOURCE``). One grouped read per build gives
the stock per condition, and :func:`condition_stock` splits it into usable and
damaged with the configured condition lists. ``stock_on_hand`` stays the MARD
total; the split sits beside it, and is None -- unknown, not zero -- for a part
with no condition rows. On the 15-Oct extract MCHB's total equals MARD's on all
730 stocked 80-series parts, and the 1,516 MARD rows with no MCHB row all hold
nothing.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import RowMapping, text
from sqlalchemy.orm import Session

from app.core.db import MSSQL
from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, series_like_patterns
from app.initiatives.i8.staged import Sources, reading
from app.shared import CriticalitySource, get_criticality_source
from app.shared.sql_lists import like_any

logger = get_logger(__name__)

SOURCES: tuple[str, ...] = ("mara", "mard", "marc", "ekpo", "zmm065")


@dataclass(frozen=True)
class UniverseRow:
    """One repairable material at one plant."""

    material_id: str
    """The canonical (leading-zeros-stripped) material number."""

    plant: str | None
    """Plant code. None for a material that exists in no plant-level source."""

    description: str | None

    material_type: str | None
    """ZREP where MARA has a row. None for the ~90% of the series it omits."""

    stock_on_hand: Decimal | None
    """Unrestricted stock, SUMMED across storage locations. None if no MARD row."""

    storage_locations: int
    """How many bins the stock was summed over. Makes the sum auditable."""

    reorder_point: Decimal | None
    """None for Gamsberg (1500) -- the July MARC extract has no rows for it."""

    mrp_type: str | None

    planned_delivery_days: int | None
    """MARC lead time for buying a NEW one. The number that makes a repair
    worth waiting for, and the same MARC-coverage limit applies."""

    criticality: str | None
    """One of the five ZMM065 tiers, or None. Never defaulted."""

    open_repair_lines: int
    qty_under_repair: Decimal

    in_material_master: bool
    """Whether MARA knows this material at all. A data-completeness signal."""

    usable_stock: Decimal | None = None
    """Units in a usable condition (I8_USABLE_STOCK_CONDITIONS: new and
    refurbished), from :attr:`stock_condition_source`. None when the part has
    no condition rows -- unknown, never zero."""

    damaged_stock: Decimal | None = None
    """Units in a damaged condition (I8_DAMAGED_STOCK_CONDITIONS). None when
    the part has no condition rows."""

    stock_by_condition: tuple[tuple[str, Decimal], ...] = ()
    """Every condition SAP holds for the part with its units, by condition
    name -- including one counted as neither (MBEW's SCRAP). Empty when none."""

    stock_condition_source: str | None = None
    """``MCHB`` or ``MBEW``: where the split came from. None when the part has
    no condition rows there, so only the MARD total is known."""

    @property
    def has_open_repair(self) -> bool:
        return self.open_repair_lines > 0


@dataclass(frozen=True)
class UniverseStats:
    """Per-source counts, so no figure is ever quoted without its source."""

    rows: int
    materials: int
    plants: int
    by_source: dict[str, int] = field(default_factory=dict)
    with_stock: int = 0
    with_reorder_point: int = 0
    with_criticality: int = 0
    with_criticality_from_other_plant: int = 0
    """Of ``with_criticality``, how many got their tier from a plant that is
    not their own -- see :func:`resolve_criticality`. Reported rather than
    hidden, because it is the one part of the tier that is inferred."""

    with_open_repair: int = 0
    in_material_master: int = 0
    with_stock_condition: int = 0
    """Rows whose stock is split by condition (27-Oct-2026)."""


# Candidate rows, drawn from every source that names a material.
#
# The LIKE is a prefilter generated from configuration, never a hard-coded 80,
# and it is looser than the real test -- is_eighty_series() below is what
# actually decides. See material_number.series_like_patterns.
_CANDIDATES_SQL = """
with mat as (
    select matnr from v_mara   where {matnr_like}
    union select matnr from v_mard   where {matnr_like}
    union select matnr from v_marc   where {matnr_like}
    union select matnr from v_ekpo   where {matnr_like}
    union select matnr from v_zmm065 where {matnr_like}
),
plants as (
    select matnr, werks from v_mard   where {matnr_like} and werks is not null
    union select matnr, werks from v_marc   where {matnr_like} and werks is not null
    union select matnr, werks from v_ekpo   where {matnr_like} and werks is not null
    union select matnr, werks from v_zmm065 where {matnr_like} and werks is not null
),
stock as (
    -- SUMMED across storage locations. MARD is one row per bin, and a material
    -- sits in several: taking one row under-reports stock, which is the exact
    -- failure I08 exists to prevent.
    select matnr, werks, sum(labst) as labst, count(*) as locations
    from v_mard where {matnr_like} and werks is not null
    group by matnr, werks
),
planning as (
    -- MARC is one row per material+plant, so max() picks that single value.
    select matnr, werks, max(minbe) as minbe, max(dismm) as dismm,
           max(plifz) as plifz
    from v_marc where {matnr_like} and werks is not null
    group by matnr, werks
),
-- Description candidates, one aggregate per source.
--
-- Deliberately NOT correlated subqueries. A coalesce() of four per-material
-- lookups reads well and is 14,000 sequential scans over 82k-133k row views;
-- the first version of this query took minutes. Aggregating each source once
-- and hash-joining the results takes about a second.
zmm_desc as (
    select matnr, min(maktx) as maktx from v_zmm065
    where {matnr_like} and maktx is not null group by matnr
),
makt_desc as (
    select matnr, min(maktx) as maktx from v_makt
    where {matnr_like} and maktx is not null group by matnr
),
mara_row as (
    select matnr, min(maktx) as maktx, max(mtart) as mtart from v_mara
    where {matnr_like} group by matnr
),
ekpo_desc as (
    select matnr, min(txz01) as txz01 from v_ekpo
    where {matnr_like} and txz01 is not null group by matnr
),
marc_mat as (select distinct matnr from v_marc where {matnr_like}),
mard_mat as (select distinct matnr from v_mard where {matnr_like}),
ekpo_mat as (select distinct matnr from v_ekpo where {matnr_like}),
zmm_mat  as (select distinct matnr from v_zmm065 where {matnr_like})
select
    m.matnr                  as matnr,
    pl.werks                 as werks,
    -- Description precedence: ZMM065 first because it covers roughly four
    -- times as many repair materials as MAKT, then MAKT, then MARA, then the
    -- PO short text -- the only description that exists at all for a material
    -- named on a purchase order and nowhere else.
    coalesce(zd.maktx, kd.maktx, ar.maktx, pd.txz01) as maktx,
    ar.mtart                 as mtart,
    s.labst                  as labst,
    coalesce(s.locations, 0) as locations,
    p.minbe                  as minbe,
    p.dismm                  as dismm,
    p.plifz                  as plifz,
    case when ar.matnr is not null then 1 else 0 end as in_mara,
    case when dm.matnr is not null then 1 else 0 end as in_mard,
    case when cm.matnr is not null then 1 else 0 end as in_marc,
    case when pm.matnr is not null then 1 else 0 end as in_ekpo,
    case when zm.matnr is not null then 1 else 0 end as in_zmm065
from mat m
left join plants    pl on pl.matnr = m.matnr
left join stock     s  on s.matnr  = m.matnr and s.werks = pl.werks
left join planning  p  on p.matnr  = m.matnr and p.werks = pl.werks
left join zmm_desc  zd on zd.matnr = m.matnr
left join makt_desc kd on kd.matnr = m.matnr
left join mara_row  ar on ar.matnr = m.matnr
left join ekpo_desc pd on pd.matnr = m.matnr
left join mard_mat  dm on dm.matnr = m.matnr
left join marc_mat  cm on cm.matnr = m.matnr
left join ekpo_mat  pm on pm.matnr = m.matnr
left join zmm_mat   zm on zm.matnr = m.matnr
"""

# SQL Server re-expands each CTE at every reference, re-running the per-row
# sap_key/sap_num helpers each time, so these slices are staged once instead.
_SOURCE_COLUMNS: dict[str, str] = {
    "v_mara": "matnr, maktx, mtart",
    "v_makt": "matnr, maktx",
    "v_marc": "matnr, werks, minbe, dismm, plifz",
    "v_mard": "matnr, werks, labst",
    "v_ekpo": "matnr, werks, txz01",
    "v_zmm065": "matnr, werks, maktx",
}
_VIEWS: dict[str, str] = {view: view for view in _SOURCE_COLUMNS}
_SOURCE_NAME = re.compile(r"\b(?:" + "|".join(_SOURCE_COLUMNS) + r")\b")


def _render_candidates(sources: Mapping[str, str], matnr_like: str) -> str:
    """The candidates query, reading each view from ``sources[view]``."""
    return _SOURCE_NAME.sub(lambda m: sources[m.group(0)], _CANDIDATES_SQL).replace(
        "{matnr_like}", matnr_like
    )


def fetch_universe_candidates(
    db: Session,
    matnr_like: str,
    params: Mapping[str, str],
    *,
    sources: Mapping[str, str] = _VIEWS,
) -> Sequence[RowMapping]:
    """Run the candidates query, staged through temp tables on SQL Server."""
    if db.get_bind().dialect.name != MSSQL:
        return db.execute(text(_render_candidates(sources, matnr_like)), params).mappings().all()

    staged = {view: "#i8_" + view.removeprefix("v_") for view in _SOURCE_COLUMNS}
    for view, table in staged.items():
        columns, source = _SOURCE_COLUMNS[view], sources[view]
        db.execute(text(f"drop table if exists {table}"))
        # Created without bind parameters: a parameterised statement runs in
        # sp_executesql, which drops any temp table it creates when it returns.
        db.execute(text(f"select {columns} into {table} from {source} where 1 = 0"))
        db.execute(
            text(f"insert into {table} select {columns} from {source} where {matnr_like}"),
            params,
        )
    rows = db.execute(text(_render_candidates(staged, matnr_like)), params).mappings().all()
    for table in staged.values():
        db.execute(text(f"drop table {table}"))
    return rows


# --- stock by condition (27-Oct-2026) -----------------------------------

# One grouped read per build, chosen by I8_STOCK_CONDITION_SOURCE. Both give
# (matnr, werks, stock_condition, quantity); which conditions are usable or
# damaged is decided in Python (condition_stock), never here.
#
# MCHB: one row per storage location and condition, summed like MARD.
# MBEW: one row per condition, plus a blank-BWTAR row aggregating them -- left
# out here, and again by condition_stock, as it is no condition at all.
CONDITION_STOCK_SQL: dict[str, str] = {
    "MCHB": """
select matnr, werks, charg as stock_condition, sum(clabs) as quantity
from v_mchb
where {matnr_like} and werks is not null
group by matnr, werks, charg
""",
    "MBEW": """
select matnr, bwkey as werks, bwtar as stock_condition, sum(lbkum) as quantity
from v_mbew
where {matnr_like} and bwkey is not null and bwtar is not null and bwtar <> ''
group by matnr, bwkey, bwtar
""",
}


@dataclass(frozen=True)
class ConditionStock:
    """One part's stock at one plant, split by condition."""

    usable: Decimal
    damaged: Decimal
    by_condition: tuple[tuple[str, Decimal], ...]
    source: str


def condition_stock(
    rows: Iterable[Mapping],
    *,
    source: str,
    usable: Sequence[str],
    damaged: Sequence[str],
) -> dict[tuple[str, str | None], ConditionStock]:
    """(material, plant) -> its stock split by condition.

    ``rows`` carry ``matnr``, ``werks``, ``stock_condition`` and ``quantity``,
    as :data:`CONDITION_STOCK_SQL` returns them. A row with no condition (MBEW's
    blank-BWTAR aggregate, an unbatched MCHB row) or no readable quantity says
    nothing about condition and is skipped; a part left with no rows gets no
    entry, so its split stays unknown rather than zero. Conditions are compared
    upper-cased, as the settings lists are. One counted as neither usable nor
    damaged (MBEW's SCRAP) still appears in ``by_condition``.
    """
    usable_set, damaged_set = set(usable), set(damaged)
    totals: dict[tuple[str, str | None], dict[str, Decimal]] = {}
    for row in rows:
        condition = (row["stock_condition"] or "").strip().upper()
        quantity = row["quantity"]
        if not condition or quantity is None:
            continue
        by_condition = totals.setdefault((row["matnr"], row["werks"]), {})
        by_condition[condition] = by_condition.get(condition, Decimal(0)) + Decimal(quantity)

    split: dict[tuple[str, str | None], ConditionStock] = {}
    for key, by_condition in totals.items():
        split[key] = ConditionStock(
            usable=sum((q for c, q in by_condition.items() if c in usable_set), Decimal(0)),
            damaged=sum((q for c, q in by_condition.items() if c in damaged_set), Decimal(0)),
            by_condition=tuple(sorted(by_condition.items())),
            source=source,
        )
    return split


def fetch_condition_stock(
    db: Session,
    cfg: I8Settings,
    matnr_like: str,
    params: Mapping[str, str],
    sources: Sources | None = None,
) -> dict[tuple[str, str | None], ConditionStock]:
    """The configured source's stock per condition, split -- one query."""
    source = cfg.stock_condition_source
    sql = reading(CONDITION_STOCK_SQL[source].replace("{matnr_like}", matnr_like), sources)
    rows = db.execute(text(sql), params).mappings().all()
    return condition_stock(
        rows,
        source=source,
        usable=cfg.usable_stock_condition_list,
        damaged=cfg.damaged_stock_condition_list,
    )


# (material, plant) -> (open repair lines, quantity out for repair)
OpenRepairIndex = dict[tuple[str, str | None], tuple[int, Decimal]]

# (material, plant) -> tier name, or None where no source knows.
CriticalityMap = dict[tuple[str, str | None], str | None]


def resolve_criticality(
    keys: list[tuple[str, str | None]],
    source: CriticalitySource | None = None,
) -> tuple[CriticalityMap, set[tuple[str, str | None]]]:
    """Every row's criticality tier, from the shared W3.4 source, in one batch.

    I08 does not own criticality and no longer reads ZMM065 itself: the tier is
    one business fact, and W3.4 exists so all three initiatives get the same
    answer for it. This function is only the shape adapter -- the port answers
    one material-plant at a time, and the universe holds thousands of them.

    **Why this is a batch and not a loop.** ``get()`` opens its own database
    session, measured at 6.8 ms. Calling it per row is 26 seconds over today's
    3,802 rows, against a 5.7 s build before criticality. ``get_many()`` was
    added to the shared port for exactly this, and does it in one round trip.

    **The plant fallback, stated plainly.** ZMM065 covers both in-scope plants,
    but not every material in the universe is described at both of them. Where
    a material+plant has no ZMM065 row, the material's tier *where it is known*
    is used -- but only when it carries the same tier everywhere it appears.
    A material that is CRITICAL at one plant and NORMAL at another is answered
    with nothing, because there is no honest way to pick. That is the shared
    port's own plant-less rule (``get(material, None)``), not a local invention,
    and the keys it applied to come back in the second return value so the count
    can be reported rather than buried.

    The predecessor to this function kept an any-plant entry per material on a
    first-value-wins basis, which silently picked a winner for the 63 materials
    that disagree across plants. This does not.

    Returns ``(tier by key, keys answered from another plant)``.
    """
    source = source or get_criticality_source()
    wanted = list(dict.fromkeys(keys))

    # Ask for both shapes in one call: the exact material-plant, and the
    # plant-less form used only where the exact one has no answer.
    exact = [key for key in wanted if key[1] is not None]
    plantless: list[tuple[str, str | None]] = list(
        dict.fromkeys((material, None) for material, _plant in wanted)
    )
    answers = source.get_many(exact + plantless)

    resolved: CriticalityMap = {}
    from_other_plant: set[tuple[str, str | None]] = set()

    for key in wanted:
        material, plant = key
        direct = answers.get(key)
        if direct is not None and direct.found and direct.tier is not None:
            resolved[key] = direct.tier.value
            continue

        if plant is not None:
            # No row for this material at this plant. Fall back to the tier the
            # material carries elsewhere, if it is unambiguous.
            elsewhere = answers.get((material, None))
            if elsewhere is not None and elsewhere.found and elsewhere.tier is not None:
                resolved[key] = elsewhere.tier.value
                from_other_plant.add(key)
                continue

        # Never defaulted. None means "no source knows", which is a correct
        # answer and must render as unknown -- see the module docstring.
        resolved[key] = None

    return resolved, from_other_plant


def load_universe(
    db: Session,
    cfg: I8Settings | None = None,
    open_repair_index: OpenRepairIndex | None = None,
    criticality_source: CriticalitySource | None = None,
    sources: Sources | None = None,
) -> tuple[list[UniverseRow], UniverseStats]:
    """Build the repairable universe.

    ``open_repair_index`` maps (material, plant) to (open line count, quantity
    out for repair). It is passed in rather than computed here so the repair-PO
    rule lives in exactly one place -- ``register.py``, where the Pstyp ruling
    is applied and tested. Pass None to build the universe without repair
    state, which is what W5.1 delivered on its own.

    ``criticality_source`` is the shared W3.4 port. It defaults to the
    configured process-wide source and is injectable so a test can vary the
    tiers without a database.

    ``sources`` is a snapshot build's staged copies -- see ``staged.py``.
    """
    cfg = cfg or get_i8_settings()
    patterns = series_like_patterns(cfg)
    index = open_repair_index or {}

    # One OR of LIKEs, reused by every source below: SQL Server has no
    # ``like any(array)``. Each pattern is still a bind parameter.
    matnr_like, pattern_params = like_any("matnr", "pattern", patterns)
    # A snapshot build's staged copies (staged.py) where it has them.
    views = _VIEWS if sources is None else {view: reading(view, sources) for view in _SOURCE_COLUMNS}
    rows = fetch_universe_candidates(db, matnr_like, pattern_params, sources=views)

    # THE gate, applied before anything else looks at these rows. The database
    # prefilter above only narrowed the candidates; this is what decides, and it
    # is the same function the unit tests run against the CPI zero-padded form.
    #
    # Filtering here rather than inside the build loop is what lets criticality
    # be fetched in one batch below: there is no reason to look up a tier for a
    # row the predicate is about to reject.
    repairable = [row for row in rows if is_eighty_series(row["matnr"], cfg)]

    criticality_map, inferred_from_other_plant = resolve_criticality(
        [(row["matnr"], row["werks"]) for row in repairable],
        source=criticality_source,
    )
    conditions = fetch_condition_stock(db, cfg, matnr_like, pattern_params, sources)

    universe: list[UniverseRow] = []
    by_source = dict.fromkeys(SOURCES, 0)
    seen_materials: set[str] = set()
    seen_plants: set[str] = set()

    for row in repairable:
        material_id = row["matnr"]
        plant = row["werks"]
        open_lines, qty_under_repair = index.get((material_id, plant), (0, Decimal(0)))
        condition = conditions.get((material_id, plant))

        universe.append(
            UniverseRow(
                material_id=material_id,
                plant=plant,
                description=row["maktx"],
                material_type=row["mtart"],
                stock_on_hand=row["labst"],
                storage_locations=row["locations"],
                reorder_point=row["minbe"],
                mrp_type=row["dismm"],
                planned_delivery_days=(
                    int(row["plifz"]) if row["plifz"] is not None else None
                ),
                criticality=criticality_map.get((material_id, plant)),
                open_repair_lines=open_lines,
                qty_under_repair=qty_under_repair,
                in_material_master=bool(row["in_mara"]),
                usable_stock=condition.usable if condition else None,
                damaged_stock=condition.damaged if condition else None,
                stock_by_condition=condition.by_condition if condition else (),
                stock_condition_source=condition.source if condition else None,
            )
        )

        if material_id not in seen_materials:
            seen_materials.add(material_id)
            for source in SOURCES:
                if row[f"in_{source}"]:
                    by_source[source] += 1
        if plant:
            seen_plants.add(plant)

    stats = UniverseStats(
        rows=len(universe),
        materials=len(seen_materials),
        plants=len(seen_plants),
        by_source=by_source,
        with_stock=sum(1 for r in universe if r.stock_on_hand is not None),
        with_reorder_point=sum(1 for r in universe if r.reorder_point is not None),
        with_criticality=sum(1 for r in universe if r.criticality is not None),
        with_criticality_from_other_plant=sum(
            1 for r in universe if (r.material_id, r.plant) in inferred_from_other_plant
        ),
        with_open_repair=sum(1 for r in universe if r.has_open_repair),
        in_material_master=sum(1 for r in universe if r.in_material_master),
        with_stock_condition=sum(1 for r in universe if r.stock_condition_source is not None),
    )
    logger.info(
        "I08 universe: %d rows, %d materials, %d plants (MARA knows %d); "
        "criticality on %d rows, %d of them inferred from another plant; "
        "stock split by condition (%s) on %d rows",
        stats.rows,
        stats.materials,
        stats.plants,
        stats.in_material_master,
        stats.with_criticality,
        stats.with_criticality_from_other_plant,
        cfg.stock_condition_source,
        stats.with_stock_condition,
    )
    return universe, stats
