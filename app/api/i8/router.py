"""HTTP routes for Initiative 08, mounted at /api/i8.

Routes parse input, call a service and shape a response. Every rule lives in
``app/initiatives/i8``, which is what makes the rules testable without an HTTP
client and what keeps the repair-PO convention in one place.

What this module is allowed to write
-------------------------------------
Through W5.2 the answer was "nothing", and a contract test asserted it against
the served OpenAPI spec. **W5.3 loosens that, deliberately and by exactly one
step**, so it is worth stating precisely what the guarantee is now:

* **ONE write path exists**: ``POST /api/i8/attestations``.
* It writes to **one table we own**, ``i8_attestation``, and that table is
  append-only -- no endpoint updates or deletes a row, and on Postgres a trigger
  makes the attempt an error.
* **Nothing here writes to SAP, ever.** Not a purchase order, not a movement,
  not a master-data field. The platform reads SAP and records its own findings
  beside it. That is the guarantee that actually matters, and it is unchanged.

``test_the_module_is_read_only_except_for_attestations`` asserts all three,
rather than being deleted when it went red.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.i8.mappers import (
    attestation_item,
    declaration_item,
    declaration_meta,
    exception_item,
    exception_meta,
    register_meta,
    repair_chain,
    timeline,
    universe_item,
    universe_meta,
    vendor_item,
)
from app.api.i8.schemas import (
    Attestation,
    AttestationRequest,
    AttestationResponse,
    DeclarationResponse,
    ExceptionResponse,
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
from app.initiatives.i8.attestation import (
    AttestationDraft,
    AttestationError,
    find as find_attestations,
    record as record_attestation,
)
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.service import (
    AttestationView,
    Snapshot,
    get_attestation_view,
    get_snapshot,
    reset_attestation_view,
)
from app.initiatives.i8.universe import UniverseRow
from app.shared import get_criticality_source

router = APIRouter(prefix="/i8", tags=["i8 - refurbishable spares"])

T = TypeVar("T")


def snapshot_dependency(db: Session = Depends(get_db)) -> Snapshot:
    """The assembled I08 read models, built once per process."""
    return get_snapshot(db)


SnapshotDep = Annotated[Snapshot, Depends(snapshot_dependency)]
SettingsDep = Annotated[I8Settings, Depends(get_i8_settings)]


def attestation_view_dependency(
    db: Session = Depends(get_db), snapshot: Snapshot = Depends(snapshot_dependency)
) -> AttestationView:
    """Coverage, the declaration queue and the exception queue.

    Cached separately from the snapshot: the July extract never changes, but
    attestations do, and a planner who records one must see it immediately.
    """
    return get_attestation_view(db, snapshot)


AttestationViewDep = Annotated[AttestationView, Depends(attestation_view_dependency)]
DbDep = Annotated[Session, Depends(get_db)]


def _current_user() -> str:
    """Who is attesting.

    **A placeholder, and named as one.** Entra sign-in is not wired into this
    module yet (``app/integrations/entra``), so there is no authenticated
    principal to read. Every attestation recorded before that lands is stamped
    with this value, which makes those rows obviously provisional rather than
    quietly attributing an audit record to a real person who did not make it.

    When auth arrives this becomes the caller's object id and nothing else
    changes -- ``record()`` already takes the attestor as an argument rather
    than reading it from the request body.
    """
    return "UNAUTHENTICATED_LOCAL_USER"


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
    declaration_statuses: dict[tuple[str, str], str] | None = None,
) -> RepairChain:
    """A register row enriched with its material's stock position.

    Stock and reorder point live on the material+plant, not on the PO line, so
    they come from the universe rather than from a second query. The declaration
    status comes from W5.3's attestation view for the same reason -- and because
    that table changes while the process runs, which the July snapshot does not.
    """
    row = lookup.get((line.material_id, line.plant))
    return repair_chain(
        line,
        cfg,
        stock_on_hand=row.stock_on_hand if row else None,
        reorder_point=row.reorder_point if row else None,
        new_unit_lead_time_days=row.planned_delivery_days if row else None,
        declaration_status=(declaration_statuses or {}).get(line.key, "Required"),
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
    material_id: str,
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    view: AttestationViewDep,
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
        repair_lines=[
            _chain(line, cfg, _stock_lookup(snapshot), view.declaration_status_by_line)
            for line in lines
        ],
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
    view: AttestationViewDep,
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
        items=[
            _chain(line, cfg, lookup, view.declaration_status_by_line)
            for line in page_lines
        ],
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
    document: str,
    item: str,
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    view: AttestationViewDep,
) -> RepairDetail:
    line = snapshot.line(document, item)
    if line is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No repair line {document}/{item} in this extract.",
        )
    return RepairDetail(
        line=_chain(line, cfg, _stock_lookup(snapshot), view.declaration_status_by_line),
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


# --- W5.3: attestation, declarations and exceptions ------------------------


@router.post(
    "/attestations",
    response_model=Attestation,
    status_code=status.HTTP_201_CREATED,
    summary="Record a condition-to-repair attestation",
)
def post_attestation(
    body: AttestationRequest,
    db: DbDep,
    cfg: SettingsDep,
) -> Attestation:
    """**The only write path in Initiative 08.**

    Writes one row to ``i8_attestation``, a table we own. It does not write to
    SAP and cannot: the platform has no write path into SAP, which is why this
    control can be recorded and reported but never enforced.

    An attestation is never updated. To correct one, POST again with
    ``supersedes`` set to the original's id -- that creates a new row, leaves
    the original readable, and the pair is the audit trail.
    """
    draft = AttestationDraft(
        material_id=body.material_id,
        plant=body.plant,
        quantity=body.quantity,
        condition_description=body.condition_description,
        fault_category=body.fault_category,
        recommendation=body.recommendation,
        serial_number=body.serial_number,
        evidence_reference=body.evidence_reference,
        supersedes=body.supersedes,
    )
    try:
        # The attestor comes from the caller, never from the body.
        stored = record_attestation(db, draft, attestor=_current_user(), cfg=cfg)
    except AttestationError as exc:
        # 422, not 400: the request was well-formed JSON that broke a business
        # rule -- an unknown fault category, a supersedes that points nowhere.
        # 422 as a literal: starlette has deprecated the
        # HTTP_422_UNPROCESSABLE_ENTITY constant and renamed it, and pinning the
        # number avoids breaking on whichever spelling this version has.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # The declaration and exception queues are derived from this table, so they
    # are now stale. Invalidated at the write site rather than on a timer: a
    # planner must see their own submission immediately.
    reset_attestation_view()

    return attestation_item(stored, cfg)


@router.get(
    "/attestations",
    response_model=AttestationResponse,
    summary="Recorded attestations, newest first",
)
def get_attestations(
    db: DbDep,
    cfg: SettingsDep,
    material_id: str | None = Query(
        None, alias="materialId", description="Material number, padded or not"
    ),
    plant: str | None = Query(None, description="Plant code, e.g. 1300"),
    current_only: bool = Query(
        False,
        alias="currentOnly",
        description=(
            "Hide attestations that a later amendment replaced. Off by default: "
            "the superseded rows are the audit history"
        ),
    ),
) -> AttestationResponse:
    rows = find_attestations(
        db,
        material_id=material_id,
        plant=plant,
        include_superseded=not current_only,
    )

    # Reverse index built once rather than a query per row.
    superseded_by = {r.supersedes: r.id for r in rows if r.supersedes}

    return AttestationResponse(
        items=[
            attestation_item(r, cfg, superseded_by=superseded_by.get(r.id)) for r in rows
        ],
        total=len(rows),
        # Served with the data so a form does not hard-code VZI's vocabulary.
        fault_categories=list(cfg.fault_category_list),
    )


@router.get(
    "/declarations",
    response_model=DeclarationResponse,
    summary="The condition-to-repair declaration queue",
)
def get_declarations(
    view: AttestationViewDep,
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    plant: str | None = Query(None, description="Plant code, e.g. 1300"),
    status_filter: str | None = Query(
        None,
        alias="status",
        description="Required, Pending, Completed or Flagged",
    ),
    outstanding_only: bool = Query(
        False,
        alias="outstandingOnly",
        description="Only rows wanting attention -- Required and Flagged",
    ),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, alias="pageSize", ge=1),
) -> DeclarationResponse:
    """One row per repair line: was the part assessed before it was sent away.

    The meta counts are over everything matching the filters, not over the page
    -- a queue that says "12" when it means "12 on this page" is worse than no
    number at all.
    """
    rows = view.declarations

    if plant:
        rows = tuple(r for r in rows if r.plant == plant)
    if status_filter:
        wanted = status_filter.strip().lower()
        rows = tuple(r for r in rows if r.status.lower() == wanted)
    if outstanding_only:
        rows = tuple(r for r in rows if r.is_outstanding)

    size = min(page_size or cfg.default_page_size, cfg.max_page_size)
    items, total = _paginate(rows, page, size)

    return DeclarationResponse(
        items=[declaration_item(r, cfg) for r in items],
        page=page,
        page_size=size,
        total=total,
        meta=declaration_meta(rows, view.coverage.window_days),
        reference_date=snapshot.reference_date,
    )


@router.get(
    "/exceptions",
    response_model=ExceptionResponse,
    summary="The exception queue -- repair lines with no attestation",
)
def get_exceptions(
    view: AttestationViewDep,
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    type_filter: str | None = Query(
        None, alias="type", description="MISSING_ATTESTATION"
    ),
    plant: str | None = Query(None, description="Plant code, e.g. 1300"),
    open_only: bool = Query(
        False,
        alias="openOnly",
        description=(
            "Only exceptions on repairs still out at a vendor -- the ones "
            "somebody can still act on"
        ),
    ),
    page: int = Query(1, ge=1),
    page_size: int | None = Query(None, alias="pageSize", ge=1),
) -> ExceptionResponse:
    """Repair lines that went out with no recorded condition assessment.

    Expect this to be large. Every historical repair line raises it, because the
    control did not exist before this platform -- that number is the business
    case for W5.3, not a bug in it.
    """
    items = view.exceptions

    if type_filter:
        wanted = type_filter.strip().upper()
        items = tuple(i for i in items if i.type == wanted)
    if plant:
        items = tuple(i for i in items if i.plant == plant)
    if open_only:
        items = tuple(i for i in items if i.is_open_repair)

    size = min(page_size or cfg.default_page_size, cfg.max_page_size)
    page_items, total = _paginate(items, page, size)

    return ExceptionResponse(
        items=[exception_item(i, cfg) for i in page_items],
        page=page,
        page_size=size,
        total=total,
        # Meta describes the whole check, not the filtered view: "how many of
        # the register is uncovered" is the number that matters and it must not
        # change because somebody filtered to one plant.
        meta=exception_meta(view.exception_stats),
        reference_date=snapshot.reference_date,
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
            "attestationWindowDays": cfg.attestation_window_days,
            "faultCategories": ", ".join(cfg.fault_category_list),
        },
    )
