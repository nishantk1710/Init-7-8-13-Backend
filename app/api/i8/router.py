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

from collections import Counter
from collections.abc import Sequence
from typing import Annotated, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.i8.mappers import (
    attestation_item,
    coding_candidate_item,
    coding_candidate_meta,
    declaration_facets,
    declaration_item,
    declaration_meta,
    department_name,
    exception_facets,
    exception_history_entry,
    exception_item,
    exception_meta,
    material_descriptions,
    register_facets,
    register_meta,
    repair_chain,
    request_chain,
    request_timeline,
    timeline,
    universe_item,
    universe_meta,
)
from app.api.i8.schemas import (
    Attestation,
    AttestationRequest,
    AttestationResponse,
    CodingCandidateResponse,
    DeclarationResponse,
    ExceptionEventRequest,
    ExceptionQueueItem,
    ExceptionResponse,
    MaterialReference,
    RepairChain,
    RegisterResponse,
    RegisterViewCounts,
    RepairableUnitResponse,
    RepairDetail,
    RepairEvidenceItem,
    RepairRequestEvidenceItem,
    SAPDocumentReference,
    SnapshotInfo,
    UniverseDetail,
    UniverseResponse,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i8.acquisitions import PURCHASE_ORDER
from app.initiatives.i8.aging import aging_bucket as band_of
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
    UNTOUCHED,
    ExceptionStateError,
    ExceptionWorkState,
    add_comment,
    assign,
    auto_resolve,
    canonical_status,
    load_states,
    set_status,
    state_of,
    validate_kind,
)
from app.initiatives.i8.exceptions import ExceptionItem
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.initiatives.i8.register import RepairLine
from app.initiatives.i8.repairable_unit import assess as assess_repairable_unit
from app.initiatives.i8.requisitions import NOT_ORDERED, PR_RAISED, RepairRequest
from app.initiatives.i8.service import (
    AttestationView,
    Snapshot,
    SnapshotBuilding,
    SnapshotFailed,
    cached_snapshot,
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


def _matches(needle: str, *fields: str | None) -> bool:
    """Whether any of ``fields`` contains ``needle``, ignoring case.

    ``needle`` arrives trimmed and lower-cased. This is a queue screen's search
    box moved to the server, so it searches every row rather than the page the
    browser holds.
    """
    return any(needle in field.lower() for field in fields if field)


def _type_set(raw: str | None) -> frozenset[str]:
    """``A,B`` -> {"A", "B"}, upper-cased; blanks are dropped."""
    return frozenset(
        part.strip().upper() for part in (raw or "").split(",") if part.strip()
    )


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
    exceptions: Sequence[ExceptionItem] | None = None,
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
        exceptions=exceptions,
    )


def _request_chain(
    request: RepairRequest,
    cfg: I8Settings,
    lookup: dict[tuple[str, str | None], UniverseRow],
    exceptions: Sequence[ExceptionItem] | None = None,
    view: AttestationView | None = None,
) -> RepairChain:
    """A repair PR's register row, with its material's stock position from the
    universe -- the same lookups a PO row gets in :func:`_chain` -- and its
    declaration from the attestation view."""
    row = lookup.get((request.material_id, request.plant))
    return request_chain(
        request,
        cfg,
        stock_on_hand=row.stock_on_hand if row else None,
        reorder_point=row.reorder_point if row else None,
        new_unit_lead_time_days=row.planned_delivery_days if row else None,
        criticality=row.criticality if row else None,
        exceptions=exceptions,
        declaration=_request_declaration(request, view) if view else None,
    )


def _criticality_of(
    line: RepairLine | RepairRequest, lookup: dict[tuple[str, str | None], UniverseRow]
) -> str | None:
    """The rating a register row shows: its material's, at its plant."""
    row = lookup.get((line.material_id, line.plant))
    return row.criticality if row else None


#: How a PR row's document is named on the detail route and in a declaration:
#: ``PR-{BANFN}``, the register row id ``PR-{BANFN}-{BNFPO}`` split at its last
#: hyphen.
_REQUEST_PREFIX = "PR-"


def _item_key(item: str) -> str:
    """A PR item as the snapshot keys it: leading zeros stripped ('10')."""
    return item.strip().lstrip("0") or item.strip()


def _declaration_status_of(line: RepairLine, view: AttestationView) -> str:
    """What a register row's Declaration Status cell says: the queue row's
    status, or "Required" for a line the queue has no row for -- the default
    ``repair_chain`` applies."""
    row = view.declaration_by_line.get(line.key)
    return row.status if row is not None else "Required"


def _request_declaration(request: RepairRequest, view: AttestationView):
    """A repair PR's declaration-queue row. Its ``related_repair_id`` is the
    PR's row id ``PR-{BANFN}-{BNFPO}``, so the view's index holds it under
    (``PR-{BANFN}``, BNFPO) -- apart from every PO line's key."""
    return view.declaration_by_line.get(tuple(request.row_id.rsplit("-", 1)))


def _request_declaration_status(request: RepairRequest, view: AttestationView) -> str:
    """The same for a repair PR row (27-Oct-2026: PRs can be declared on)."""
    row = _request_declaration(request, view)
    return row.status if row is not None else "Required"


def _raised_by_line(
    view: AttestationView, excluded_types: frozenset[str]
) -> dict[tuple[str, str], list[ExceptionItem]]:
    """Each repair line's exception items, less the excluded types.

    One pass over the queue per request, so the register can fill its
    Exceptions column -- and filter on it -- for any page without the screen
    loading the whole queue to join it in the browser.

    PO lines only. A bought-new finding whose repair is a PR with no PO yet
    names the PR number; keyed in with the PO lines, it could land on a PO line
    that shares the number, and a PR row shows no exceptions in phase 1.
    """
    by_line: dict[tuple[str, str], list[ExceptionItem]] = {}
    for item in view.exceptions:
        if item.type not in excluded_types and item.repair_document_type == PURCHASE_ORDER:
            by_line.setdefault((item.purchasing_document, item.item), []).append(item)
    return by_line


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
    summary="Open repair lines, at material + repair-PO-line grain",
)
def get_register(
    snapshot: SnapshotDep,
    cfg: SettingsDep,
    view: AttestationViewDep,
    plant: str | None = Query(None),
    vendor: str | None = Query(
        None, description="Vendor code. The literal `none` returns the lines with no vendor."
    ),
    material: str | None = Query(None),
    repair_status: str | None = Query(None, alias="status"),
    overdue_only: bool = Query(False, alias="overdueOnly"),
    overdue_status: str | None = Query(
        None,
        alias="overdueStatus",
        description=(
            "OVERDUE, ON_TIME or NO_DUE_DATE; NOT_ORDERED for the repair PRs "
            "with no PO yet, when they are served"
        ),
    ),
    criticality: str | None = Query(
        None,
        description=(
            "The rating the line shows -- its material's, at its plant. The "
            "literal `none` returns the unrated lines."
        ),
    ),
    declaration_status: str | None = Query(
        None, alias="declarationStatus", description="Required, Completed, Flagged or Historical"
    ),
    aging_bucket: str | None = Query(None, alias="agingBucket"),
    stage_aging_bucket: str | None = Query(
        None,
        alias="stageAgingBucket",
        description=(
            "The aging band of the days a line has spent in its current stage, "
            "where agingBucket bands the days it has been open"
        ),
    ),
    with_exceptions: bool = Query(
        False,
        alias="withExceptions",
        description="Only lines a check has raised something against",
    ),
    quick_view: str | None = Query(
        None,
        alias="view",
        description=(
            "A quick view on top of the filters: overdue, undeclared, atVendor, "
            "withExceptions or notOrdered (repair PRs with no PO yet). "
            "viewCounts counts each one before it is applied."
        ),
    ),
    exclude_exception_type: str | None = Query(
        None,
        alias="excludeExceptionType",
        description=(
            "Comma-separated exception types the caller does not carry: left "
            "out of each line's `exceptions` and of `withExceptions`"
        ),
    ),
    q: str | None = Query(
        None,
        max_length=200,
        description="Material number or description, repair line, or the PO or PR number",
    ),
    page: int = Query(1, ge=1),
    page_size: int = Query(None, alias="pageSize", ge=1),
) -> RegisterResponse:
    """Repair lines SAP still has open, one page at a time, each with what the
    checks raise against it.

    Open is SAP's "Delivery completed" flag, since 19-Oct-2026: a line SAP has
    closed, deleted or blocked is not listed, and ``meta`` counts each kind.

    ``meta`` describes the register whatever the filters. ``facets`` are taken
    before every filter, so a screen showing one page can still offer every
    value in its dropdowns.

    Since 27-Oct-2026, while ``I8_REPAIR_REQUESTS_ENABLED`` is on, the open
    repair PRs with no PO yet follow the PO rows (``lineKind`` ``PR``). The
    ``meta`` line counts stay PO-only; it counts the PRs separately, served or
    not.
    """
    page_size = min(page_size or cfg.default_page_size, cfg.max_page_size)
    lookup = _stock_lookup(snapshot)
    raised = _raised_by_line(view, _type_set(exclude_exception_type))

    lines = snapshot.lines
    # The open repair PRs with no PO yet, served after every PO row while the
    # switch is on. Never read while it is off, so the register is then exactly
    # what it was before they existed. Every filter below narrows them by what
    # their row shows: no vendor, PR Raised, NOT_ORDERED, Required, days from
    # the PR date, no exceptions.
    requests = snapshot.repair_requests if cfg.repair_requests_enabled else ()
    facets = register_facets(
        lines, cfg, lambda row: _criticality_of(row, lookup), requests=requests
    )

    if plant:
        lines = tuple(line for line in lines if line.plant == plant)
        requests = tuple(r for r in requests if r.plant == plant)
    if vendor:
        wanted_vendor = vendor.strip()
        if wanted_vendor.lower() == "none":
            lines = tuple(line for line in lines if not line.vendor)
        else:
            lines = tuple(line for line in lines if line.vendor == wanted_vendor)
            requests = ()
    if material:
        key = normalise(material)
        lines = tuple(line for line in lines if line.material_id == key)
        requests = tuple(r for r in requests if r.material_id == key)
    if repair_status:
        lines = tuple(line for line in lines if line.repair_status == repair_status)
        if repair_status != PR_RAISED:
            requests = ()
    if aging_bucket:
        lines = tuple(line for line in lines if line.aging_bucket == aging_bucket)
        requests = tuple(r for r in requests if r.aging_bucket == aging_bucket)
    if overdue_only:
        lines = tuple(line for line in lines if line.is_overdue)
        requests = ()
    if overdue_status:
        wanted_overdue = overdue_status.strip().upper()
        lines = tuple(line for line in lines if line.overdue_status == wanted_overdue)
        if wanted_overdue != NOT_ORDERED:
            requests = ()
    if criticality:
        # The rating the row shows, at the line's own plant. This used to
        # match any plant's rating for the material, so a filter could return
        # rows whose Criticality cell said something else.
        wanted_rating = criticality.strip().upper()
        if wanted_rating == "NONE":
            lines = tuple(line for line in lines if not _criticality_of(line, lookup))
            requests = tuple(r for r in requests if not _criticality_of(r, lookup))
        else:
            lines = tuple(
                line
                for line in lines
                if (_criticality_of(line, lookup) or "").upper() == wanted_rating
            )
            requests = tuple(
                r for r in requests if (_criticality_of(r, lookup) or "").upper() == wanted_rating
            )
    if declaration_status:
        wanted_declaration = declaration_status.strip().lower()
        lines = tuple(
            line
            for line in lines
            if _declaration_status_of(line, view).lower() == wanted_declaration
        )
        requests = tuple(
            r for r in requests if _request_declaration_status(r, view).lower() == wanted_declaration
        )
    if stage_aging_bucket:
        bands = cfg.aging_band_boundaries_list
        lines = tuple(
            line
            for line in lines
            if band_of(line.days_in_current_stage, bands) == stage_aging_bucket
        )
        # Nothing has moved since the PR was raised: its stage is as old as it.
        requests = tuple(r for r in requests if band_of(r.days_open, bands) == stage_aging_bucket)
    if with_exceptions:
        lines = tuple(line for line in lines if raised.get(line.key))
        requests = ()
    if q and q.strip():
        needle = q.strip().lower()
        lines = tuple(
            line
            for line in lines
            if _matches(
                needle,
                line.material_id,
                line.description,
                f"{line.purchasing_document}-{line.item}",
                f"{line.purchasing_document}/{line.item}",
                (f"{line.pr_number}/{line.pr_item}" if line.pr_item else line.pr_number),
            )
        )
        requests = tuple(
            r
            for r in requests
            if _matches(
                needle,
                r.material_id,
                r.description,
                r.row_id,
                f"{r.purchase_requisition}/{r.item}",
            )
        )

    # The quick views: what the screen's preset buttons select, counted over
    # the filtered lines before one of them is applied. A PR row is in its own
    # and, since 27-Oct-2026, "undeclared": nothing is ordered, so nothing is
    # overdue or at the vendor and no check reads it, but it can be declared on.
    # "Undeclared" is Required only -- Historical is not waiting, and Flagged
    # has its declaration. The PO tests below see PO lines only.
    view_tests = {
        "overdue": lambda line: line.overdue_status == "OVERDUE",
        "undeclared": lambda line: _declaration_status_of(line, view) == "Required",
        "atVendor": lambda line: line.repair_status == "At Vendor",
        "withExceptions": lambda line: bool(raised.get(line.key)),
        "notOrdered": lambda line: False,
    }
    view_counts = RegisterViewCounts(
        all=len(lines) + len(requests),
        overdue=sum(1 for line in lines if view_tests["overdue"](line)),
        undeclared=sum(1 for line in lines if view_tests["undeclared"](line))
        + sum(1 for r in requests if _request_declaration_status(r, view) == "Required"),
        at_vendor=sum(1 for line in lines if view_tests["atVendor"](line)),
        with_exceptions=sum(1 for line in lines if view_tests["withExceptions"](line)),
        not_ordered=len(requests),
    )
    if quick_view:
        test = view_tests.get(quick_view)
        if test is None:
            raise HTTPException(
                # A bare 422, as elsewhere here: Starlette renamed the constant.
                status_code=422,
                detail=f"Unknown view {quick_view!r}; use one of {', '.join(view_tests)}.",
            )
        lines = tuple(line for line in lines if test(line))
        if quick_view == "undeclared":
            requests = tuple(r for r in requests if _request_declaration_status(r, view) == "Required")
        elif quick_view != "notOrdered":
            requests = ()

    # PO rows first, then the PRs in the snapshot's order (PR date, number,
    # item), so a page boundary falls the same way on every request.
    rows: tuple[RepairLine | RepairRequest, ...] = (*lines, *requests)
    page_rows, total = _paginate(rows, page, page_size)
    return RegisterResponse(
        items=[
            _request_chain(row, cfg, lookup, exceptions=[], view=view)
            if isinstance(row, RepairRequest)
            else _chain(row, cfg, lookup, view, exceptions=raised.get(row.key, []))
            for row in page_rows
        ],
        page=page,
        page_size=page_size,
        total=total,
        meta=register_meta(snapshot.register_stats),
        facets=facets,
        reference_date=snapshot.reference_date,
        view_counts=view_counts,
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
    """One register row and its timeline.

    ``document`` ``PR-{BANFN}`` names an open repair PR with no PO yet -- the
    register row id split at its last hyphen. Served only while
    ``I8_REPAIR_REQUESTS_ENABLED`` is on; otherwise, or for a PR that is not
    open, 404 like any unknown line.
    """
    if document.upper().startswith(_REQUEST_PREFIX):
        request = (
            snapshot.request(document[len(_REQUEST_PREFIX) :].strip(), _item_key(item))
            if cfg.repair_requests_enabled
            else None
        )
        if request is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No open repair request {document}/{item}.",
            )
        return RepairDetail(
            line=_request_chain(request, cfg, _stock_lookup(snapshot), view=view),
            timeline=request_timeline(request, snapshot.reference_date),
        )

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


# No vendor turnaround route since 19-Oct-2026. It was dispatch to goods
# receipt over completed repairs; the register no longer lists closed lines or
# reads goods receipts, and the figure measured posting practice anyway -- stores
# post the 541 on the day of the receipt on about 90% of repairs.


# --- W5.3: attestation, declarations and exceptions ------------------------


NOT_OPEN_IN_REGISTER = "That repair line is not open in the register."


def _bound_line(
    body: AttestationRequest, snapshot: Snapshot, cfg: I8Settings
) -> tuple[str, str, str, str, str | None] | None:
    """The register line a declaration names, as (type, document, item,
    material, plant), or None for an unbound declaration.

    Raises the 422 when the named line is not open in the register. A PR is
    open in the register only while PR rows are served. A ``PR-`` document (the
    register's PR row id) names a repair PR whatever type is sent. Half a key
    is left to :func:`validate`, which says so in its own words.
    """
    document = (body.repair_document or "").strip()
    item = (body.repair_item or "").strip()
    if not document or not item:
        return None
    is_request = body.repair_document_type == "PR"
    if document.upper().startswith(_REQUEST_PREFIX):
        is_request = True
        document = document[len(_REQUEST_PREFIX) :].strip()
    if is_request:
        request = (
            snapshot.request(document, _item_key(item))
            if cfg.repair_requests_enabled
            else None
        )
        if request is None:
            raise HTTPException(status_code=422, detail=NOT_OPEN_IN_REGISTER)
        return ("PR", *request.key, request.material_id, request.plant)
    line = snapshot.line(document, item) or snapshot.line(document, _item_key(item))
    if line is None:
        raise HTTPException(status_code=422, detail=NOT_OPEN_IN_REGISTER)
    return ("PO", *line.key, line.material_id, line.plant)


def _check_binding(
    body: AttestationRequest,
    bound: tuple[str, str, str, str, str | None],
    db: Session,
) -> None:
    """The declaration must be for the bound line's part and plant, and a line
    with a current declaration takes an amendment of it, not a second one --
    two current answers for one line is the ambiguity the binding removes."""
    kind, document, item, material, plant = bound
    own_material = normalise(body.material_id) or body.material_id.strip()
    own_plant = (body.plant or "").strip()
    if own_material != material or (plant is not None and own_plant != plant):
        raise HTTPException(
            status_code=422,
            detail=(
                f"This declaration is for {own_material} at {own_plant}, but that "
                f"repair line is for {material} at {plant or 'no plant'}."
            ),
        )
    current = [
        row
        for row in find_attestations(
            db, material_id=material, plant=own_plant, include_superseded=False
        )
        if row.repair_document == document
        and row.repair_item == item
        and (row.repair_document_type or "PO") == kind
    ]
    if current and (body.supersedes or "").strip() not in {row.id for row in current}:
        raise HTTPException(
            status_code=422,
            detail=f"This line already has declaration {current[0].id}. Amend it instead.",
        )


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

    A declaration may name a repair PO line or (``repairDocumentType`` 'PR',
    27-Oct-2026) an open repair PR with no PO yet. The named line must be open
    in the register and be for the same part and plant, and a line that
    already has a current declaration takes an amendment of it -- each a 422
    before anything is written. The stored key is the register's own.
    """
    bound = _bound_line(body, snapshot, cfg)
    if bound is not None:
        _check_binding(body, bound, db)
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
        repair_document=bound[1] if bound else body.repair_document,
        repair_item=bound[2] if bound else body.repair_item,
        repair_document_type=bound[0] if bound else body.repair_document_type,
        attestor_name=body.attestor_name,
        amendment_reason=body.amendment_reason,
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
    # The snapshot already built, never one built for this: the declaration
    # form reads this endpoint for its fault categories, and should not sit
    # through a cold start for a label. Without one, the number shows alone.
    descriptions = material_descriptions(cached_snapshot(), (r.material_id for r in rows))

    return AttestationResponse(
        items=[
            attestation_item(
                r,
                cfg,
                superseded_by=superseded_by.get(r.id),
                description=descriptions.get(r.material_id),
            )
            for r in rows
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
    vendor: str | None = Query(None, description="Vendor code, as `facets.vendors` lists it"),
    repair_status: str | None = Query(
        None,
        alias="repairStatus",
        description="Where the repair stands -- PO Issued or At Vendor",
    ),
    q: str | None = Query(
        None,
        max_length=200,
        description="Material number or description, repair line or requisition",
    ),
    status_filter: str | None = Query(
        None,
        alias="status",
        description="Required, Completed, Flagged or Historical",
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

    ``facets`` are not: they are taken after ``status`` and ``outstandingOnly``,
    which choose what kind of row is listed, and before the filters a screen's
    dropdowns and search box set. A screen showing one page of thousands
    cannot build its dropdowns from the rows it holds.
    """
    rows = view.declarations

    if status_filter:
        wanted = status_filter.strip().lower()
        rows = tuple(r for r in rows if r.status.lower() == wanted)
    if outstanding_only:
        rows = tuple(r for r in rows if r.is_outstanding)

    facets = declaration_facets(rows, cfg)

    if plant:
        rows = tuple(r for r in rows if r.plant == plant)
    if vendor:
        wanted_vendor = vendor.strip()
        rows = tuple(r for r in rows if r.vendor == wanted_vendor)
    if repair_status:
        wanted_repair = repair_status.strip().lower()
        rows = tuple(r for r in rows if (r.repair_status or "").lower() == wanted_repair)
    if q and q.strip():
        needle = q.strip().lower()
        rows = tuple(
            r
            for r in rows
            if _matches(needle, r.material_id, r.description, r.related_repair_id, r.pr_number)
        )

    size = min(page_size or cfg.default_page_size, cfg.max_page_size)
    items, total = _paginate(rows, page, size)

    return DeclarationResponse(
        items=[declaration_item(r, cfg) for r in items],
        page=page,
        page_size=size,
        total=total,
        meta=declaration_meta(rows, view.coverage.window_days),
        facets=facets,
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
            "One repair line, as the register keys it -- '4500000677-20', or "
            "'PR-2000029113-10' for a repair PR with no PO yet. Everything "
            "raised against that line, whatever the check."
        ),
    ),
    status_filter: str | None = Query(
        None,
        alias="status",
        description=(
            "OPEN / CLOSED. The retired IN_PROGRESS and RESOLVED still answer, "
            "as OPEN and CLOSED, so an old link does not come back empty."
        ),
    ),
    owner: str | None = Query(
        None,
        description=(
            "Items owned by this person. The literal 'unassigned' returns "
            "the ones nobody has taken."
        ),
    ),
    actionable_only: bool = Query(
        False,
        alias="actionableOnly",
        description="Leave out items raised before the control existed (preAutomation)",
    ),
    exclude_type: str | None = Query(
        None,
        alias="excludeType",
        description=(
            "Comma-separated types this queue does not carry. Left out of the "
            "items, the facets and the meta counts alike."
        ),
    ),
    q: str | None = Query(
        None,
        max_length=200,
        description="Exception id, material number or description, or repair line",
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

    ``excludeType`` is not a filter: it takes a check out of the queue, so its
    items leave the meta counts as well as the list. ``facets`` are taken after
    it and before every filter, so a screen showing one page of thousands can
    still offer every plant and type in its dropdowns.
    """
    items = view.exceptions

    # Before any filtering, and over every computed id: a state row still
    # OPEN whose item is no longer raised describes a finding that has gone
    # away -- somebody recorded the declaration, or SAP closed the repair line.
    # Resolving it here means the person who owned it sees why, instead of
    # watching it vanish from their queue.
    #
    # Before excludeType too. An item this caller's queue does not carry is
    # still raised, and resolving it because the caller looked away would
    # close somebody else's work.
    auto_resolve(db, (i.id for i in items))

    excluded_types = _type_set(exclude_type)
    excluded = tuple(i for i in items if i.type in excluded_types)
    if excluded:
        items = tuple(i for i in items if i.type not in excluded_types)

    facets = exception_facets(items, cfg)

    # The type filter comes last, after the counts below; the rest narrow in
    # any order.
    if plant:
        items = tuple(i for i in items if i.plant == plant)
    if repair:
        # The repair as the register names its row: '{EBELN}-{EBELP}', or
        # 'PR-{BANFN}-{BNFPO}' for a repair PR with no PO yet (a bought-new
        # finding can name one). A bare document -- 'PR-{BANFN}' for a PR --
        # matches every line of it.
        wanted_line = repair.strip()
        items = tuple(
            i
            for i in items
            if i.repair_reference == wanted_line
            or i.repair_reference.rsplit("-", 1)[0] == wanted_line
        )
    if actionable_only:
        items = tuple(i for i in items if not i.pre_automation)
    if q and q.strip():
        needle = q.strip().lower()
        items = tuple(
            i
            for i in items
            if _matches(
                needle,
                i.id,
                i.material_id,
                i.description,
                i.repair_reference,
            )
        )

    # Folded once for the whole filtered set, because status and owner filter
    # on it -- paging first would filter a page rather than the queue. Only
    # items somebody has touched have a history, so this stays small while the
    # queue runs to thousands.
    states = load_states(db)

    if status_filter:
        wanted_status = canonical_status(status_filter)
        items = tuple(
            i for i in items if states.get(i.id, UNTOUCHED).status == wanted_status
        )
    if owner:
        wanted_owner = owner.strip()
        if wanted_owner.lower() == "unassigned":
            items = tuple(i for i in items if not states.get(i.id, UNTOUCHED).owner)
        else:
            items = tuple(
                i for i in items if states.get(i.id, UNTOUCHED).owner == wanted_owner
            )

    # What each type tab holds: every filter applied except the type itself,
    # so the open tab's count is the number of rows under it.
    type_counts = Counter(i.type for i in items)
    if type_filter:
        wanted = type_filter.strip().upper()
        items = tuple(i for i in items if i.type == wanted)

    size = min(page_size or cfg.default_page_size, cfg.max_page_size)
    page_items, total = _paginate(items, page, size)

    return ExceptionResponse(
        items=[
            exception_item(
                i,
                cfg,
                state=states.get(i.id),
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
        # change because somebody filtered to one plant. Only a check taken out
        # of the queue altogether comes off it.
        meta=exception_meta(view.exception_stats, excluded_types, excluded),
        facets=facets,
        reference_date=snapshot.reference_date,
        type_counts=dict(type_counts),
    )


@router.get(
    "/exceptions/{exception_id}",
    response_model=ExceptionQueueItem,
    summary="One exception, with its status, owner and history",
)
def get_exception(
    exception_id: str,
    view: AttestationViewDep,
    cfg: SettingsDep,
    db: DbDep,
) -> ExceptionQueueItem:
    """The item a link names, wherever it sits in the queue.

    A paged screen holds one page, so it cannot open a linked item by finding
    it among its rows: the item may be on any page, or filtered off them all.

    An item the checks no longer raise still answers while it has a history,
    with ``stillDetected`` false -- somebody followed a link to work they
    remember, and the answer is that the data moved. An id with neither is
    404: nothing raised it and nobody touched it.
    """
    found = next((i for i in view.exceptions if i.id == exception_id), None)
    work = state_of(db, exception_id)
    if found is None and not work.history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No exception {exception_id!r} is raised or has a history.",
        )
    return _exception_response(cfg, exception_id, found, work)


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
            "against live gpt-4o. With it off no model is called, but verdicts "
            "already on record are still served: a material comes back "
            "UNSCREENED only when nothing has ever judged its current text"
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
            # Left out of the register since 21-Oct-2026: an RFQ orders nothing.
            "rfqDocType": cfg.rfq_doc_type,
            # What "open" means since 19-Oct-2026 -- SAP's rule, not ours.
            "openLineRule": (
                f"EKPO.ELIKZ not '{cfg.delivery_completed_indicator}' (delivery "
                f"completed), EKPO.LOEKZ not '{cfg.po_deleted_indicator}' "
                f"(deleted) or '{cfg.po_blocked_indicator}' (blocked)"
            ),
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
            # Since 27-Oct-2026: whether the open repair PRs with no PO yet are
            # served, and the rule that finds them -- always built, so the
            # meta counts them either way.
            "repairRequestsEnabled": cfg.repair_requests_enabled,
            "openRepairRequestRule": (
                f"a repair-order line (RESB.BWART '{cfg.repair_reservation_movement_type}') "
                f"names the PR; the PR item is not deleted (EBAN.LOEKZ not "
                f"'{cfg.requisition_deleted_indicator}'); no live PO points to it; and "
                f"one such line is neither deleted (RESB.XLOEK not "
                f"'{cfg.reservation_deleted_indicator}') nor final-issued (RESB.KZEAR "
                f"not '{cfg.reservation_final_issue_indicator}')"
            ),
            # Since 27-Oct-2026: where usable vs damaged stock is read from,
            # and which conditions count as which. The stock on hand stays
            # the MARD total either way.
            "stockConditionSource": cfg.stock_condition_source,
            "usableStockConditions": ", ".join(cfg.usable_stock_condition_list),
            "damagedStockConditions": ", ".join(cfg.damaged_stock_condition_list),
            # Since 27-Oct-2026: whether the assistant gives the need-date
            # line (off until SAP confirms how MRP treats an open repair PO).
            "needDateAdviceEnabled": cfg.need_date_advice_enabled,
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
    cfg: SettingsDep,
    material: Annotated[str, Query(description="Material number, padded or stripped")],
    plant: Annotated[str | None, Query(description="Plant code. Omitted means every plant.")] = None,
) -> RepairableUnitResponse:
    """Whether a repairable unit is on the shelf or coming back from repair.

    ``today`` is the snapshot's reference date rather than the wall clock, so
    the overdue counts here agree with the register screen beside it. That is
    the right trade on a frozen July extract; when the source becomes live it
    is ``get_snapshot``'s caching that has to change, not this route.

    Open repair PRs count only while ``I8_REPAIR_REQUESTS_ENABLED`` is on, as
    in the assistant (since 27-Oct-2026), so the two cannot disagree.
    """
    verdict = assess_repairable_unit(
        material_id=material,
        plant=plant,
        universe_rows=snapshot.universe,
        repair_lines=snapshot.lines,
        today=snapshot.reference_date,
        repair_requests=snapshot.repair_requests if cfg.repair_requests_enabled else None,
        department_name=department_name,
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
        open_repair_request_lines=verdict.open_repair_request_lines,
        request_evidence=[
            RepairRequestEvidenceItem(
                purchase_requisition=item.purchase_requisition,
                item=item.item,
                quantity=item.quantity,
                requested_at=item.requested_on,
                days_since_requested=item.days_since_requested,
                department=item.department,
                department_name=item.department_name,
                repair_order=item.repair_order,
            )
            for item in verdict.request_evidence
        ],
        usable_stock=verdict.usable_stock,
        damaged_stock=verdict.damaged_stock,
        stock_condition_known=verdict.stock_condition_known,
        stock_source=verdict.stock_source,
    )


@router.post(
    "/exceptions/{exception_id}/events",
    response_model=ExceptionQueueItem,
    status_code=status.HTTP_201_CREATED,
    summary="Append one entry to an exception's history",
)
def post_exception_event(
    exception_id: str,
    body: ExceptionEventRequest,
    view: AttestationViewDep,
    cfg: SettingsDep,
    db: DbDep,
) -> ExceptionQueueItem:
    """**Writes the history, never the finding.**

    The exception itself is computed from SAP data and this cannot change it.
    What it records is who is working the finding and what they said -- the
    half FR-11 asks for and no check can derive.

    One POST for assigning, commenting and moving, because all three are the
    same operation on the same append-only table. There is deliberately no
    PATCH: the item's status is not a column somebody edits, it is the last
    entry that set one, and ``tests/test_write_paths.py`` holds the whole
    application to that.

    An unknown id is still accepted. The checks are recomputed on every read,
    so an id can be real and currently unraised -- the gap it describes was
    closed since the caller loaded the screen. Rejecting it would throw away
    the comment somebody just typed about exactly that.
    """
    found = next((i for i in view.exceptions if i.id == exception_id), None)

    try:
        kind = validate_kind(body.kind)
        if kind == "ASSIGNED":
            assign(db, exception_id, body.owner, actor=_current_user())
        elif kind == "STATUS":
            if body.status is None:
                raise ExceptionStateError("a STATUS entry needs a status")
            set_status(
                db, exception_id, body.status, actor=_current_user(), note=body.text
            )
        else:
            add_comment(db, exception_id, body.text, actor=_current_user())
    except ExceptionStateError as exc:
        # 422, not 400: well-formed JSON that broke a rule of the queue.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _exception_response(cfg, exception_id, found, state_of(db, exception_id))


def _exception_response(
    cfg: I8Settings,
    exception_id: str,
    found: ExceptionItem | None,
    work: ExceptionWorkState,
) -> ExceptionQueueItem:
    """The item as it now stands, for a write or a lookup by id to return.

    ``found`` is None when the checks are no longer raising this id. The row
    still answers -- with ``stillDetected`` false, and a placeholder body that
    says the finding is gone rather than inventing a repair line for it. That
    is the honest answer to "I just commented on something that resolved
    underneath me".
    """
    if found is not None:
        return exception_item(found, cfg, state=work, still_detected=True)

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
        pre_automation=False,
        status=work.status,
        owner=work.owner,
        still_detected=False,
        history=[exception_history_entry(e) for e in work.history],
    )
