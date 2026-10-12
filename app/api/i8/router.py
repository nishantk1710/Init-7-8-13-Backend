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
    coding_candidate_item,
    coding_candidate_meta,
    declaration_item,
    declaration_meta,
    exception_history_entry,
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
    CodingCandidateResponse,
    DeclarationResponse,
    ExceptionCommentRequest,
    ExceptionQueueItem,
    ExceptionResponse,
    ExceptionUpdateRequest,
    MaterialReference,
    RepairChain,
    RegisterResponse,
    RepairableUnitResponse,
    RepairDetail,
    RepairEvidenceItem,
    SAPDocumentReference,
    SnapshotInfo,
    UniverseDetail,
    UniverseResponse,
    VendorResponse,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i8.aging import bucket_labels
from app.initiatives.i8.attestation import (
    AttestationDraft,
    AttestationError,
    explain_coverage,
    find as find_attestations,
    record as record_attestation,
)
from app.initiatives.i8.coding_candidates import (
    CandidateSource,
    meets_confidence_threshold,
)
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.exception_state import (
    ExceptionStateError,
    add_comment,
    assign,
    auto_resolve,
    load_histories,
    load_states,
    set_status,
)
from app.initiatives.i8.exceptions import ExceptionItem
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.repairable_unit import assess as assess_repairable_unit
from app.initiatives.i8.service import (
    AttestationView,
    Snapshot,
    SnapshotBuilding,
    SnapshotFailed,
    get_attestation_view,
    get_coding_screen,
    get_snapshot,
    reset_attestation_view,
)
from app.initiatives.i8.universe import UniverseRow
from app.shared import get_criticality_source

router = APIRouter(prefix="/i8", tags=["i8 - refurbishable spares"])

T = TypeVar("T")

#: Seconds a client should wait before retrying while the snapshot builds.
SNAPSHOT_RETRY_AFTER_SECONDS = 10


def snapshot_dependency(db: Session = Depends(get_db)) -> Snapshot:
    """The assembled I08 read models, built once per process.

    503 with ``Retry-After`` while it is unavailable -- the I13 contract, which
    the frontend already renders as "still preparing".
    """
    try:
        return get_snapshot(db, wait_seconds=get_settings().i8_snapshot_wait_seconds)
    except SnapshotBuilding as building:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "building",
                "message": "I08 data is being prepared. Retry shortly.",
                "startedAt": building.started_at.isoformat() if building.started_at else None,
            },
            headers={"Retry-After": str(SNAPSHOT_RETRY_AFTER_SECONDS)},
        ) from None
    except SnapshotFailed as failed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "failed", "message": f"I08 data could not be prepared: {failed}"},
            headers={"Retry-After": str(SNAPSHOT_RETRY_AFTER_SECONDS * 6)},
        ) from None


SnapshotDep = Annotated[Snapshot, Depends(snapshot_dependency)]
SettingsDep = Annotated[I8Settings, Depends(get_i8_settings)]


def attestation_view_dependency(
    db: Session = Depends(get_db), snapshot: Snapshot = Depends(snapshot_dependency)
) -> AttestationView:
    """Coverage, the declaration queue and the exception queue.

    Cached separately from the snapshot: the snapshot turns over when its
    source is reloaded, but an attestation must be visible the moment the
    planner who recorded it reloads the queue.
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
    view: AttestationView | None = None,
) -> RepairChain:
    """A register row enriched with its material's stock position.

    Stock and reorder point live on the material+plant, not on the PO line, so
    they come from the universe rather than from a second query. The declaration
    and the justification come from W5.3's attestation view for the same reason
    -- and because those tables change while the process runs, which the July
    snapshot does not.
    """
    row = lookup.get((line.material_id, line.plant))
    return repair_chain(
        line,
        cfg,
        stock_on_hand=row.stock_on_hand if row else None,
        reorder_point=row.reorder_point if row else None,
        new_unit_lead_time_days=row.planned_delivery_days if row else None,
        criticality=row.criticality if row else None,
        declaration=view.declaration_by_line.get(line.key) if view else None,
        justification=view.justifications.get(line.key) if view else None,
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
            _chain(line, cfg, _stock_lookup(snapshot), view)
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
            _chain(line, cfg, lookup, view)
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
        line=_chain(line, cfg, _stock_lookup(snapshot), view),
        timeline=timeline(
            line, snapshot.reference_date, view.coverage.covered.get(line.key)
        ),
    )


# --- W5.2 Layer 4: vendor analytics ---------------------------------------


@router.get(
    "/vendors/turnaround",
    response_model=VendorResponse,
    summary="Vendor turnaround analytics over completed repairs",
)
def get_vendor_turnaround(snapshot: SnapshotDep) -> VendorResponse:
    stats = snapshot.register_stats
    # Computed rather than quoted: excluding deleted PO lines moved every one of
    # these, and a note that states last month's figures is worse than none.
    headerless = stats.total_lines - stats.lines_with_po_header
    return VendorResponse(
        items=[vendor_item(v) for v in snapshot.vendors],
        total=len(snapshot.vendors),
        reference_date=snapshot.reference_date,
        note=(
            "Averages are over COMPLETED repairs only -- including open ones "
            "would make the slowest vendor look fastest. Vendors come from the "
            f"PO header, so the {headerless:,} repair lines with no header are "
            "grouped under UNKNOWN rather than dropped. LFA1 resolves only "
            f"{stats.vendors_resolved_to_a_name} of the {stats.distinct_vendors} "
            "repair vendors to a name; the rest show their code."
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
    snapshot: SnapshotDep,
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
        stored = record_attestation(
            db,
            draft,
            attestor=_current_user(),
            cfg=cfg,
            known_materials={row.material_id for row in snapshot.universe},
        )
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

    # Say what the write actually achieved, rather than leaving the caller to
    # infer it from a queue that may not have moved. See the field docs on
    # Attestation.coversRepairLines -- this is the difference between an honest
    # "recorded, and here is why nothing changed" and a UI that looks broken.
    covers, note = explain_coverage(stored, snapshot.lines, cfg)

    return attestation_item(
        stored, cfg, covers_repair_lines=covers, coverage_note=note
    )


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
        description="Required, Completed or Flagged",
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
    db: DbDep,
    type_filter: str | None = Query(
        None, alias="type", description="MISSING_ATTESTATION"
    ),
    plant: str | None = Query(None, description="Plant code, e.g. 1300"),
    repair: str | None = Query(
        None,
        description=(
            "One repair line, as the register keys it -- '4500000677-20'. "
            "Everything raised against that line, whatever the check."
        ),
    ),
    status_filter: str | None = Query(
        None,
        alias="status",
        description="OPEN / IN_PROGRESS / RESOLVED / CLOSED",
    ),
    owner: str | None = Query(
        None,
        description=(
            "Items owned by this person. The literal 'unassigned' returns "
            "the ones nobody has taken."
        ),
    ),
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

    The items are recomputed on every read; their status, owner and history
    come from ``i8_exception_state`` and are merged on here. An item nobody has
    touched reports OPEN and unassigned, which is the truth about it rather
    than a placeholder.
    """
    items = view.exceptions

    # Before any filtering, and over every computed id: a state row still
    # OPEN whose item is no longer raised describes a finding that has gone
    # away -- somebody recorded the declaration, or the goods receipt landed.
    # Resolving it here means the person who owned it sees why, instead of
    # watching it vanish from their queue.
    auto_resolve(db, (i.id for i in items))

    if type_filter:
        wanted = type_filter.strip().upper()
        items = tuple(i for i in items if i.type == wanted)
    if plant:
        items = tuple(i for i in items if i.plant == plant)
    if repair:
        wanted_line = repair.strip()
        items = tuple(
            i
            for i in items
            if f"{i.purchasing_document}-{i.item}" == wanted_line
            or i.purchasing_document == wanted_line
        )
    if open_only:
        items = tuple(i for i in items if i.is_open_repair)

    # Loaded once for the whole filtered set, because status and owner filter
    # on it -- paging first would filter a page rather than the queue.
    states = load_states(db, [i.id for i in items])

    if status_filter:
        wanted_status = status_filter.strip().upper()
        items = tuple(
            i
            for i in items
            if (states[i.id].status if i.id in states else "OPEN") == wanted_status
        )
    if owner:
        wanted_owner = owner.strip()
        if wanted_owner.lower() == "unassigned":
            items = tuple(
                i for i in items if i.id not in states or not states[i.id].owner
            )
        else:
            items = tuple(
                i
                for i in items
                if i.id in states and states[i.id].owner == wanted_owner
            )

    size = min(page_size or cfg.default_page_size, cfg.max_page_size)
    page_items, total = _paginate(items, page, size)

    # Only the page's histories: the queue runs to thousands of items and a
    # history per row is the one part of this response that has no bound.
    histories = load_histories(db, [i.id for i in page_items])

    return ExceptionResponse(
        items=[
            exception_item(
                i,
                cfg,
                state=states.get(i.id),
                history=histories.get(i.id, ()),
                # Listed means raised: these all came from a check that just
                # ran. The flag matters for an item fetched by id elsewhere.
                still_detected=True,
            )
            for i in page_items
        ],
        page=page,
        page_size=size,
        total=total,
        # Meta describes the whole check, not the filtered view: "how many of
        # the register is uncovered" is the number that matters and it must not
        # change because somebody filtered to one plant.
        meta=exception_meta(view.exception_stats),
        reference_date=snapshot.reference_date,
    )


# --- W5.5: coding candidates -----------------------------------------------


def _parse_sources(raw: str | None) -> tuple[str, ...] | None:
    """A comma-separated ``sources`` query into the tuple the screen wants.

    None means "take the configured default", which is not the same as an
    empty list. An unrecognised name raises rather than being dropped: a typo
    in a query string is a caller's mistake and should be told to them, unlike
    a typo in a deployment setting, which must never stop the app from
    starting.
    """
    if raw is None:
        return None
    names = tuple(part.strip().upper() for part in raw.split(",") if part.strip())
    known = {s.value for s in CandidateSource}
    unknown = [n for n in names if n not in known]
    if unknown or not names:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown coding-candidate source(s): {', '.join(unknown) or '(none given)'}. "
                f"Valid: {', '.join(sorted(known))}."
            ),
        )
    # De-duplicated, order preserved: PO,PO,PR is one PO read, not two.
    seen: list[str] = []
    for name in names:
        if name not in seen:
            seen.append(name)
    return tuple(seen)


@router.get(
    "/coding-candidates",
    response_model=CodingCandidateResponse,
    summary="Materials whose PO text says repair but whose number does not",
)
def get_coding_candidates(
    db: DbDep,
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    screen: bool = Query(
        False,
        description=(
            "Run the LANGUAGE JUDGEMENT as well. Off by default because it is "
            "one model call per material -- 41 materials took 246 seconds "
            "against live gpt-4o. With it off every verdict is UNSCREENED and "
            "the keyword screen, the material split and the twin check have "
            "still run"
        ),
    ),
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Screen at most this many materials. Use it to demonstrate the "
            "model pass in seconds rather than minutes; the response reports "
            "wasTruncated so a partial run cannot read as a complete one"
        ),
    ),
    verdict: str | None = Query(
        None, description="MISCODED_REPAIRABLE, CONSUMABLE_FOR_REPAIR, ..."
    ),
    actionable_only: bool = Query(
        False,
        alias="actionableOnly",
        description="Only MISCODED_REPAIRABLE and UNCLEAR -- the rows worth reading",
    ),
    sources: str | None = Query(
        None,
        description=(
            "Which free-text sources to read, comma separated: PO (EKPO.TXZ01), "
            "PR (EBAN.TXZ01), MOVEMENT (MSEG.SGTXT). Defaults to "
            "I8_CODING_CANDIDATE_SOURCES. MOVEMENT is off by default because "
            "nobody has measured whether it finds anything PO and PR text do "
            "not -- a 541 is already a part on its way to repair, so its "
            "comments sit mostly on materials that ARE 80-series. Pass "
            "sources=PO,PR,MOVEMENT and compare meta.linesBySource to settle it"
        ),
    ),
    corroborated_only: bool = Query(
        False,
        alias="corroboratedOnly",
        description=(
            "Only candidates with an 80-series TWIN carrying identical text. "
            "These are provable from the data rather than argued from language"
        ),
    ),
    meets_confidence_threshold_only: bool = Query(
        False,
        alias="meetsConfidenceThresholdOnly",
        description=(
            "Only candidates whose model confidence clears "
            "I8_CODING_CANDIDATE_CONFIDENCE_THRESHOLD. Nothing is ever hidden "
            "by default -- this only narrows the list when asked"
        ),
    ),
) -> CodingCandidateResponse:
    """A material is repairable only if somebody typed an 80-series number.

    Nothing in SAP marks it, so when it is missed the part becomes invisible to
    everything I08 builds -- and when it wears out, somebody buys a new one.
    This finds those.

    **Read `meta` before the items.** It says how many lines were screened, how
    many were dropped for having no material number at all, whether a limit
    truncated the run, and -- crucially -- WHICH PROVIDER answered. A `provider`
    of `stub` means no model judged anything and every verdict is UNSCREENED.
    """
    wanted = _parse_sources(sources)
    result = get_coding_screen(
        db, snapshot, cfg, use_model=screen, limit=limit, sources=wanted
    )
    items = result.candidates

    if verdict:
        wanted = verdict.strip().upper()
        items = tuple(c for c in items if c.verdict == wanted)
    if actionable_only:
        items = tuple(c for c in items if c.is_actionable)
    if corroborated_only:
        items = tuple(c for c in items if c.is_corroborated)
    if meets_confidence_threshold_only:
        items = tuple(
            c
            for c in items
            if meets_confidence_threshold(
                c.confidence, cfg.coding_candidate_confidence_threshold
            )
        )

    return CodingCandidateResponse(
        items=[coding_candidate_item(c, cfg) for c in items],
        total=len(items),
        # Meta describes the whole screen, never the filtered view: "how much of
        # the free text was searched" must not change because somebody asked for
        # one verdict.
        meta=coding_candidate_meta(result.stats),
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
        source_loaded_at=snapshot.source_loaded_at,
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
            "attestationCutoverDate": cfg.attestation_cutover_date or "(not set)",
            # Where the lead-time benchmark comes from and what it is measured
            # between. Echoed as prose because the answer is a ruling, not a
            # number: it is the same field and the same span Initiative 07 uses,
            # and the two must not drift apart unnoticed.
            "leadTimeSource": "MARC.PLIFZ (calendar days, PO to received)",
            "leadTimeGraceDays": 0,
            "faultCategories": ", ".join(cfg.fault_category_list),
            "repairLanguage": ", ".join(cfg.repair_language_list),
            # The current band labels, not the raw day boundaries -- the
            # frontend needs names to render, not the arithmetic that produced
            # them. See app.initiatives.i8.aging.bucket_labels.
            "agingBands": ", ".join(bucket_labels(cfg.aging_band_boundaries_list)),
            "codingCandidateConfidenceThreshold": cfg.coding_candidate_confidence_threshold,
        },
    )


# --- W7.2 / FR-6: the repairable-unit rule, on its own ---------------------
#
# Exposed separately from the assistant so the rule is demoable and testable
# without a conversation, and so the frontend can ask the question directly on
# a register screen. A GET -- it decides nothing and records nothing.


@router.get(
    "/repairable-unit",
    response_model=RepairableUnitResponse,
    summary="FR-6: does a repairable unit already exist for this material?",
)
def get_repairable_unit(
    snapshot: SnapshotDep,
    material: Annotated[str, Query(description="Material number, padded or stripped")],
    plant: Annotated[str | None, Query(description="Plant code. Omitted means every plant.")] = None,
) -> RepairableUnitResponse:
    """Whether a repairable unit is on the shelf or coming back from repair.

    ``today`` is the snapshot's reference date rather than the wall clock, so
    the overdue counts here agree with the register screen beside it. That is
    the right trade on a frozen July extract; when the source becomes live it
    is ``get_snapshot``'s caching that has to change, not this route.
    """
    verdict = assess_repairable_unit(
        material_id=material,
        plant=plant,
        universe_rows=snapshot.universe,
        repair_lines=snapshot.lines,
        today=snapshot.reference_date,
    )
    return RepairableUnitResponse(
        material_id=verdict.material_id,
        plant=verdict.plant,
        is_repairable_material=verdict.is_repairable_material,
        exists=verdict.exists,
        sources=[source.value for source in verdict.sources],
        stock_on_hand=verdict.stock_on_hand,
        stock_is_unknown=verdict.stock_is_unknown,
        stock_locations=verdict.stock_locations,
        open_repair_lines=verdict.open_repair_lines,
        quantity_under_repair=verdict.quantity_under_repair,
        soonest_due_date=verdict.soonest_due_date,
        overdue_lines=verdict.overdue_lines,
        headline=verdict.headline,
        caveats=list(verdict.caveats),
        evidence=[
            RepairEvidenceItem(
                purchasing_document=item.purchasing_document,
                item=item.item,
                quantity=item.quantity,
                raised_at=item.raised_at,
                due_date=item.due_date,
                days_overdue=item.days_overdue,
                vendor=item.vendor,
                vendor_name=item.vendor_name,
                status=item.status,
                dispatched=item.dispatched,
            )
            for item in verdict.evidence
        ],
        reference_date=snapshot.reference_date,
    )


@router.patch(
    "/exceptions/{exception_id}",
    response_model=ExceptionQueueItem,
    summary="Move an exception, or change who owns it",
)
def patch_exception(
    exception_id: str,
    body: ExceptionUpdateRequest,
    view: AttestationViewDep,
    cfg: SettingsDep,
    db: DbDep,
) -> ExceptionQueueItem:
    """**Writes the state, never the finding.**

    The exception itself is computed from SAP data and this cannot change it.
    What it changes is who owns the finding and what state the work is in,
    which is the half FR-11 asks for and no check can derive.

    An unknown id is still accepted. The checks are recomputed on every read,
    so an id can be real and currently unraised -- the gap it describes was
    closed since the caller loaded the screen. Rejecting it would lose the
    comment somebody just typed about exactly that.
    """
    found = next((i for i in view.exceptions if i.id == exception_id), None)

    try:
        if "owner" in body.model_fields_set:
            assign(db, exception_id, body.owner, actor=_current_user())
        if body.status is not None:
            set_status(
                db, exception_id, body.status, actor=_current_user(), note=body.note
            )
    except ExceptionStateError as exc:
        # 422, not 400: well-formed JSON that broke a rule of the queue.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if body.status is None and "owner" not in body.model_fields_set:
        raise HTTPException(
            status_code=422,
            detail="send a status, an owner, or both -- this request changes nothing",
        )

    return _exception_response(db, cfg, exception_id, found)


@router.post(
    "/exceptions/{exception_id}/comments",
    response_model=ExceptionQueueItem,
    status_code=status.HTTP_201_CREATED,
    summary="Say something about an exception",
)
def post_exception_comment(
    exception_id: str,
    body: ExceptionCommentRequest,
    view: AttestationViewDep,
    cfg: SettingsDep,
    db: DbDep,
) -> ExceptionQueueItem:
    """Add a comment without moving the item.

    Chasing a vendor, recording what somebody said on the phone, noting why
    this one is being left alone -- none of which is a status change, and all
    of which is the reason anybody can follow the item later.
    """
    found = next((i for i in view.exceptions if i.id == exception_id), None)
    try:
        add_comment(db, exception_id, body.text, actor=_current_user())
    except ExceptionStateError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _exception_response(db, cfg, exception_id, found)


def _exception_response(
    db: DbDep,
    cfg: I8Settings,
    exception_id: str,
    found: ExceptionItem | None,
) -> ExceptionQueueItem:
    """The item as it now stands, for a write to return.

    ``found`` is None when the checks are no longer raising this id. The row
    still answers -- with ``stillDetected`` false, and a placeholder body that
    says the finding is gone rather than inventing a repair line for it. That
    is the honest answer to "I just commented on something that resolved
    underneath me".
    """
    states = load_states(db, [exception_id])
    histories = load_histories(db, [exception_id])

    if found is not None:
        return exception_item(
            found,
            cfg,
            state=states.get(exception_id),
            history=histories.get(exception_id, ()),
            still_detected=True,
        )

    state = states.get(exception_id)
    return ExceptionQueueItem(
        id=exception_id,
        type="UNKNOWN",
        severity="info",
        material=MaterialReference(material_id="", material_code="", description=None),
        plant=None,
        repair_line=SAPDocumentReference(type="PO", document_number="", line=None),
        title="No longer raised",
        detail=(
            "No check is currently raising this item. The data it was found on "
            "has changed since it was listed."
        ),
        raised_at=None,
        is_open_repair=False,
        pre_automation=False,
        status=state.status if state else "OPEN",
        owner=state.owner if state else None,
        still_detected=False,
        history=[exception_history_entry(e) for e in histories.get(exception_id, ())],
    )
