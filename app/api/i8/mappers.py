"""Domain objects -> API models.

Kept out of the routes so a route stays three lines -- parse, call, shape -- and
out of ``app/initiatives/i8`` so the business layer never has to know what the
wire format looks like. The frontend contract can change here without any rule
changing underneath it.
"""

from __future__ import annotations

from app.api.i8.schemas import (
    Attestation,
    DeclarationItem,
    DeclarationMeta,
    ExceptionMeta,
    ExceptionQueueItem,
    LifecycleStage,
    MaterialReference,
    PlantReference,
    RegisterMeta,
    RepairChain,
    SAPDocumentReference,
    UniverseItem,
    UniverseMeta,
    UniverseSources,
    VendorTurnaroundItem,
)
from app.initiatives.i8.aging import days_between
from app.initiatives.i8.attestation import CONDITION_LABELS, Recommendation
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.declarations import DeclarationRow
from app.initiatives.i8.exceptions import RAISED_BY_I8, ExceptionItem, ExceptionStats
from app.initiatives.i8.models import RepairAttestation
from app.initiatives.i8.register import RegisterStats, RepairLine
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
) -> RepairChain:
    """One register row.

    Stock and reorder point are passed in from the universe rather than
    re-queried: they belong to the material+plant, not to the PO line, and the
    snapshot already holds them.
    """
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
        receipt_status=line.receipt_status,
        overdue_status=line.overdue_status,
        days_open=line.days_open,
        aging_bucket=line.aging_bucket,
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
    if line.is_open:
        return f"Still at the vendor. {promised}"
    return f"Returned {line.received_at}. {promised}"


def timeline(line: RepairLine, today) -> list[LifecycleStage]:
    """The lifecycle, stage by stage, with the evidence for each.

    Stages 1 and 2 of the full lifecycle -- removal from the machine and the
    condition attestation -- are present as explicitly unavailable rather than
    omitted. A reader should be able to see that they were considered and why
    they cannot be shown, instead of wondering whether they were forgotten.
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
            occurred_at=None,
            evidence="W5.3 owns this. Hook only.",
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


def register_meta(stats: RegisterStats) -> RegisterMeta:
    return RegisterMeta(**vars(stats))


# --- W5.3: attestation, declarations, exceptions --------------------------


def attestation_item(
    row: RepairAttestation,
    cfg: I8Settings,
    *,
    superseded_by: str | None = None,
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
        supersedes=row.supersedes,
        superseded_by=superseded_by,
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
        source=row.source,
        has_active_repair=row.has_active_repair,
        related_repair_id=row.related_repair_id,
        status=row.status,
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


def exception_item(item: ExceptionItem, cfg: I8Settings) -> ExceptionQueueItem:
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
        title=item.title,
        detail=item.detail,
        raised_at=item.raised_at,
        is_open_repair=item.is_open_repair,
    )


def exception_meta(stats: ExceptionStats) -> ExceptionMeta:
    return ExceptionMeta(
        total=stats.total,
        by_type=stats.by_type,
        by_severity=stats.by_severity,
        lines_checked=stats.lines_checked,
        lines_covered=stats.lines_covered,
        attestation_window_days=stats.attestation_window_days,
        # Which types are actually implemented, so an empty count is
        # distinguishable from an unimplemented check.
        types_raised=sorted(t.value for t in RAISED_BY_I8),
    )
