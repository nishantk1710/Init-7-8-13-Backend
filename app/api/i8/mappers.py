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
    VendorTurnaroundItem,
)
from app.initiatives.i8.aging import days_between
from app.initiatives.i8.attestation import CONDITION_LABELS, Recommendation
from app.initiatives.i8.coding_candidates import (
    CodingCandidate,
    ScreenStats,
    meets_confidence_threshold,
)
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.declarations import DeclarationRow
from app.initiatives.i8.exceptions import RAISED_BY_I8, ExceptionItem, ExceptionStats
from app.initiatives.i8.line_justifications import LineJustification
from app.initiatives.i8.exception_state import UNTOUCHED, ExceptionWorkState
from app.initiatives.i8.models import ExceptionEvent, RepairAttestation
from app.initiatives.i8.register import PARTIALLY_RECEIVED, RegisterStats, RepairLine
from app.initiatives.i8.universe import UniverseRow, UniverseStats
from app.initiatives.i8.vendors import VendorTurnaround


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
    )


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
        receipt_status=line.receipt_status,
        overdue_status=line.overdue_status,
        lead_time_status=line.lead_time_status,
        days_open=line.days_open,
        aging_bucket=line.aging_bucket,
        lead_time_days=line.lead_time_days,
        days_elapsed=line.days_elapsed,
        days_over_lead_time=line.days_over_lead_time,
        days_at_vendor=line.days_at_vendor,
        days_in_current_stage=line.days_in_current_stage,
        days_remaining_in_repair=line.days_remaining,
        raised_at=line.raised_at,
        po_issued_at=line.po_date,
        sent_to_vendor_at=line.dispatched_at,
        expected_return=line.due_date,
        received_at=line.received_at,
        ordered_qty=line.ordered_qty,
        received_qty=line.received_qty,
        unit=line.unit,
        repair_cost=line.net_price,
        new_unit_lead_time_days=new_unit_lead_time_days,
        item_category=line.item_category,
        doc_type=line.doc_type,
        corroborated_by_doc_type=line.corroborated_by_doc_type,
        has_po_header=line.has_po_header,
        delivery_completed=line.delivery_completed,
        reversals=line.reversals,
        schedule_lines=line.schedule_lines,
        criticality=criticality,
        po_blocked=line.po_blocked,
        declaration_id=declaration.declaration_id if declaration else None,
        declared_by=declaration.declared_by if declaration else None,
        declared_at=declaration.declared_at if declaration else None,
        condition=declaration.condition if declaration else None,
        next_action=declaration.next_action if declaration else None,
        requester=line.requisitioner,
        justification=repair_justification(justification) if justification else None,
        exceptions=(
            [line_exception(item) for item in exceptions] if exceptions is not None else None
        ),
    )


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
                purchase=SAPDocumentReference(
                    type="PO",
                    document_number=item.acquisition_document or "",
                    line=item.acquisition_item,
                ),
                raised_at=item.raised_at,
                pre_automation=item.pre_automation,
            )
            for item in value.unjustified
        ],
    )


def _at_vendor_evidence(line: RepairLine) -> str:
    """Why this line is, or is not, showing time at a vendor."""
    if line.dispatched_at is None:
        return (
            "No dispatch movement attributable to this line, so time at the "
            "vendor cannot be measured. Measured across the extract, NO open "
            "repair line has an attachable 541 -- dispatch movements exist only "
            "for repairs that have already come back."
        )
    promised = (
        f"Promised back {line.due_date}."
        if line.due_date
        else "No schedule line, so no promised return date exists."
    )
    # Keyed on the receipt itself, not on is_open. Now that openness is read
    # off the quantity, a partially received line is both open AND holds a
    # receipt date, and a line with nothing ordered is closed with no receipt
    # date at all -- "Returned None." is what the old two-way branch printed.
    if line.received_at is None:
        return f"Still at the vendor. {promised}"
    if line.is_open:
        return (
            f"Partly back {line.received_at}; {line.qty_under_repair} of "
            f"{line.ordered_qty} still out. {promised}"
        )
    return f"Returned {line.received_at}. {promised}"


def timeline(
    line: RepairLine, today, attestation: RepairAttestation | None = None
) -> list[LifecycleStage]:
    """The lifecycle, stage by stage, with the evidence for each.

    Stage 1 -- removal from the machine -- is present as explicitly unavailable
    rather than omitted. A reader should be able to see that it was considered
    and why it cannot be shown, instead of wondering whether it was forgotten.

    **Stage 2 is no longer a hook.** It read "W5.3 owns this. Hook only." until
    W5.3 landed; it now carries the real attestation where one covers this line,
    and says what was searched for where none does. That distinction is the
    whole point of the stage: "nobody assessed this part" and "we have not
    looked" are different statements.
    """
    stages: list[LifecycleStage] = [
        LifecycleStage(
            stage="removed",
            label="Removed from machine",
            occurred_at=None,
            evidence=(
                "MSEG 261/201 carries no PO reference at all, so removal cannot "
                "be tied to this line. Modelled at material+plant level or left "
                "out -- never linked by a guess."
            ),
        ),
        LifecycleStage(
            stage="attested",
            label="Condition attested",
            occurred_at=attestation.attested_at.date() if attestation else None,
            evidence=(
                (
                    f"Attestation {attestation.id}: "
                    f"{CONDITION_LABELS[Recommendation(attestation.recommendation)]}"
                    f" ({attestation.fault_category.replace('_', ' ').lower()}), "
                    f"by {attestation.attestor}"
                )
                if attestation
                else (
                    "No condition-to-repair attestation covers this line. The "
                    "part was sent for repair with no recorded assessment -- "
                    "see GET /api/i8/exceptions. Until this platform there was "
                    "nowhere to record one, so this is expected on every "
                    "historical repair rather than a data gap."
                )
            ),
            days_since=(
                days_between(attestation.attested_at.date(), today)
                if attestation
                else None
            ),
        ),
        LifecycleStage(
            stage="po_raised",
            label="Repair PO raised",
            occurred_at=line.raised_at,
            evidence=(
                f"EKPO line {line.purchasing_document}/{line.item}, "
                f"item category {line.item_category}"
                + (f", document type {line.doc_type}" if line.doc_type else "")
            ),
            days_since=days_between(line.raised_at, today),
        ),
        LifecycleStage(
            stage="dispatched",
            label="Dispatched to vendor",
            occurred_at=line.dispatched_at,
            evidence=(
                "MSEG 541 on this PO line (vendor-stock side)"
                if line.dispatched_at
                else "No 541 movement attributable to this line."
            ),
            days_since=days_between(line.dispatched_at, today),
        ),
        LifecycleStage(
            stage="at_vendor",
            label="At vendor",
            # The stage began when the unit was dispatched, whether or not it
            # has since come back. days_since is the DURATION at the vendor --
            # to the receipt where there is one, to today where there is not.
            occurred_at=line.dispatched_at,
            evidence=_at_vendor_evidence(line),
            days_since=line.days_at_vendor,
        ),
        LifecycleStage(
            stage="received",
            label="Returned and received",
            occurred_at=line.received_at,
            evidence=(
                f"EKBE category E movement 101, net of {line.reversals} reversal(s)"
                if line.received_at
                else "No goods receipt yet."
            ),
            days_since=days_between(line.received_at, today),
        ),
    ]
    return stages


def vendor_item(vendor: VendorTurnaround) -> VendorTurnaroundItem:
    return VendorTurnaroundItem(
        vendor=vendor.vendor,
        vendor_name=vendor.vendor_name,
        total_lines=vendor.total_lines,
        open_count=vendor.open_count,
        overdue_count=vendor.overdue_count,
        received_count=vendor.received_count,
        avg_turnaround_days=vendor.avg_turnaround_days,
        min_turnaround_days=vendor.min_turnaround_days,
        max_turnaround_days=vendor.max_turnaround_days,
        turnaround_sample=vendor.turnaround_sample,
        on_time_rate=vendor.on_time_rate,
        avg_days_open=vendor.avg_days_open,
    )


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


def register_meta(
    stats: RegisterStats,
    held: Sequence[RepairLine] | None = None,
    listed: Sequence[RepairLine] = (),
) -> RegisterMeta:
    """The register's counts -- restated over ``listed`` when ``excludeHeld``
    took ``held`` out (``held`` is None when it did not).

    Without the restatement the caption would describe a register nobody can
    see: "5,155 repair lines, 827 still open" over a listing of 5,063. Only the
    four figures the caption reads change; the rest still count every line.
    """
    meta = RegisterMeta(**vars(stats))
    if held is None:
        return meta
    return meta.model_copy(
        update={
            "total_lines": max(0, stats.total_lines - len(held)),
            "open_lines": max(0, stats.open_lines - sum(1 for line in held if line.is_open)),
            "blocked_lines": sum(1 for line in listed if line.po_blocked),
            "partially_received_lines": sum(
                1 for line in listed if line.receipt_status == PARTIALLY_RECEIVED
            ),
            "held_lines": len(held),
        }
    )


def register_facets(
    lines: Sequence[RepairLine],
    cfg: I8Settings,
    criticality_of: Callable[[RepairLine], str | None],
) -> RegisterFacets:
    """The plants, vendors, repair statuses and criticalities among ``lines``.

    ``criticality_of`` is the rating the line shows -- its material's at its
    plant -- so the dropdown offers exactly what the column displays.
    """
    criticalities = [criticality_of(line) for line in lines]
    return RegisterFacets(
        plants=_plant_options((line.plant for line in lines), cfg),
        vendors=_vendor_options((line.vendor, line.vendor_name) for line in lines),
        has_unknown_vendor=any(not line.vendor for line in lines),
        repair_statuses=sorted({line.repair_status for line in lines}),
        criticalities=sorted({c for c in criticalities if c}),
        has_unrated=any(not c for c in criticalities),
    )


def line_exception(item: ExceptionItem) -> LineException:
    return LineException(id=item.id, type=item.type, severity=item.severity)


# --- W5.3: attestation, declarations, exceptions --------------------------


def attestation_item(
    row: RepairAttestation,
    cfg: I8Settings,
    *,
    superseded_by: str | None = None,
    covers_repair_lines: list[str] | None = None,
    coverage_note: str | None = None,
) -> Attestation:
    """One stored attestation.

    ``superseded_by`` is passed in rather than followed from the row, because
    resolving it per row is a query per attestation. The caller already has the
    whole list and can build the reverse index once.
    """
    return Attestation(
        id=row.id,
        material=MaterialReference(
            material_id=row.material_id,
            material_code=row.material_id,
            # No description here on purpose: the attestation table does not
            # carry one, and copying it from the register at write time would
            # freeze a description that lives somewhere else.
            description=None,
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
        has_active_repair=row.has_active_repair,
        related_repair_id=row.related_repair_id,
        vendor=row.vendor,
        vendor_name=row.vendor_name,
        repair_status=row.repair_status,
        repair_po=(
            SAPDocumentReference(
                type="PO", document_number=row.repair_document, line=row.repair_item
            )
            if row.repair_document
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
        repair_line=SAPDocumentReference(
            type="PO", document_number=item.purchasing_document, line=item.item
        ),
        acquisition_line=(
            SAPDocumentReference(
                type="PO",
                document_number=item.acquisition_document,
                line=item.acquisition_item,
            )
            if item.acquisition_document
            else None
        ),
        title=item.title,
        detail=item.detail,
        raised_at=item.raised_at,
        is_open_repair=item.is_open_repair,
        pre_automation=item.pre_automation,
        status=work.status,
        owner=work.owner,
        still_detected=still_detected,
        history=[exception_history_entry(e) for e in work.history],
    )


def exception_history_entry(event: ExceptionEvent) -> ExceptionHistoryEntry:
    return ExceptionHistoryEntry(
        id=event.id,
        kind=event.kind,
        status=event.status,
        owner=event.owner,
        actor=event.actor,
        text=event.text or "",
        at=event.at,
    )


def exception_meta(
    stats: ExceptionStats,
    excluded_types: frozenset[str] = frozenset(),
    excluded: Sequence[ExceptionItem] = (),
) -> ExceptionMeta:
    """The counts, less whatever the caller's queue does not carry.

    ``excluded_types`` are the checks taken out with ``excludeType`` and
    ``excluded`` the items they raised. Their items come off ``total``,
    ``byType``, ``bySeverity``, ``preAutomation`` and ``actionable`` and their
    types off ``typesRaised``, so the meta describes the queue as it is listed.
    Subtracted rather than recounted: the lines checked and covered and the
    windows are facts about the run that no count of items reproduces.
    """
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
        types_raised=sorted(t.value for t in RAISED_BY_I8 if t.value not in excluded_types),
        acquisitions_checked=stats.acquisitions_checked,
        justification_window_days=stats.justification_window_days,
        justification_cutover_date=stats.justification_cutover_date,
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
