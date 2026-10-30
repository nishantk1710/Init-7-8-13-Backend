"""Domain objects -> API models.

Kept out of the routes so a route stays three lines -- parse, call, shape -- and
out of ``app/initiatives/i8`` so the business layer never has to know what the
wire format looks like. The frontend contract can change here without any rule
changing underneath it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from app.api.i8.schemas import (
    Attestation,
    CodingCandidateItem,
    CodingCandidateLine,
    CodingCandidateMeta,
    CodingCandidatePrice,
    CodingCandidateTwin,
    DeclarationFacets,
    DeclarationItem,
    DeclarationMeta,
    ExceptionFacets,
    ExceptionHistoryEntry,
    ExceptionMeta,
    ExceptionQueueItem,
    LifecycleStage,
    LineException,
    MaterialReference,
    NotificationItem,
    PlantReference,
    RegisterFacets,
    RegisterMeta,
    RepairChain,
    RepairJustification,
    RepairJustificationEntry,
    SAPDocumentReference,
    UniverseItem,
    UniverseMeta,
    UniverseSources,
    UnjustifiedPurchase,
    VendorOption,
)
from app.assistant import departments
from app.initiatives.i8.aging import NO_LEAD_TIME, days_between
from app.initiatives.i8.attestation import (
    CONDITION_LABELS,
    LINK_BOUND,
    LINK_REFERENCE,
    LINK_WINDOW,
    Recommendation,
)
from app.initiatives.i8.coding_candidates import (
    CodingCandidate,
    ScreenStats,
    meets_confidence_threshold,
)
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.declarations import DeclarationRow
from app.initiatives.i8.exceptions import (
    RAISED_BY_I8,
    RAISED_WITHOUT_COMPLETE_FLOW,
    ExceptionItem,
    ExceptionStats,
)
from app.initiatives.i8.line_justifications import LineJustification
from app.initiatives.i8.exception_state import (
    UNTOUCHED,
    ExceptionWorkState,
    canonical_status,
)
from app.initiatives.i8.models import ExceptionEvent, Notification, RepairAttestation
from app.initiatives.i8.register import RegisterStats, RepairLine, is_placeholder_price, repair_cost
from app.initiatives.i8.requisitions import NOT_ORDERED, PR_RAISED, RepairRequest
from app.initiatives.i8.service import Snapshot
from app.initiatives.i8.universe import UniverseRow, UniverseStats


def plant_reference(plant: str | None, cfg: I8Settings) -> PlantReference | None:
    """A plant code with its name, or the code again when no name is known.

    Never invents a name. Only 1300 and 1500 are documented anywhere in this
    repository; the rest are served as codes so nobody reads a guess as a fact.
    """
    if not plant:
        return None
    return PlantReference(plant_id=plant, name=cfg.plant_name_map.get(plant, plant))


def universe_item(row: UniverseRow, cfg: I8Settings) -> UniverseItem:
    return UniverseItem(
        id=f"{row.material_id}-{row.plant or 'NA'}",
        material=MaterialReference(
            material_id=row.material_id,
            material_code=row.material_id,
            description=row.description,
        ),
        plant=plant_reference(row.plant, cfg),
        material_type=row.material_type,
        stock_on_hand=row.stock_on_hand,
        storage_locations=row.storage_locations,
        reorder_point=row.reorder_point,
        mrp_type=row.mrp_type,
        new_unit_lead_time_days=row.planned_delivery_days,
        criticality=row.criticality,
        has_open_repair=row.has_open_repair,
        open_repair_lines=row.open_repair_lines,
        qty_under_repair=row.qty_under_repair,
        in_material_master=row.in_material_master,
        # Null while the complete flow is off (I8_COMPLETE_FLOW_ENABLED).
        usable_stock=row.usable_stock if cfg.complete_flow_enabled else None,
        damaged_stock=row.damaged_stock if cfg.complete_flow_enabled else None,
    )


def stock_split(row: UniverseRow | None, cfg: I8Settings | None = None) -> dict:
    """The register row's stock split from its universe row (28-Oct-2026).

    ``stock_condition_known`` is null when there is no universe row, so a row
    with no stock record at all does not read as "condition unknown". All
    three are null while the complete flow is off (``cfg``), as on a register
    built before the split existed."""
    if row is None or (cfg is not None and not cfg.complete_flow_enabled):
        return {"usable_stock": None, "damaged_stock": None, "stock_condition_known": None}
    return {
        "usable_stock": row.usable_stock,
        "damaged_stock": row.damaged_stock,
        "stock_condition_known": row.stock_condition_source is not None,
    }


def repair_chain(
    line: RepairLine,
    cfg: I8Settings,
    *,
    stock_on_hand=None,
    reorder_point=None,
    new_unit_lead_time_days: int | None = None,
    declaration_status: str = "Required",
    criticality: str | None = None,
    declaration: DeclarationRow | None = None,
    justification: LineJustification | None = None,
    exceptions: Sequence[ExceptionItem] | None = None,
    usable_stock=None,
    damaged_stock=None,
    stock_condition_known: bool | None = None,
) -> RepairChain:
    """One register row.

    Stock and reorder point are passed in from the universe rather than
    re-queried: they belong to the material+plant, not to the PO line, and the
    snapshot already holds them.

    ``declaration_status`` comes from W5.3's attestation view for the same
    reason: it belongs to the attestation table, which has a different lifetime
    from the July snapshot. It defaults to "Required" because that is the honest
    answer for a line nobody has checked -- but it is now a real computed value
    rather than the placeholder it was through W5.2.

    ``declaration`` is the line's queue row, when the caller has the view: its
    status overrides ``declaration_status``, and it brings who declared, when,
    the condition and the next action with it.

    ``exceptions`` are the items raised against the line, when the caller
    computed them -- the register listing does, the detail endpoint does not.
    """
    if declaration is not None:
        declaration_status = declaration.status
    return RepairChain(
        id=f"{line.purchasing_document}-{line.item}",
        material=MaterialReference(
            material_id=line.material_id,
            material_code=line.material_id,
            description=line.description,
        ),
        plant=plant_reference(line.plant, cfg),
        stock_on_hand=stock_on_hand,
        reorder_point=reorder_point,
        qty_under_repair=line.qty_under_repair,
        repair_pr=SAPDocumentReference(
            type="PR", document_number=line.pr_number or "", line=line.pr_item
        ),
        repair_po=SAPDocumentReference(
            type="PO", document_number=line.purchasing_document, line=line.item
        ),
        vendor=line.vendor,
        vendor_name=line.vendor_name,
        repair_status=line.repair_status,
        declaration_status=declaration_status,
        overdue_status=line.overdue_status,
        lead_time_status=line.lead_time_status,
        days_open=line.days_open,
        aging_bucket=line.aging_bucket,
        lead_time_days=line.lead_time_days,
        days_elapsed=line.days_elapsed,
        days_over_lead_time=line.days_over_lead_time,
        days_in_current_stage=line.days_in_current_stage,
        days_remaining_in_repair=line.days_remaining,
        raised_at=line.raised_at,
        po_issued_at=line.po_date,
        sent_to_vendor_at=line.dispatched_at,
        expected_return=line.due_date,
        ordered_qty=line.ordered_qty,
        unit=line.unit,
        repair_cost=repair_cost(line.net_price, cfg),
        repair_cost_currency=line.currency,
        repair_cost_placeholder=line.net_price if is_placeholder_price(line.net_price, cfg) else None,
        new_unit_lead_time_days=new_unit_lead_time_days,
        item_category=line.item_category,
        doc_type=line.doc_type,
        corroborated_by_doc_type=line.corroborated_by_doc_type,
        has_po_header=line.has_po_header,
        schedule_lines=line.schedule_lines,
        criticality=criticality,
        declaration_id=declaration.declaration_id if declaration else None,
        declared_by=declaration.declared_by if declaration else None,
        declared_at=declaration.declared_at if declaration else None,
        condition=declaration.condition if declaration else None,
        next_action=declaration.next_action if declaration else None,
        # The department that raised the repair PR (27-Oct-2026), falling back
        # to the PO line's own requisitioner when that PR was not read.
        requester=line.pr_department or line.requisitioner,
        requester_name=department_name(line.pr_department or line.requisitioner),
        declaration_link=declaration.declaration_link if declaration else None,
        declaration_session_id=declaration.declaration_session_id if declaration else None,
        justification=repair_justification(justification) if justification else None,
        exceptions=(
            [line_exception(item) for item in exceptions] if exceptions is not None else None
        ),
        usable_stock=usable_stock,
        damaged_stock=damaged_stock,
        stock_condition_known=stock_condition_known,
    )


def request_chain(
    request: RepairRequest,
    cfg: I8Settings,
    *,
    stock_on_hand=None,
    reorder_point=None,
    new_unit_lead_time_days: int | None = None,
    criticality: str | None = None,
    exceptions: Sequence[ExceptionItem] | None = None,
    declaration: DeclarationRow | None = None,
    usable_stock=None,
    damaged_stock=None,
    stock_condition_known: bool | None = None,
) -> RepairChain:
    """One register row for an open repair PR with no PO yet.

    The same row shape as :func:`repair_chain`, so the screen needs no second
    table: what a PO line has and a PR does not -- the PO, the vendor, a
    promised date, a dispatch -- is null, and the days count from the PR date.
    Stock, reorder point and criticality come from the universe exactly as for
    a PO row: they belong to the material at the plant, not to the document.

    ``declaration`` is the PR's queue row, when the caller has the view: since
    27-Oct-2026 a PR can be declared on, and its status, who declared, when
    and the condition come from it. Without one the status is "Required".
    ``exceptions`` is an empty list on the listing and None on the detail
    page, as for a PO row.
    """
    return RepairChain(
        id=request.row_id,
        material=MaterialReference(
            material_id=request.material_id,
            material_code=request.material_id,
            description=request.description,
        ),
        plant=plant_reference(request.plant, cfg),
        stock_on_hand=stock_on_hand,
        reorder_point=reorder_point,
        qty_under_repair=request.quantity,
        repair_pr=SAPDocumentReference(
            type="PR", document_number=request.purchase_requisition, line=request.item
        ),
        repair_po=None,
        vendor=None,
        vendor_name=None,
        repair_status=PR_RAISED,
        declaration_status=declaration.status if declaration else "Required",
        declaration_id=declaration.declaration_id if declaration else None,
        declared_by=declaration.declared_by if declaration else None,
        declared_at=declaration.declared_at if declaration else None,
        condition=declaration.condition if declaration else None,
        next_action=declaration.next_action if declaration else None,
        declaration_link=declaration.declaration_link if declaration else None,
        declaration_session_id=declaration.declaration_session_id if declaration else None,
        overdue_status=NOT_ORDERED,
        lead_time_status=NO_LEAD_TIME,
        days_open=request.days_open,
        aging_bucket=request.aging_bucket,
        # Nothing has moved since the PR was raised, so the current stage is
        # as old as the request.
        days_in_current_stage=request.days_open,
        raised_at=request.requested_on,
        unit=request.unit,
        new_unit_lead_time_days=new_unit_lead_time_days,
        criticality=criticality,
        requester=request.department,
        requester_name=department_name(request.department),
        exceptions=(
            [line_exception(item) for item in exceptions] if exceptions is not None else None
        ),
        line_kind="PR",
        usable_stock=usable_stock,
        damaged_stock=damaged_stock,
        stock_condition_known=stock_condition_known,
    )


def department_name(code: str | None) -> str | None:
    """A department code's name from the list the assistant keeps, or None.

    Never waits for that list to load: a row shown with its code alone is
    better than a slow register."""
    return departments.name_for(code)


def repair_justification(value: LineJustification) -> RepairJustification:
    """One line's Justification cell."""
    return RepairJustification(
        status=value.status,
        entries=[
            RepairJustificationEntry(
                id=record.id,
                reason_category=record.reason_category,
                free_text=record.free_text,
                author=record.author,
                recorded_at=record.recorded_at,
                session_id=record.session_id,
            )
            for record in value.recorded
        ],
        unjustified_purchases=[
            UnjustifiedPurchase(
                exception_id=item.id,
                # A new-buy PR or a direct PO line (27-Oct-2026), as found.
                purchase=SAPDocumentReference(
                    type=item.acquisition_document_type,
                    document_number=item.acquisition_document or "",
                    line=item.acquisition_item,
                ),
                raised_at=item.raised_at,
                pre_automation=item.pre_automation,
            )
            for item in value.unjustified
        ],
    )


def _day(value) -> str:
    """A date the way the screens show one: ``08 May 2025``."""
    return value.strftime("%d %b %Y")


def _person(actor: str | None) -> str:
    """Who recorded something, without showing a placeholder id as a name."""
    if not actor or actor.upper().startswith("UNAUTHENTICATED"):
        return "someone not signed in"
    return actor


def _at_vendor_evidence(line: RepairLine) -> str:
    """Why this line is, or is not, showing time at a vendor.

    The evidence is read by the people using the repair page, so it names SAP
    movements the way they know them (541) rather than the tables behind them.
    """
    if line.dispatched_at is None:
        return (
            "Time at the vendor counts from the dispatch, which SAP has not "
            "recorded for this line. Stores often post the dispatch (movement "
            "541) on the day the part comes back, so few open lines have one."
        )
    promised = (
        f"Promised back {_day(line.due_date)}."
        if line.due_date
        else "The PO has no delivery date, so no return was promised."
    )
    return (
        f"At the vendor since {_day(line.dispatched_at)}, and SAP still has the "
        f"line open. {promised}"
    )


def _requested_evidence(document: str, item: str, department: str | None) -> str:
    """``Repair PR {BANFN}/{BNFPO}``, and the department that asked for it --
    named when the department list has its code, the code alone otherwise."""
    requested = f"Repair PR {document}/{item}"
    if department:
        name = department_name(department)
        requested += f", requested by department {department}" + (f" ({name})" if name else "")
    return f"{requested}."


def _declared_stage(
    attestation: RepairAttestation | None, today, link: str | None, reference: str | None
) -> LifecycleStage:
    """The declaration stage, saying how the declaration was matched (since
    27-Oct-2026): it names the line, the R-reference typed on the repair order
    names the assistant visit it was made in, or it is a part declared at the
    same plant within the date window -- three strengths of claim."""
    if attestation is None:
        return LifecycleStage(
            stage="attested", label="Condition declared", occurred_at=None, evidence="Not declared yet."
        )
    evidence = (
        f"Declaration {attestation.id}: "
        f"{CONDITION_LABELS[Recommendation(attestation.recommendation)]}"
        f" ({attestation.fault_category.replace('_', ' ').lower()}), "
        f"by {_person(getattr(attestation, 'attestor_name', None) or attestation.attestor)}."
    )
    if link == LINK_BOUND:
        evidence += " It names this repair."
    elif link == LINK_REFERENCE:
        evidence += (
            f" Linked by the R-reference {reference} typed on the repair order."
            if reference
            else " Linked by the R-reference typed on the repair order."
        )
    elif link == LINK_WINDOW:
        evidence += " Matched by material and plant within the declaration window."
    return LifecycleStage(
        stage="attested",
        label="Condition declared",
        occurred_at=attestation.attested_at.date(),
        evidence=evidence,
        days_since=days_between(attestation.attested_at.date(), today),
    )


def timeline(
    line: RepairLine,
    today,
    attestation: RepairAttestation | None = None,
    link: str | None = None,
    reference: str | None = None,
    *,
    requested_stage: bool = True,
) -> list[LifecycleStage]:
    """The lifecycle, stage by stage, with the evidence for each.

    ``requested_stage`` is False while the complete flow is off: no "Repair
    requested" stage, as before 27-Oct-2026.

    There is no "Removed from machine" stage since 27-Oct-2026 (ruled 26-Oct):
    SAP keeps no record of a unit coming off a machine -- the goods issue to
    the machine (261/201) names no repair PO, and on the 19-Oct measurement the
    stage was empty on every line by construction. "Condition declared" and the
    replacement issue tell that part of the story, so the stage was dropped
    rather than shown permanently blank.

    There is no "received" stage since 19-Oct-2026: every line served here is
    open in SAP, and goods receipts are not read. The line leaves the register
    when SAP marks it "Delivery completed".

    **The declaration stage is no longer a hook.** It read "W5.3 owns this.
    Hook only." until W5.3 landed; it now carries the real attestation where one covers this line,
    and says what was searched for where none does. That distinction is the
    whole point of the stage: "nobody assessed this part" and "we have not
    looked" are different statements.
    """
    stages: list[LifecycleStage] = []
    # The repair starts at the request (27-Oct-2026): the PR the PO was
    # ordered from, when the register knows it.
    if requested_stage and line.pr_number:
        stages.append(
            LifecycleStage(
                stage="pr_raised",
                label="Repair requested",
                occurred_at=getattr(line, "pr_requested_on", None),
                evidence=_requested_evidence(
                    line.pr_number, line.pr_item or "", getattr(line, "pr_department", None)
                ),
                days_since=days_between(getattr(line, "pr_requested_on", None), today),
            )
        )
    stages += [
        _declared_stage(attestation, today, link, reference),
        LifecycleStage(
            stage="po_raised",
            label="Repair PO raised",
            occurred_at=line.raised_at,
            evidence=(
                f"Repair PO {line.purchasing_document}/{line.item} "
                f"(item category {line.item_category}"
                + (f", document type {line.doc_type}" if line.doc_type else "")
                + ")"
            ),
            days_since=days_between(line.raised_at, today),
        ),
        LifecycleStage(
            stage="dispatched",
            label="Sent to vendor",
            occurred_at=line.dispatched_at,
            evidence=(
                "Goods issue to the vendor (movement 541) on this PO line."
                if line.dispatched_at
                else "No goods issue to the vendor (movement 541) is linked to this line."
            ),
            days_since=days_between(line.dispatched_at, today),
        ),
        LifecycleStage(
            stage="at_vendor",
            label="At vendor",
            # The stage began when the unit was dispatched. days_since is the
            # DURATION at the vendor so far: dispatch to today.
            occurred_at=line.dispatched_at,
            evidence=_at_vendor_evidence(line),
            days_since=line.days_at_vendor,
        ),
    ]
    return stages


def request_timeline(
    request: RepairRequest,
    today,
    attestation: RepairAttestation | None = None,
    link: str | None = None,
    reference: str | None = None,
) -> list[LifecycleStage]:
    """The lifecycle of a repair PR with no PO yet: requested, declared (or
    not yet), and the PO it is waiting on, saying plainly it has not happened.
    """
    return [
        LifecycleStage(
            stage="pr_raised",
            label="Repair requested",
            occurred_at=request.requested_on,
            evidence=_requested_evidence(request.purchase_requisition, request.item, request.department),
            days_since=days_between(request.requested_on, today),
        ),
        _declared_stage(attestation, today, link, reference),
        LifecycleStage(
            stage="po_raised",
            label="Repair PO raised",
            occurred_at=None,
            evidence="No PO yet.",
        ),
    ]


def universe_meta(stats: UniverseStats) -> UniverseMeta:
    return UniverseMeta(
        total_rows=stats.rows,
        total_materials=stats.materials,
        plants=stats.plants,
        by_source=UniverseSources(**stats.by_source),
        with_stock=stats.with_stock,
        with_reorder_point=stats.with_reorder_point,
        with_criticality=stats.with_criticality,
        with_criticality_from_other_plant=stats.with_criticality_from_other_plant,
        with_open_repair=stats.with_open_repair,
        in_material_master=stats.in_material_master,
    )


#: The register counts of the open repair PRs, served as 0 while the complete
#: flow is off: before it the register did not look for them at all.
_REQUEST_COUNTS = (
    "open_repair_requests",
    "requisitions_excluded_deleted",
    "requisitions_excluded_live_po",
    "requisitions_excluded_reservation_closed",
)


def register_meta(stats: RegisterStats, cfg: I8Settings | None = None) -> RegisterMeta:
    """The register's counts, whatever the filters. With ``cfg`` and the
    complete flow off, the repair-PR counts read 0."""
    counts = vars(stats)
    if cfg is not None and not cfg.complete_flow_enabled:
        counts = {**counts, **{name: 0 for name in _REQUEST_COUNTS}}
    return RegisterMeta(**counts)


def register_facets(
    lines: Sequence[RepairLine],
    cfg: I8Settings,
    criticality_of: Callable[[RepairLine | RepairRequest], str | None],
    requests: Sequence[RepairRequest] = (),
) -> RegisterFacets:
    """The plants, vendors, repair statuses and criticalities among ``lines``
    and the served repair PRs, ``requests``.

    ``criticality_of`` is the rating the row shows -- its material's at its
    plant -- so the dropdown offers exactly what the column displays. A PR row
    has no vendor, so any PR makes ``vendor=none`` worth offering.
    """
    criticalities = [criticality_of(row) for row in (*lines, *requests)]
    return RegisterFacets(
        plants=_plant_options(
            (*(line.plant for line in lines), *(request.plant for request in requests)), cfg
        ),
        vendors=_vendor_options((line.vendor, line.vendor_name) for line in lines),
        has_unknown_vendor=bool(requests) or any(not line.vendor for line in lines),
        repair_statuses=sorted(
            {line.repair_status for line in lines} | ({PR_RAISED} if requests else set())
        ),
        criticalities=sorted({c for c in criticalities if c}),
        has_unrated=any(not c for c in criticalities),
    )


def line_exception(item: ExceptionItem) -> LineException:
    return LineException(
        id=item.id, type=item.type, severity=item.severity, pre_automation=item.pre_automation
    )


# --- W5.3: attestation, declarations, exceptions --------------------------


def material_descriptions(snapshot: Snapshot | None, material_ids: Iterable[str]) -> dict[str, str]:
    """Material descriptions by material number, for the ones the snapshot knows.

    The universe first, then the repair lines -- a material can have a repair
    line and no universe row. Empty without a snapshot: this is a label, and a
    caller should show the number alone rather than wait for a build.
    """
    wanted = set(material_ids)
    if snapshot is None or not wanted:
        return {}
    found: dict[str, str] = {}
    for source in (snapshot.universe, snapshot.lines):
        for row in source:
            if row.material_id in wanted and row.description and row.material_id not in found:
                found[row.material_id] = row.description
    return found


def attestation_item(
    row: RepairAttestation,
    cfg: I8Settings,
    *,
    superseded_by: str | None = None,
    covers_repair_lines: list[str] | None = None,
    coverage_note: str | None = None,
    description: str | None = None,
) -> Attestation:
    """One stored attestation.

    ``superseded_by`` is passed in rather than followed from the row, because
    resolving it per row is a query per attestation. The caller already has the
    whole list and can build the reverse index once.

    ``description`` is the material's, looked up when the row is read -- see
    :func:`material_descriptions`.
    """
    return Attestation(
        id=row.id,
        material=MaterialReference(
            material_id=row.material_id,
            material_code=row.material_id,
            # Never stored with the attestation: copied at write time it would
            # freeze a description that lives somewhere else. The caller reads
            # it from the snapshot instead, when there is one.
            description=description,
        ),
        plant=plant_reference(row.plant, cfg),
        quantity=row.quantity,
        condition_description=row.condition_description,
        fault_category=row.fault_category,
        recommendation=row.recommendation,
        condition=CONDITION_LABELS[Recommendation(row.recommendation)],
        serial_number=row.serial_number,
        evidence_reference=row.evidence_reference,
        attestor=row.attestor,
        attested_at=row.attested_at,
        session_id=row.session_id,
        repair_document=row.repair_document,
        repair_item=row.repair_item,
        supersedes=row.supersedes,
        superseded_by=superseded_by,
        attestor_name=row.attestor_name,
        amendment_reason=row.amendment_reason,
        repair_document_type=(
            (row.repair_document_type or "PO") if row.repair_document else None
        ),
        covers_repair_lines=covers_repair_lines,
        coverage_note=coverage_note,
    )


def declaration_item(row: DeclarationRow, cfg: I8Settings) -> DeclarationItem:
    return DeclarationItem(
        id=row.id,
        pr=(
            SAPDocumentReference(
                type="PR", document_number=row.pr_number, line=row.pr_item
            )
            if row.pr_number
            else None
        ),
        material=MaterialReference(
            material_id=row.material_id,
            material_code=row.material_id,
            description=row.description,
        ),
        plant=plant_reference(row.plant, cfg),
        requester=row.requester,
        requester_name=row.requester_name,
        repair_document_type=row.repair_document_type,
        related_repair_id=row.related_repair_id,
        vendor=row.vendor,
        vendor_name=row.vendor_name,
        repair_status=row.repair_status,
        repair_po=(
            SAPDocumentReference(
                type="PO", document_number=row.repair_document, line=row.repair_item
            )
            # A PR row's repair document is the PR, already served as ``pr``.
            if row.repair_document and row.repair_document_type == "PO"
            else None
        ),
        status=row.status,
        declaration_id=row.declaration_id,
        declared_by=row.declared_by,
        declared_at=row.declared_at,
        condition=row.condition,
        next_action=row.next_action,
        created_at=row.created_at,
    )


def declaration_meta(rows, window_days: int) -> DeclarationMeta:
    by_status: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
    return DeclarationMeta(
        total=len(rows),
        by_status=by_status,
        outstanding=sum(1 for r in rows if r.is_outstanding),
        attestation_window_days=window_days,
    )


def declaration_facets(rows: Sequence[DeclarationRow], cfg: I8Settings) -> DeclarationFacets:
    """The plants, vendors and repair statuses present among ``rows``."""
    return DeclarationFacets(
        plants=_plant_options((r.plant for r in rows), cfg),
        vendors=_vendor_options((r.vendor, r.vendor_name) for r in rows),
        repair_statuses=sorted({r.repair_status for r in rows if r.repair_status}),
    )


def _plant_options(plants: Iterable[str | None], cfg: I8Settings) -> list[PlantReference]:
    """Each plant once, by code, with its name."""
    return [
        reference
        for plant in sorted({p for p in plants if p})
        if (reference := plant_reference(plant, cfg)) is not None
    ]


def _vendor_options(vendors: Iterable[tuple[str | None, str | None]]) -> list[VendorOption]:
    """Each vendor once, from (code, name) pairs; pairs with no code are skipped.

    Keyed by the code, and a code seen once without a name and once with one
    keeps the name. Sorted by what a dropdown shows -- the name, else the code
    -- so the list reads alphabetically rather than by code.
    """
    names: dict[str, str | None] = {}
    for code, name in vendors:
        if code:
            names[code] = names.get(code) or name
    return [
        VendorOption(vendor=code, name=name)
        for code, name in sorted(
            names.items(), key=lambda entry: ((entry[1] or entry[0]).lower(), entry[0])
        )
    ]


def exception_item(
    item: ExceptionItem,
    cfg: I8Settings,
    state: ExceptionWorkState | None = None,
    still_detected: bool = True,
) -> ExceptionQueueItem:
    """One computed finding, plus whatever people have done about it.

    ``state`` is None for an item nobody has touched, and ``UNTOUCHED`` is then
    the truth about it rather than a placeholder: an exception starts OPEN and
    unassigned, which is exactly what an empty history means.
    """
    work = state or UNTOUCHED
    return ExceptionQueueItem(
        id=item.id,
        type=item.type,
        severity=item.severity,
        material=MaterialReference(
            material_id=item.material_id,
            material_code=item.material_id,
            description=item.description,
        ),
        plant=plant_reference(item.plant, cfg),
        # The document kinds as the finding names them: a repair PR with no PO
        # yet is the bare PR number with type PR, and the purchase is usually a
        # new-buy PR (27-Oct-2026). Every other check names a PO line.
        repair_line=SAPDocumentReference(
            type=item.repair_document_type, document_number=item.purchasing_document, line=item.item
        ),
        acquisition_line=(
            SAPDocumentReference(
                type=item.acquisition_document_type,
                document_number=item.acquisition_document,
                line=item.acquisition_item,
            )
            if item.acquisition_document
            else None
        ),
        title=item.title,
        detail=item.detail,
        raised_at=item.raised_at,
        pre_automation=item.pre_automation,
        declaration_id=item.declaration_id,
        reservation_line=(
            SAPDocumentReference(
                type="RESERVATION", document_number=item.reservation_document, line=item.reservation_item
            )
            if item.reservation_document
            else None
        ),
        status=work.status,
        owner=work.owner,
        still_detected=still_detected,
        history=[exception_history_entry(e) for e in work.history],
    )


def notification_item(
    row: Notification,
    *,
    repair_line: str | None = None,
    description: str | None = None,
    plant_name: str | None = None,
) -> NotificationItem:
    """A logged notification on the wire. ``emailed`` follows ``emailed_at``,
    which nothing sets until the email sender exists. The keyword fields are
    labels the route looks up at read time (28-Oct-2026)."""
    return NotificationItem(
        repair_line=repair_line,
        description=description,
        plant_name=plant_name,
        id=row.id,
        exception_id=row.exception_id,
        type=row.exception_type,
        severity=row.severity,
        title=row.title,
        material_id=row.material_id,
        plant=row.plant,
        logged_at=row.logged_at,
        channel="EMAIL",
        emailed=row.emailed_at is not None,
    )


def exception_history_entry(event: ExceptionEvent) -> ExceptionHistoryEntry:
    return ExceptionHistoryEntry(
        id=event.id,
        kind=event.kind,
        status=canonical_status(event.status),
        owner=event.owner,
        actor=event.actor,
        text=event.text or "",
        at=event.at,
    )


def exception_meta(
    stats: ExceptionStats,
    excluded_types: frozenset[str] = frozenset(),
    excluded: Sequence[ExceptionItem] = (),
    *,
    complete_flow: bool = True,
) -> ExceptionMeta:
    """The counts, less whatever the caller's queue does not carry.

    ``excluded_types`` are the checks taken out with ``excludeType`` and
    ``excluded`` the items they raised. Their items come off ``total``,
    ``byType``, ``bySeverity``, ``preAutomation`` and ``actionable`` and their
    types off ``typesRaised``, so the meta describes the queue as it is listed.
    Subtracted rather than recounted: the lines checked and covered and the
    windows are facts about the run that no count of items reproduces.

    ``complete_flow`` False (``I8_COMPLETE_FLOW_ENABLED`` off) names only the
    types raised then in ``typesRaised``.
    """
    implemented = RAISED_BY_I8 if complete_flow else RAISED_WITHOUT_COMPLETE_FLOW
    pre_automation = sum(1 for item in excluded if item.pre_automation)
    by_severity = dict(stats.by_severity)
    for item in excluded:
        if item.severity in by_severity:
            by_severity[item.severity] = max(0, by_severity[item.severity] - 1)
    return ExceptionMeta(
        total=max(0, stats.total - len(excluded)),
        by_type={t: n for t, n in stats.by_type.items() if t not in excluded_types},
        by_severity=by_severity,
        lines_checked=stats.lines_checked,
        lines_covered=stats.lines_covered,
        attestation_window_days=stats.attestation_window_days,
        pre_automation=max(0, stats.pre_automation - pre_automation),
        actionable=max(0, stats.actionable - (len(excluded) - pre_automation)),
        attestation_cutover_date=stats.attestation_cutover_date,
        # Which types are actually implemented, so an empty count is
        # distinguishable from an unimplemented check.
        types_raised=sorted(t.value for t in implemented if t.value not in excluded_types),
        acquisitions_checked=stats.acquisitions_checked,
        justification_window_days=stats.justification_window_days,
        justification_cutover_date=stats.justification_cutover_date,
        session_check_start_date=stats.session_check_start_date,
    )


def exception_facets(items: Sequence[ExceptionItem], cfg: I8Settings) -> ExceptionFacets:
    """The plants and types present among ``items``."""
    return ExceptionFacets(
        plants=_plant_options((i.plant for i in items), cfg),
        types=sorted({i.type for i in items}),
    )


# --- W5.5: coding candidates ----------------------------------------------


def coding_candidate_item(
    candidate: CodingCandidate, cfg: I8Settings
) -> CodingCandidateItem:
    return CodingCandidateItem(
        material_id=candidate.material_id,
        verdict=candidate.verdict,
        confidence=candidate.confidence,
        reason=candidate.reason,
        plants=list(candidate.plants),
        lines=[
            CodingCandidateLine(
                source=line.source,
                document=line.document,
                document_item=line.document_item,
                plant=plant_reference(line.plant, cfg),
                short_text=line.short_text,
                matched_keywords=list(line.matched),
                raised_at=line.raised_at,
                item_category=line.item_category,
            )
            for line in candidate.lines
        ],
        distinct_texts=list(candidate.texts),
        twins=[
            CodingCandidateTwin(material_id=material, shared_text=shared)
            for material, shared in candidate.twins
        ],
        is_corroborated=candidate.is_corroborated,
        is_actionable=candidate.is_actionable,
        meets_confidence_threshold=meets_confidence_threshold(
            candidate.confidence, cfg.coding_candidate_confidence_threshold
        ),
        in_repairable_universe=candidate.in_repairable_universe,
        model=candidate.model,
        provider=candidate.provider,
        prompt_version=candidate.prompt_version,
        screened_at=candidate.screened_at,
        new_item_prices=[
            CodingCandidatePrice(
                plant=price.plant,
                price=price.price,
                valuation_type=price.valuation_type,
                missing=price.missing,
                mbew_value=price.mbew_value,
            )
            for price in candidate.new_item_prices
        ],
    )


def coding_candidate_meta(stats: ScreenStats) -> CodingCandidateMeta:
    return CodingCandidateMeta(
        sources=list(stats.sources),
        lines_with_text=stats.lines_with_text,
        lines_with_text_by_source=dict(stats.lines_with_text_by_source),
        lines_with_repair_language=stats.lines_with_repair_language,
        lines_by_source=dict(stats.lines_by_source),
        deleted_requisitions_dropped=stats.deleted_requisitions_dropped,
        converted_requisitions_dropped=stats.converted_requisitions_dropped,
        verdicts_reused=stats.verdicts_reused,
        lines_already_eighty_series=stats.lines_already_eighty_series,
        lines_without_material=stats.lines_without_material,
        lines_screened=stats.lines_screened,
        materials_found=stats.materials_found,
        materials_screened=stats.materials_screened,
        was_truncated=stats.was_truncated,
        corroborated=stats.corroborated,
        new_item_price_counts=dict(stats.new_item_price_counts),
        by_verdict=stats.by_verdict,
        keywords=list(stats.keywords),
        provider=stats.provider,
        model=stats.model,
        staged_rows_kept=stats.staged_rows_kept,
        staged_rows_scanned=stats.staged_rows_scanned,
    )
