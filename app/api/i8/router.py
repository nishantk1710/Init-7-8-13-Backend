"""HTTP routes for Initiative 08, mounted at /api/i8.

Read-only, end to end. No endpoint here writes anything -- not to our database
and not to SAP. W5.1 and W5.2 are read models; the first write path in I08 is
W5.3's attestation, and it is not in this window.

Routes parse input, call a service and shape a response. Every rule lives in
``app/initiatives/i8``, which is what makes the rules testable without an HTTP
client and what keeps the repair-PO convention in one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.i8.mappers import (
    register_meta,
    repair_chain,
    timeline,
    universe_item,
    universe_meta,
    vendor_item,
)
from app.api.i8.schemas import (
    MaterialReference,
    RepairChain,
    RegisterResponse,
    RepairDetail,
    SnapshotInfo,
    UniverseDetail,
    UniverseResponse,
    VendorResponse,
)
from app.core.db import get_db
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.service import Snapshot, get_snapshot
from app.initiatives.i8.universe import UniverseRow
from app.shared import get_criticality_source

router = APIRouter(prefix="/i8", tags=["i8 - refurbishable spares"])

T = TypeVar("T")


def snapshot_dependency(db: Session = Depends(get_db)) -> Snapshot:
    """The assembled I08 read models, built once per process."""
    return get_snapshot(db)


SnapshotDep = Annotated[Snapshot, Depends(snapshot_dependency)]
SettingsDep = Annotated[I8Settings, Depends(get_i8_settings)]


def _paginate(
    items: Sequence[T], page: int, page_size: int
) -> tuple[list[T], int]:
    """One page, and the unpaged total.

    Paging is not optional: the register holds 1,225 lines and 788 of them are
    open. Sending them all in one response is how a UI table becomes unusable.
    """
    start = (page - 1) * page_size
    return list(items[start : start + page_size]), len(items)


def _stock_lookup(snapshot: Snapshot) -> dict[tuple[str, str | None], UniverseRow]:
    """(material, plant) -> universe row.

    Built once per request and passed down. Building it inside the per-line
    mapper instead would rebuild a 3,802-entry dict for every row on the page.
    """
    return {(row.material_id, row.plant): row for row in snapshot.universe}


def _chain(
    line: RepairLine,
    cfg: I8Settings,
    lookup: dict[tuple[str, str | None], UniverseRow],
) -> RepairChain:
    """A register row enriched with its material's stock position.

    Stock and reorder point live on the material+plant, not on the PO line, so
    they come from the universe rather than from a second query.
    """
    row = lookup.get((line.material_id, line.plant))
    return repair_chain(
        line,
        cfg,
        stock_on_hand=row.stock_on_hand if row else None,
        reorder_point=row.reorder_point if row else None,
        new_unit_lead_time_days=row.planned_delivery_days if row else None,
    )


# --- W5.1: the repairable universe ----------------------------------------


@router.get(
    "/universe",
    response_model=UniverseResponse,
    summary="Repairable materials (80-series), one row per material + plant",
)
def get_universe(
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    plant: str | None = Query(None, description="Plant code, e.g. 1300"),
    criticality: str | None = Query(
        None, description="NORMAL, OBSOLETE, CRITICAL, IMPACT or INSURANCE"
    ),
    has_open_repair: bool | None = Query(
        None, alias="hasOpenRepair", description="Only materials out for repair"
    ),
    in_material_master: bool | None = Query(
        None,
        alias="inMaterialMaster",
        description=(
            "Only materials the MARA extract knows. Set true to reproduce the "
            "362-material figure quoted in the plan; the full universe is "
            "roughly ten times that."
        ),
    ),
    search: str | None = Query(None, description="Material number or description"),
    page: int = Query(1, ge=1),
    page_size: int = Query(None, alias="pageSize", ge=1),
) -> UniverseResponse:
    page_size = min(page_size or cfg.default_page_size, cfg.max_page_size)

    rows = snapshot.universe
    if plant:
        rows = tuple(r for r in rows if r.plant == plant)
    if criticality:
        wanted = criticality.strip().upper()
        rows = tuple(r for r in rows if r.criticality == wanted)
    if has_open_repair is not None:
        rows = tuple(r for r in rows if r.has_open_repair is has_open_repair)
    if in_material_master is not None:
        rows = tuple(r for r in rows if r.in_material_master is in_material_master)
    if search:
        needle = search.strip().lower()
        rows = tuple(
            r
            for r in rows
            if needle in r.material_id.lower()
            or (r.description and needle in r.description.lower())
        )

    page_rows, total = _paginate(rows, page, page_size)
    return UniverseResponse(
        items=[universe_item(row, cfg) for row in page_rows],
        page=page,
        page_size=page_size,
        total=total,
        meta=universe_meta(snapshot.universe_stats),
        reference_date=snapshot.reference_date,
    )


@router.get(
    "/universe/{material_id}",
    response_model=UniverseDetail,
    summary="One repairable material, its plants and its repair history",
)
def get_universe_material(
    material_id: str, snapshot: SnapshotDep, cfg: SettingsDep
) -> UniverseDetail:
    # Normalised on the way in, so a caller may pass either the extract form
    # (8000005632) or the zero-padded CPI form (000000008000005632).
    key = normalise(material_id)
    if key is None or not is_eighty_series(key, cfg):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"{material_id!r} is not a repairable 80-series material number."
            ),
        )

    rows = snapshot.material(key)
    lines = snapshot.lines_for_material(key)
    if not rows and not lines:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No repairable material {key} in this extract.",
        )

    description = next(
        (r.description for r in rows if r.description),
        next((line.description for line in lines if line.description), None),
    )
    return UniverseDetail(
        material=MaterialReference(
            material_id=key, material_code=key, description=description
        ),
        plants=[universe_item(row, cfg) for row in rows],
        repair_lines=[_chain(line, cfg, _stock_lookup(snapshot)) for line in lines],
    )


# --- W5.2: the repair register --------------------------------------------


@router.get(
    "/register",
    response_model=RegisterResponse,
    summary="Repair lines, at material + repair-PO-line grain",
)
def get_register(
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    plant: str | None = Query(None),
    vendor: str | None = Query(None),
    material: str | None = Query(None),
    repair_status: str | None = Query(None, alias="status"),
    overdue_only: bool = Query(False, alias="overdueOnly"),
    open_only: bool = Query(False, alias="openOnly"),
    criticality: str | None = Query(None),
    aging_bucket: str | None = Query(None, alias="agingBucket"),
    page: int = Query(1, ge=1),
    page_size: int = Query(None, alias="pageSize", ge=1),
) -> RegisterResponse:
    page_size = min(page_size or cfg.default_page_size, cfg.max_page_size)

    lines = snapshot.lines
    if plant:
        lines = tuple(line for line in lines if line.plant == plant)
    if vendor:
        lines = tuple(line for line in lines if line.vendor == vendor)
    if material:
        key = normalise(material)
        lines = tuple(line for line in lines if line.material_id == key)
    if repair_status:
        lines = tuple(line for line in lines if line.repair_status == repair_status)
    if aging_bucket:
        lines = tuple(line for line in lines if line.aging_bucket == aging_bucket)
    if overdue_only:
        lines = tuple(line for line in lines if line.is_overdue)
    if open_only:
        lines = tuple(line for line in lines if line.is_open)
    if criticality:
        wanted = criticality.strip().upper()
        by_material = {
            row.material_id
            for row in snapshot.universe
            if row.criticality == wanted
        }
        lines = tuple(line for line in lines if line.material_id in by_material)

    page_lines, total = _paginate(lines, page, page_size)
    lookup = _stock_lookup(snapshot)
    return RegisterResponse(
        items=[_chain(line, cfg, lookup) for line in page_lines],
        page=page,
        page_size=page_size,
        total=total,
        meta=register_meta(snapshot.register_stats),
        reference_date=snapshot.reference_date,
    )


@router.get(
    "/register/{document}/{item}",
    response_model=RepairDetail,
    summary="One repair line, with its full lifecycle timeline",
)
def get_repair_line(
    document: str, item: str, snapshot: SnapshotDep, cfg: SettingsDep
) -> RepairDetail:
    line = snapshot.line(document, item)
    if line is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No repair line {document}/{item} in this extract.",
        )
    return RepairDetail(
        line=_chain(line, cfg, _stock_lookup(snapshot)),
        timeline=timeline(line, snapshot.reference_date),
    )


# --- W5.2 Layer 4: vendor analytics ---------------------------------------


@router.get(
    "/vendors/turnaround",
    response_model=VendorResponse,
    summary="Vendor turnaround analytics over completed repairs",
)
def get_vendor_turnaround(snapshot: SnapshotDep) -> VendorResponse:
    return VendorResponse(
        items=[vendor_item(v) for v in snapshot.vendors],
        total=len(snapshot.vendors),
        reference_date=snapshot.reference_date,
        note=(
            "Averages are over COMPLETED repairs only -- including open ones "
            "would make the slowest vendor look fastest. Vendors come from the "
            "PO header, so the 455 repair lines with no header in this extract "
            "are grouped under UNKNOWN rather than dropped. LFA1 resolves only "
            "4 of the 61 repair vendors to a name; the rest show their code."
        ),
    )


# --- Diagnostics -----------------------------------------------------------


@router.get(
    "/snapshot",
    response_model=SnapshotInfo,
    summary="What the register and universe currently hold, and the rules in force",
)
def get_snapshot_info(snapshot: SnapshotDep, cfg: SettingsDep) -> SnapshotInfo:
    """Every headline count in one place, with the settings that produced them.

    Exists so a surprising number in the UI can be traced to a setting without
    reading the deployment, and so the UAT pack can quote figures that are
    reproducible rather than remembered.
    """
    return SnapshotInfo(
        reference_date=snapshot.reference_date,
        built_at=snapshot.built_at,
        build_seconds=snapshot.build_seconds,
        repair_register=register_meta(snapshot.register_stats),
        universe=universe_meta(snapshot.universe_stats),
        rules={
            "seriesPrefixes": cfg.series_prefixes,
            "materialNumberLength": cfg.material_number_length,
            "repairItemCategory": cfg.repair_item_category,
            "repairDocType": cfg.repair_doc_type,
            "overdueGraceDays": cfg.overdue_grace_days,
            # The shared W3.4 source that actually answered, not an I08
            # setting -- I08 has not owned this since 15-Sep. A fallback chain
            # reports as "zzcritic+zmm065", which is the point: the name says
            # what really served the tiers.
            "criticalitySource": get_criticality_source().name,
            "referenceDate": cfg.reference_date or "(today)",
        },
    )
