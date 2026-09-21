"""Response models for /api/i8.

Shaped to the contract the frontend already defines in
``Init-7-8-13-Frontend/src/features/initiative-8/types/repair.ts``. W5.4 (the
register UI) is In Progress against dummy data on the same critical path, so
every field renamed here costs someone else an afternoon and a merge conflict
on 16-Sep. Where a name exists over there, it is used here.

Field names are camelCase to match, via an alias generator, so the Python side
stays snake_case and nothing has to be transliterated by hand.

Three deliberate departures from the frontend type, each because the data does
not support it:

``expectedReturn`` is optional here and required there
    63 repair lines have no schedule line in EKET at all. Sending a fabricated
    date would be worse than sending null: it is the lines with no agreed date
    that most need chasing.

``vendor`` is optional here and required there
    Vendors come from the PO header, and 455 repair lines have no header in
    this extract. Null means "not known from this data", which the UI can show;
    an empty string would read as a vendor whose name is blank.

``newUnitCost`` is absent
    No valuation source is in I08's table set. MBEW was extracted for I07 and
    I13. Rather than invent a number or quietly send zero -- which would make
    every repair look infinitely worth doing -- the field is left out and the
    gap is stated.

``repairPR`` needed no departure: EKPO carries a requisition number on all
1,225 repair lines, so it is served as the non-optional reference the frontend
already expects.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class I8Model(BaseModel):
    """Base: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class MaterialReference(I8Model):
    """Matches the shared frontend contract of the same name."""

    material_id: str
    material_code: str
    description: str | None = None


class PlantReference(I8Model):
    plant_id: str
    name: str
    """The display name, or the plant code when no name is documented."""


class SAPDocumentReference(I8Model):
    type: Literal["RR", "RESERVATION", "PR", "PO", "GR", "GI"]
    document_number: str
    line: str | None = None


# --- Universe (W5.1) ------------------------------------------------------


class UniverseItem(I8Model):
    """One repairable material at one plant."""

    id: str
    material: MaterialReference
    plant: PlantReference | None = None

    material_type: str | None = None
    """ZREP where the material master knows this material, else null."""

    stock_on_hand: Decimal | None = None
    """Summed across storage locations. Null means no MARD row, not zero stock."""

    storage_locations: int = 0
    reorder_point: Decimal | None = None
    """Null outside plants 1300 and 1200 -- MARC covers no other plant."""

    mrp_type: str | None = None
    new_unit_lead_time_days: int | None = None
    criticality: str | None = None
    """One of NORMAL, OBSOLETE, CRITICAL, IMPACT, INSURANCE -- or null. Never
    defaulted: an unknown tier shown as NORMAL is a wrong answer."""

    has_open_repair: bool = False
    open_repair_lines: int = 0
    qty_under_repair: Decimal = Decimal(0)

    in_material_master: bool = False
    """False for roughly nine in ten of the series -- the MARA extract is a
    slice, which is why detection is a rule and not a lookup."""


class UniverseSources(I8Model):
    """Distinct 80-series materials per source. Quoted with every total so no
    figure travels without the extract it came from."""

    mara: int = 0
    mard: int = 0
    marc: int = 0
    ekpo: int = 0
    zmm065: int = 0


class UniverseMeta(I8Model):
    total_rows: int
    total_materials: int
    plants: int
    by_source: UniverseSources
    with_stock: int
    with_reorder_point: int
    with_criticality: int
    with_criticality_from_other_plant: int
    """Of ``withCriticality``, how many tiers came from a plant other than the
    row's own. ZMM065 covers plants 1300 and 1500 only, so a row at 1600, 2000
    or 3000 can only be answered from where the material IS described -- and
    only when it carries the same tier everywhere. Reported so the inferred part
    of the number is visible rather than assumed."""

    with_open_repair: int
    in_material_master: int


class UniverseResponse(I8Model):
    items: list[UniverseItem]
    page: int
    page_size: int
    total: int
    """Rows matching the filters, not rows on this page."""

    meta: UniverseMeta
    reference_date: date


class UniverseDetail(I8Model):
    """One material, every plant it sits in, and its repair history."""

    material: MaterialReference
    plants: list[UniverseItem]
    repair_lines: list[RepairChain]


# --- Register (W5.2) ------------------------------------------------------


class RepairChain(I8Model):
    """One repair PO line. Mirrors the frontend's RepairChain."""

    id: str
    """``{EBELN}-{EBELP}``. The register key, as one string."""

    material: MaterialReference
    plant: PlantReference | None = None

    stock_on_hand: Decimal | None = None
    reorder_point: Decimal | None = None
    qty_under_repair: Decimal = Decimal(0)

    repair_pr: SAPDocumentReference
    repair_po: SAPDocumentReference | None = None

    vendor: str | None = None
    vendor_name: str | None = None

    repair_status: str
    receipt_status: str
    overdue_status: str
    """RECEIVED, NO_DUE_DATE, OVERDUE or ON_TIME. Not in the frontend type yet:
    it is the state the 63 no-due-date lines need to be visible at all."""

    lead_time_status: str = "NO_LEAD_TIME"
    """WITHIN_LEAD_TIME, BEYOND_LEAD_TIME or NO_LEAD_TIME.

    A SECOND, INDEPENDENT signal, not a fallback for ``overdueStatus``. One says
    whether the line passed the date somebody promised on the PO; this says
    whether it has taken longer than the material's planned delivery time. They
    can disagree on the same row and neither overrides the other.
    """

    declaration_status: Literal["Required", "Pending", "Completed", "Flagged"] = (
        "Required"
    )
    """W5.3 owns this. Served as the hook, never computed here."""

    days_open: int | None = None
    aging_bucket: str | None = None

    lead_time_days: int | None = None
    """MARC.PLIFZ for this material at this plant -- planned delivery time in
    calendar days, PO to received. Null where MARC has no row for the pair, or
    the value is unmaintained. The July MARC extract covers plants 1300 and 1200
    only, so this is null on every Gamsberg line."""

    days_elapsed: int | None = None
    """Raised to received, or raised to today while still out. What
    ``leadTimeStatus`` is measured on -- it stops when the unit comes back,
    unlike ``daysOpen``."""

    days_over_lead_time: int | None = None
    """Positive once past the planned time, negative while inside it, null when
    there is no lead time to measure against. Null rather than 0: "nobody told
    us how long this takes" must not average in as "finished exactly on time"."""

    days_at_vendor: int | None = None
    days_in_current_stage: int | None = None
    days_remaining_in_repair: int | None = None
    """Negative once the promised date has passed, as the frontend documents."""

    raised_at: date | None = None
    po_issued_at: date | None = None
    sent_to_vendor_at: date | None = None
    expected_return: date | None = None
    received_at: date | None = None

    ordered_qty: Decimal = Decimal(0)
    received_qty: Decimal = Decimal(0)
    unit: str | None = None
    repair_cost: Decimal | None = None
    """Net order price of the repair line -- what this repair costs."""

    new_unit_lead_time_days: int | None = None

    # --- provenance, so a reader can see how solid each row is ----------
    item_category: str | None = None
    doc_type: str | None = None
    corroborated_by_doc_type: bool = False
    """True when the PO header says ZREP. False includes the 455 lines whose
    header is simply absent -- which is not evidence against them."""

    has_po_header: bool = False
    delivery_completed: bool = False
    """SAP's delivery-complete flag. Reported, not trusted: 1,025 lines carry it
    and only 436 have any goods receipt."""

    reversals: int = 0
    schedule_lines: int = 0


class RegisterMeta(I8Model):
    total_lines: int
    open_lines: int
    received_lines: int
    overdue_lines: int
    no_due_date_lines: int
    lines_without_due_date: int
    lines_with_lead_time: int
    lines_beyond_lead_time: int
    open_lines_beyond_lead_time: int
    partially_received_lines: int
    lines_with_reversals: int
    lines_on_eighty_series: int
    lines_with_po_header: int
    lines_corroborated_by_doc_type: int
    lines_with_dispatch: int
    open_lines_with_dispatch: int
    distinct_materials: int
    distinct_vendors: int
    vendors_resolved_to_a_name: int
    candidates_scanned: int


class RegisterResponse(I8Model):
    items: list[RepairChain]
    page: int
    page_size: int
    total: int
    meta: RegisterMeta
    reference_date: date


class LifecycleStage(I8Model):
    """One step of the repair lifecycle, for the detail timeline."""

    stage: str
    label: str
    occurred_at: date | None = None
    evidence: str
    """Which SAP artefact proves this stage, or why it cannot be proven."""

    days_since: int | None = None


class RepairDetail(I8Model):
    line: RepairChain
    timeline: list[LifecycleStage]


# --- Vendors (W5.2 Layer 4) -----------------------------------------------


class VendorTurnaroundItem(I8Model):
    vendor: str
    vendor_name: str | None = None
    total_lines: int
    open_count: int
    overdue_count: int
    received_count: int

    avg_turnaround_days: float | None = None
    """Completed repairs only. Including open ones makes the slowest vendor
    look fastest, because its repairs have not come back to be counted."""

    min_turnaround_days: int | None = None
    max_turnaround_days: int | None = None
    turnaround_sample: int = 0
    """How many repairs the average came from. A mean of one is not a record."""

    on_time_rate: float | None = None
    avg_days_open: float | None = None


class VendorResponse(I8Model):
    items: list[VendorTurnaroundItem]
    total: int
    reference_date: date
    note: str


# --- Attestation, declarations and exceptions (W5.3) ----------------------


class AttestationRequest(I8Model):
    """The condition-to-repair form, as submitted.

    Note what is NOT here: ``attestor``, ``attestedAt`` and ``sessionId``. The
    first two are set by the server from the authenticated caller and the clock
    -- an audit record whose author and timestamp are the author's to choose is
    not an audit record. The third is always null: FR-8 session linkage is not
    I08's scope.
    """

    material_id: str
    plant: str
    quantity: Decimal
    condition_description: str
    """Free text. The part a human actually reads."""

    fault_category: str
    """Must be one of the configured list -- see ``GET /api/i8/config``. Not a
    free string: the list is VZI's vocabulary and is validated against it."""

    recommendation: Literal["REPAIRABLE", "BEYOND_ECONOMICAL_REPAIR", "SCRAP"]

    serial_number: str | None = None
    """Optional. I08 works at material-plant grain today; capturing a serial
    when somebody knows it costs nothing now and is unrecoverable later."""

    evidence_reference: str | None = None
    """**A reference string only.** File upload is descoped and SharePoint is
    not provisioned, so this holds a pointer somebody can follow -- the platform
    does not pretend to store the artefact."""

    supersedes: str | None = None
    """The attestation this one amends. Attestations are never edited: an
    amendment is a new record pointing at the one it replaces, and the original
    stays readable. Must be for the same material and plant."""


class Attestation(I8Model):
    """One recorded attestation."""

    id: str
    material: MaterialReference
    plant: PlantReference
    quantity: Decimal
    condition_description: str
    fault_category: str
    recommendation: str
    condition: str
    """The same judgement in the frontend's DeclarationCondition wording --
    "Repairable" / "Beyond Economical Repair" / "Scrap" -- so the UI does not
    have to carry a second mapping."""

    serial_number: str | None = None
    evidence_reference: str | None = None
    attestor: str
    attested_at: datetime
    session_id: str | None = None
    """Always null. FR-8 session linkage is not in I08's scope."""

    supersedes: str | None = None
    superseded_by: str | None = None
    """Set when a later amendment replaces this one. The original is never
    removed or edited -- this is how a reader knows it is not current."""

    # --- What this attestation actually covers ------------------------------
    #
    # Populated on POST only, and null on GET, where the caller is reading
    # history rather than asking "did the thing I just did land?".
    #
    # These exist because of a real and non-obvious trap. An attestation's
    # timestamp is server-set -- that is what makes it an audit record -- and
    # the extract is a frozen July-2026 snapshot whose repair lines were raised
    # from April 2025. So an attestation recorded TODAY is months outside the
    # matching window of every line in the register, and covers none of them.
    #
    # Both halves of that are correct and neither should change. But a UI that
    # POSTs successfully and then shows the row still reading "Required" looks
    # broken, and somebody would "fix" it by widening the window until it
    # stopped looking broken -- which would let an assessment from a completely
    # different repair cycle count. So the API says plainly what happened.

    covers_repair_lines: list[str] | None = None
    """Register line ids (``{EBELN}-{EBELP}``) this attestation now covers.
    Empty list means it was recorded and covers nothing."""

    coverage_note: str | None = None
    """Plain-language explanation of ``coversRepairLines``, including WHY it is
    empty when it is. Written to be shown to a user, not logged."""

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None


class AttestationResponse(I8Model):
    items: list[Attestation]
    total: int
    fault_categories: list[str]
    """The configured controlled list, served with the data so a form does not
    have to hard-code it."""


class DeclarationItem(I8Model):
    """One row of the condition-to-repair declaration queue.

    Field names match ``DeclarationItem`` in
    ``src/features/initiative-8/types/repair.ts`` exactly.
    """

    id: str
    pr: SAPDocumentReference | None = None
    """Null where the repair line carries no requisition number."""

    material: MaterialReference
    plant: PlantReference | None = None

    requester: str | None = None
    """EKPO.AFNAM, and a CODE rather than a name -- no person directory was
    delivered. Displayed as a code, the same way an unnamed vendor is."""

    source: Literal["Manual", "MRP-generated"] | None = None
    """**Null on every row today, and that is the honest answer.**

    A fourth deliberate departure from the frontend type. EBAN carries the
    creation indicator that would decide this and covers only 521 of the 1,201
    repair requisitions; every one of those 521 reads ``F`` (created from an
    order), which is neither "Manual" nor "MRP-generated". Both labels are false
    for every row we can see, so neither is sent."""

    has_active_repair: bool
    related_repair_id: str
    status: Literal["Required", "Pending", "Completed", "Flagged"]
    """"Pending" is never emitted: it means "submitted, awaiting sign-off" and
    no such state exists -- there is no approval workflow in SAP or here."""

    declared_by: str | None = None
    declared_at: datetime | None = None
    condition: Literal["Repairable", "Beyond Economical Repair", "Scrap"] | None = None
    next_action: str
    created_at: date | None = None


class DeclarationMeta(I8Model):
    total: int
    by_status: dict[str, int]
    outstanding: int
    """Required + Flagged -- the rows that want somebody's attention."""

    attestation_window_days: int
    """The matching rule that produced these statuses. Never quote a count
    without its source."""


class DeclarationResponse(I8Model):
    items: list[DeclarationItem]
    page: int
    page_size: int
    total: int
    meta: DeclarationMeta
    reference_date: date


class ExceptionQueueItem(I8Model):
    """One exception. No matching frontend type exists yet -- this defines it,
    in the same camelCase house style as everything else in /api/i8."""

    id: str
    type: str
    severity: Literal["info", "warning", "critical"]
    material: MaterialReference
    plant: PlantReference | None = None
    repair_line: SAPDocumentReference
    title: str
    detail: str
    """Says what is missing AND what was searched for, so a reader can tell a
    real gap from a matching rule that did not fit."""

    raised_at: date | None = None
    """The repair line's own date, not the moment the check ran."""

    is_open_repair: bool

    pre_automation: bool = False
    """The line predates the attestation control, so the gap is explained by
    when it was raised rather than by anyone failing to act. Always false while
    no cutover date is configured."""


class ExceptionMeta(I8Model):
    total: int
    by_type: dict[str, int]
    by_severity: dict[str, int]
    lines_checked: int
    lines_covered: int
    attestation_window_days: int

    pre_automation: int = 0
    actionable: int = 0
    """``total`` less ``preAutomation`` -- the work, where ``total`` is the
    business case. Both are served because quoting either alone misleads."""

    attestation_cutover_date: date | None = None
    """The cutover these counts were measured against. Null means none is
    configured, which is the current state."""

    types_raised: list[str]
    """Which exception types I08 actually raises. MISSING_SESSION_ID and
    UNJUSTIFIED_ACQUISITION are FR-5/7/8 and are declared but never raised here,
    so a caller can tell an empty count from an unimplemented check."""


class ExceptionResponse(I8Model):
    items: list[ExceptionQueueItem]
    page: int
    page_size: int
    total: int
    meta: ExceptionMeta
    reference_date: date


# --- Coding candidates (W5.5) ---------------------------------------------


class CodingCandidateLine(I8Model):
    """One PO line whose free text mentioned repair."""

    purchasing_document: str
    item: str
    plant: PlantReference | None = None
    short_text: str
    """**The text the verdict was reached on.** Served with every candidate so a
    cataloguer can check the call without going back to SAP. A flag nobody can
    audit is a flag nobody will act on."""

    matched_keywords: list[str]
    """Which configured keywords this line hit, so "why was this picked up?"
    needs no explanation."""

    raised_at: date | None = None
    item_category: str | None = None


class CodingCandidateTwin(I8Model):
    """An 80-series material carrying the SAME text as this candidate.

    The strongest evidence the screen produces, and it owes nothing to the
    model: the identical description, on the same physical part, coded both
    ways in one extract. Where this is present the finding is an observation
    about SAP rather than a language judgement.
    """

    material_id: str
    shared_text: str


class CodingCandidateItem(I8Model):
    """One material the screen judged."""

    material_id: str
    verdict: str
    """MISCODED_REPAIRABLE / REPAIR_SERVICE / CONSUMABLE_FOR_REPAIR / UNCLEAR /
    UNSCREENED. UNSCREENED means no model answered -- never "not a candidate"."""

    confidence: str
    """high, medium or low, as the model reported it. Empty when unscreened."""

    reason: str
    """Why, in the model's own words."""

    plants: list[str]
    lines: list[CodingCandidateLine]
    distinct_texts: list[str]

    twins: list[CodingCandidateTwin] = []
    is_corroborated: bool = False
    is_actionable: bool = False
    """MISCODED_REPAIRABLE or UNCLEAR -- the ones a human should look at."""

    meets_confidence_threshold: bool = False
    """Whether the model's own confidence clears the configured bar
    (``I8Settings.coding_candidate_confidence_threshold``). A sibling flag to
    ``is_actionable``, not a replacement -- they answer different questions,
    and a below-threshold candidate is still returned here, never dropped."""

    in_repairable_universe: bool = False
    """Cross-checked against W5.1. Expected false on every candidate: a material
    already in the universe is coded correctly whatever its text says. A true
    here means the two rules disagree and something is wrong."""

    model: str = ""
    provider: str = ""
    """WHO answered -- ``stub`` means nothing was really judged. Deliberately
    separate from ``model``, which is only the deployment the registry routed
    to and which the stub echoes straight back."""

    prompt_version: int | None = None
    screened_at: datetime | None = None


class CodingCandidateMeta(I8Model):
    """Every count, with the thing it counts named."""

    lines_with_text: int
    lines_with_repair_language: int
    lines_already_eighty_series: int
    """Repair language on a material that IS coded repairable -- the convention
    worked. Measured at 6."""

    lines_without_material: int
    """Free-text service purchases with no material number at all. **Measured at
    183, which is 60% of the matches.** They cannot be mis-coded because there is
    nothing to re-code, so they are never candidates -- but they are repair spend
    happening entirely outside the material master, which is its own finding."""

    lines_screened: int
    materials_found: int
    materials_screened: int
    was_truncated: bool
    """True when a limit stopped the run short. Reported rather than implied: a
    silent cap reads as "we checked everything" when it did not."""

    corroborated: int
    by_verdict: dict[str, int]
    keywords: list[str]
    provider: str
    model: str


class CodingCandidateResponse(I8Model):
    items: list[CodingCandidateItem]
    total: int
    meta: CodingCandidateMeta


# --- Diagnostics ----------------------------------------------------------


class SnapshotInfo(I8Model):
    reference_date: date
    built_at: datetime
    build_seconds: float
    # Not named `register`: that shadows BaseModel.register and pydantic
    # warns about it at import time.
    repair_register: RegisterMeta
    universe: UniverseMeta
    rules: dict[str, str | int]
    """The configuration actually in force, echoed back so a surprising number
    can be traced to a setting without reading the deployment."""


UniverseDetail.model_rebuild()
