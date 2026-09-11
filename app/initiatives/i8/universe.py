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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.criticality import criticality_for, load_criticality
from app.initiatives.i8.material_number import is_eighty_series, series_like_patterns

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
    """None outside plants 1300 and 1200 -- MARC has no Gamsberg rows."""

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
    with_open_repair: int = 0
    in_material_master: int = 0


# Candidate rows, drawn from every source that names a material.
#
# The LIKE is a prefilter generated from configuration, never a hard-coded 80,
# and it is looser than the real test -- is_eighty_series() below is what
# actually decides. See material_number.series_like_patterns.
_CANDIDATES_SQL = """
with mat as (
    select matnr from v_mara   where matnr like any(:patterns)
    union select matnr from v_mard   where matnr like any(:patterns)
    union select matnr from v_marc   where matnr like any(:patterns)
    union select matnr from v_ekpo   where matnr like any(:patterns)
    union select matnr from v_zmm065 where matnr like any(:patterns)
),
plants as (
    select matnr, werks from v_mard   where matnr like any(:patterns) and werks is not null
    union select matnr, werks from v_marc   where matnr like any(:patterns) and werks is not null
    union select matnr, werks from v_ekpo   where matnr like any(:patterns) and werks is not null
    union select matnr, werks from v_zmm065 where matnr like any(:patterns) and werks is not null
),
stock as (
    -- SUMMED across storage locations. MARD is one row per bin, and a material
    -- sits in several: taking one row under-reports stock, which is the exact
    -- failure I08 exists to prevent.
    select matnr, werks, sum(labst) as labst, count(*) as locations
    from v_mard where matnr like any(:patterns) and werks is not null
    group by 1, 2
),
planning as (
    -- MARC is one row per material+plant, so max() picks that single value.
    select matnr, werks, max(minbe) as minbe, max(dismm) as dismm,
           max(plifz) as plifz
    from v_marc where matnr like any(:patterns) and werks is not null
    group by 1, 2
),
-- Description candidates, one aggregate per source.
--
-- Deliberately NOT correlated subqueries. A coalesce() of four per-material
-- lookups reads well and is 14,000 sequential scans over 82k-133k row views;
-- the first version of this query took minutes. Aggregating each source once
-- and hash-joining the results takes about a second.
zmm_desc as (
    select matnr, min(maktx) as maktx from v_zmm065
    where matnr like any(:patterns) and maktx is not null group by 1
),
makt_desc as (
    select matnr, min(maktx) as maktx from v_makt
    where matnr like any(:patterns) and maktx is not null group by 1
),
mara_row as (
    select matnr, min(maktx) as maktx, max(mtart) as mtart from v_mara
    where matnr like any(:patterns) group by 1
),
ekpo_desc as (
    select matnr, min(txz01) as txz01 from v_ekpo
    where matnr like any(:patterns) and txz01 is not null group by 1
),
marc_mat as (select distinct matnr from v_marc where matnr like any(:patterns)),
mard_mat as (select distinct matnr from v_mard where matnr like any(:patterns)),
ekpo_mat as (select distinct matnr from v_ekpo where matnr like any(:patterns)),
zmm_mat  as (select distinct matnr from v_zmm065 where matnr like any(:patterns))
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
    (ar.matnr is not null)   as in_mara,
    (dm.matnr is not null)   as in_mard,
    (cm.matnr is not null)   as in_marc,
    (pm.matnr is not null)   as in_ekpo,
    (zm.matnr is not null)   as in_zmm065
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

# (material, plant) -> (open repair lines, quantity out for repair)
OpenRepairIndex = dict[tuple[str, str | None], tuple[int, Decimal]]


def load_universe(
    db: Session,
    cfg: I8Settings | None = None,
    open_repair_index: OpenRepairIndex | None = None,
) -> tuple[list[UniverseRow], UniverseStats]:
    """Build the repairable universe.

    ``open_repair_index`` maps (material, plant) to (open line count, quantity
    out for repair). It is passed in rather than computed here so the repair-PO
    rule lives in exactly one place -- ``register.py``, where the Pstyp ruling
    is applied and tested. Pass None to build the universe without repair
    state, which is what W5.1 delivered on its own.
    """
    cfg = cfg or get_i8_settings()
    patterns = series_like_patterns(cfg)
    index = open_repair_index or {}

    rows = db.execute(text(_CANDIDATES_SQL), {"patterns": patterns}).mappings().all()
    criticality_map = load_criticality(db, cfg)

    universe: list[UniverseRow] = []
    by_source = dict.fromkeys(SOURCES, 0)
    seen_materials: set[str] = set()
    seen_plants: set[str] = set()

    for row in rows:
        material_id = row["matnr"]

        # THE gate. The database prefilter above only narrowed the candidates;
        # this is what decides, and it is the same function the unit tests run
        # against the CPI zero-padded form.
        if not is_eighty_series(material_id, cfg):
            continue

        plant = row["werks"]
        open_lines, qty_under_repair = index.get((material_id, plant), (0, Decimal(0)))

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
                criticality=criticality_for(criticality_map, material_id, plant),
                open_repair_lines=open_lines,
                qty_under_repair=qty_under_repair,
                in_material_master=bool(row["in_mara"]),
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
        with_open_repair=sum(1 for r in universe if r.has_open_repair),
        in_material_master=sum(1 for r in universe if r.in_material_master),
    )
    logger.info(
        "I08 universe: %d rows, %d materials, %d plants (MARA knows %d)",
        stats.rows,
        stats.materials,
        stats.plants,
        stats.in_material_master,
    )
    return universe, stats
