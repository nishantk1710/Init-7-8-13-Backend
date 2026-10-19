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
    The register does not serve it yet. MBEW is now in I08's table set
    (``v_mbew``), but only the coding screen reads it so far, as each
    candidate's ``newItemPrices`` -- see ``app.initiatives.i8.new_item_price``.
    Until the register does, the field is left out rather than sent as zero,
    which would make every repair look infinitely worth doing.

``repairPR`` needed no departure: EKPO carries a requisition number on all
1,225 repair lines, so it is served as the non-optional reference the frontend
already expects.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
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
    """Null for Gamsberg (1500) -- the July MARC extract has no rows for it."""

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
    row's own. ZMM065 covers both in-scope plants, but not every material is
    described at both, so a row can only be answered from where the material IS
    described -- and only when it carries the same tier everywhere. Reported so
    the inferred part of the number is visible rather than assumed."""

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


class RepairJustificationEntry(I8Model):
    """One NEW_ACQUISITION justification recorded while the line was out."""

    id: str | None = None
    reason_category: str | None = None
    free_text: str | None = None
    author: str | None = None
    recorded_at: datetime | None = None
    session_id: str | None = None
    """The assistant session it was recorded in -- the link to the
    conversation."""


class UnjustifiedPurchase(I8Model):
    """A new unit bought while the line was out, with no reason on record.

    The same finding as the ``UNJUSTIFIED_ACQUISITION`` exception, which
    ``exception_id`` names.
    """

    exception_id: str
    purchase: SAPDocumentReference
    raised_at: date | None = None
    pre_automation: bool = False
    """Bought before the justification control existed -- nobody was asked."""


class RepairJustification(I8Model):
    """The register's Justification cell for one repair line."""

    status: Literal["RECORDED", "MISSING"]
    """MISSING whenever any purchase overlapping the line has no reason, even
    if another one does: that is the one somebody has to act on."""

    entries: list[RepairJustificationEntry]
    """Newest first. Empty when the line is only MISSING."""

    unjustified_purchases: list[UnjustifiedPurchase]


class LineException(I8Model):
    """One exception raised against a register line: enough for the register's
    Exceptions column to name it and link to it in the queue."""

    id: str
    type: str
    severity: Literal["info", "warning", "critical"]


class RepairChain(I8Model):
    """One repair PO line SAP still has open. Mirrors the frontend's RepairChain.

    Since 19-Oct-2026 the register serves no other kind: a line SAP has closed
    ("Delivery completed"), deleted or blocked is not in it, and goods receipts
    are not read -- so there is no receipt status, receipt date or received
    quantity here."""

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
    """PO Issued, or At Vendor once a dispatch (the 541) is attributable."""

    overdue_status: str
    """NO_DUE_DATE, OVERDUE or ON_TIME. NO_DUE_DATE is the state the lines with
    no schedule line need to be visible at all."""

    lead_time_status: str = "NO_LEAD_TIME"
    """WITHIN_LEAD_TIME, BEYOND_LEAD_TIME or NO_LEAD_TIME.

    A SECOND, INDEPENDENT signal, not a fallback for ``overdueStatus``. One says
    whether the line passed the date somebody promised on the PO; this says
    whether it has taken longer than the material's planned delivery time. They
    can disagree on the same row and neither overrides the other.
    """

    declaration_status: Literal["Required", "Completed", "Flagged"] = (
        "Required"
    )
    """W5.3 owns this. Served as the hook, never computed here."""

    # --- the declaration, in full -------------------------------------
    # The Declaration Queue screen was folded into the register on
    # 08-Oct-2026, so what it showed per row now travels on the row itself.
    # Same values as ``DeclarationItem``, from the same attestation view.

    declaration_id: str | None = None
    """The attestation covering this line, when one does -- so a screen can
    offer to amend it rather than only to add another."""

    declared_by: str | None = None
    declared_at: datetime | None = None
    condition: Literal["Repairable", "Beyond Economical Repair", "Scrap"] | None = None
    """What the attestation concluded. None when there is no attestation."""

    next_action: str | None = None
    """What a person should do about this line's declaration, as a sentence."""

    requester: str | None = None
    """EKPO.AFNAM, a code rather than a name -- no person directory was
    delivered."""

    justification: RepairJustification | None = None
    """FR-7 on this line: a NEW_ACQUISITION reason recorded while it was out,
    or a new purchase that overlapped it with none. None when neither --
    see ``app.initiatives.i8.line_justifications``."""

    exceptions: list[LineException] | None = None
    """What the checks raise against this line, for the register's Exceptions
    column. Served by the register listing, so the screen need not load the
    whole exception queue to fill one column of one page. An empty list means
    the checks ran and raised nothing; null means not computed here -- the
    line's detail endpoint, which links to the queue instead."""

    days_open: int | None = None
    """Raised to today -- the same number as ``daysElapsed``. The line is open
    in SAP, so it keeps ageing until SAP closes it."""
    aging_bucket: str | None = None
    """The band ``daysOpen`` falls in."""

    lead_time_days: int | None = None
    """MARC.PLIFZ for this material at this plant -- planned delivery time in
    calendar days, PO to received. Null where MARC has no row for the pair, or
    the value is unmaintained. The July MARC extract has no Gamsberg rows, so
    this is null on every Gamsberg line."""

    days_elapsed: int | None = None
    """Raised to today. What ``leadTimeStatus`` is measured on."""

    days_over_lead_time: int | None = None
    """Positive once past the planned time, negative while inside it, null when
    there is no lead time to measure against. Null rather than 0: "nobody told
    us how long this takes" must not average in as "finished exactly on time"."""

    days_in_current_stage: int | None = None
    days_remaining_in_repair: int | None = None
    """Negative once the promised date has passed, as the frontend documents."""

    raised_at: date | None = None
    po_issued_at: date | None = None
    sent_to_vendor_at: date | None = None
    expected_return: date | None = None

    ordered_qty: Decimal = Decimal(0)
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
    schedule_lines: int = 0

    criticality: str | None = None
    """The material's criticality at this plant, from the shared W3.4 source
    via the universe. Null where no source has a tier for the pair -- unknown,
    not NORMAL."""


class RegisterMeta(I8Model):
    """The register's counts. Every line counted is open in SAP; the
    ``excluded*`` figures say how many repair lines SAP has deleted, closed or
    blocked, each counted under the first of the three that applies."""

    total_lines: int
    open_lines: int
    """The same number as ``totalLines``: every listed line is open in SAP."""

    overdue_lines: int
    no_due_date_lines: int
    lines_with_lead_time: int
    lines_beyond_lead_time: int
    lines_on_eighty_series: int
    lines_with_po_header: int
    lines_corroborated_by_doc_type: int
    lines_with_dispatch: int
    distinct_materials: int
    distinct_vendors: int
    vendors_resolved_to_a_name: int
    candidates_scanned: int
    excluded_deleted_lines: int = 0
    """Repair lines deleted in SAP (EKPO.LOEKZ)."""

    excluded_delivery_completed_lines: int = 0
    """Repair lines SAP has closed -- "Delivery completed", EKPO.ELIKZ --
    whatever was received against them."""

    excluded_blocked_lines: int = 0
    """Repair lines blocked in SAP (EKPO.LOEKZ)."""


class VendorOption(I8Model):
    """One vendor a register or queue can be narrowed to."""

    vendor: str
    """The code, which is what the ``vendor`` filter takes."""

    name: str | None = None
    """LFA1's name, when one was delivered. A dropdown shows it and falls back
    to the code -- the rule the Vendor columns follow."""


class RegisterFacets(I8Model):
    """The values the register's filters can take, for a paged screen's dropdowns.

    Taken before every filter, so a dropdown always offers every value it can
    be switched to, rather than those on one page.
    """

    plants: list[PlantReference]
    vendors: list[VendorOption]
    has_unknown_vendor: bool
    """Some lines carry no vendor (no PO header). ``vendor=none`` finds them."""

    repair_statuses: list[str]
    criticalities: list[str]
    has_unrated: bool
    """Some lines' material has no criticality at their plant.
    ``criticality=none`` finds them."""


class RegisterResponse(I8Model):
    items: list[RepairChain]
    page: int
    page_size: int
    total: int
    meta: RegisterMeta
    facets: RegisterFacets
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


# --- Attestation, declarations and exceptions (W5.3) ----------------------


class AttestationRequest(I8Model):
    """The condition-to-repair form, as submitted.

    Note what is NOT here: ``attestor``, ``attestedAt`` and ``sessionId``. The
    first two are set by the server from the authenticated caller and the clock
    -- an audit record whose author and timestamp are the author's to choose is
    not an audit record. The third is always null for now: linking an
    attestation to its session is FR-4 / FR-8 and ours, but nothing supplies the
    id until the reservation carries it (RESB.BEDNR, not yet exposed).
    """

    # Lengths are the column widths in app/initiatives/i8/models.py. Without
    # them an overlong value reaches the database and comes back as a 500
    # rather than as a 422 that says which field was wrong.
    material_id: str = Field(max_length=40)
    plant: str = Field(max_length=8)
    quantity: Decimal
    condition_description: str
    """Free text. The part a human actually reads."""

    fault_category: str = Field(max_length=64)
    """Must be one of the configured list -- see ``GET /api/i8/config``. Not a
    free string: the list is VZI's vocabulary and is validated against it."""

    recommendation: Literal["REPAIRABLE", "BEYOND_ECONOMICAL_REPAIR", "SCRAP"]

    serial_number: str | None = Field(default=None, max_length=64)
    """Optional. I08 works at material-plant grain today; capturing a serial
    when somebody knows it costs nothing now and is unrecoverable later."""

    evidence_reference: str | None = Field(default=None, max_length=500)
    """**A reference string only.** File upload is descoped and SharePoint is
    not provisioned, so this holds a pointer somebody can follow -- the platform
    does not pretend to store the artefact."""

    supersedes: str | None = Field(default=None, max_length=32)
    """The attestation this one amends. Attestations are never edited: an
    amendment is a new record pointing at the one it replaces, and the original
    stays readable. Must be for the same material and plant."""

    repair_document: str | None = Field(default=None, max_length=20)
    """EKPO.EBELN of the repair line being declared against.

    FR-4's "link ... to the repair PR or PO". Send it whenever the screen knows
    which line it is declaring -- the register, the repair detail page and the
    declarations queue all do -- and the line is then covered outright, with no
    date matching involved.

    Omit it when assessing a part before anything has been raised for it.
    There is no purchase order to name in that moment, which is exactly why the
    date window exists, and it stays the fallback for those."""

    repair_item: str | None = Field(default=None, max_length=10)
    """EKPO.EBELP of that line. Send with ``repairDocument`` or not at all --
    half a key identifies no line and is rejected with a 422."""


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

    repair_document: str | None = None
    """The repair line this was declared against (EKPO.EBELN), or null for a
    declaration recorded before any repair was raised."""

    repair_item: str | None = None
    """EKPO.EBELP of that line."""

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
    # the deployed register holds years of purchasing history, with open lines
    # raised between 2018 and 2026. An UNBOUND attestation recorded today is
    # therefore outside the matching window of nearly every line, and covers
    # none of them: measured 13-Oct-2026, 5,155 of 5,155 lines read Required.
    #
    # Both halves of that are correct and neither should change. But a UI that
    # POSTs successfully and then shows the row still reading "Required" looks
    # broken, and somebody would "fix" it by widening the window until it
    # stopped looking broken -- which would let an assessment from a completely
    # different repair cycle count. So the API says plainly what happened.
    #
    # Sending `repairDocument`/`repairItem` avoids the trap rather than
    # explaining it: the line is named, so it is covered.

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

    related_repair_id: str
    vendor: str | None = None
    """The vendor the part went to, as a code. Null where the repair line has
    no PO header."""

    vendor_name: str | None = None
    """LFA1's name for that vendor, when one was delivered. The queue shows the
    name when it has one and the code when it does not."""

    repair_status: str | None = None
    """Where the REPAIR stands (PO Issued / At Vendor). Not the declaration
    status, which is ``status``."""

    repair_po: SAPDocumentReference | None = None
    """This row's repair line, unjoined.

    ``relatedRepairId`` is the same thing as one string. This is served
    alongside it so the declaration form can send FR-4's link to the repair PO
    as data rather than splitting that string on a hyphen."""
    status: Literal["Required", "Completed", "Flagged"]
    """No "Pending": that would mean "submitted, awaiting sign-off", and no such
    state exists -- there is no approval workflow in SAP, here or in the FRS."""

    declaration_id: str | None = None
    """The attestation covering this line, when one does -- so the queue can
    offer to amend it rather than only to add another."""

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


class DeclarationFacets(I8Model):
    """The values the queue's filters can take, for a paged screen's dropdowns.

    A paged screen holds one page of rows, so it cannot build its dropdowns
    from them. These are taken over the rows being listed -- after ``status``
    and ``outstandingOnly``, which choose what kind of row that is -- and before
    ``plant``, ``vendor``, ``repairStatus`` and ``q``, so choosing one plant does
    not take the other out of the list.
    """

    plants: list[PlantReference]
    vendors: list[VendorOption]
    repair_statuses: list[str]


class DeclarationResponse(I8Model):
    items: list[DeclarationItem]
    page: int
    page_size: int
    total: int
    meta: DeclarationMeta
    facets: DeclarationFacets
    reference_date: date


class ExceptionHistoryEntry(I8Model):
    """One thing that happened to an exception: an assignment, a comment, a
    status change, or the queue resolving the item itself."""

    id: str
    kind: Literal["ASSIGNED", "COMMENT", "STATUS", "AUTO_RESOLVED"]
    """``AUTO_RESOLVED`` is kept apart from ``STATUS`` deliberately. "Priya
    resolved this" and "the check stopped finding it" are different claims,
    and rendering them identically invites the first to be read where only the
    second happened."""

    status: str | None = None
    """The status this entry set. Null on a comment or an assignment."""

    owner: str | None = None
    """Who the item was assigned to, on an ``ASSIGNED`` entry. Null there means
    it was unassigned -- ``kind`` tells the two nulls apart."""

    actor: str
    """``system`` on ``AUTO_RESOLVED``; otherwise whoever acted. Server-set,
    never taken from the request body."""

    text: str = ""
    at: datetime


class ExceptionQueueItem(I8Model):
    """One exception. No matching frontend type exists yet -- this defines it,
    in the same camelCase house style as everything else in /api/i8."""

    id: str
    type: str
    severity: Literal["info", "warning", "critical"]
    material: MaterialReference
    plant: PlantReference | None = None
    repair_line: SAPDocumentReference
    """The repair line the exception is about. For ``UNJUSTIFIED_ACQUISITION``
    it is the repair that was open when the new unit was bought."""

    acquisition_line: SAPDocumentReference | None = None
    """``UNJUSTIFIED_ACQUISITION`` only: the purchase line that bought a new
    unit while ``repairLine`` was still out. Null for every other type."""

    title: str
    detail: str
    """Says what is missing AND what was searched for, so a reader can tell a
    real gap from a matching rule that did not fit."""

    raised_at: date | None = None
    """The date of the line the exception is about -- the repair line, or for
    an acquisition the purchase line -- not the moment the check ran."""

    pre_automation: bool = False
    """The line predates the control it is measured against, so the gap is
    explained by when it was raised rather than by anyone failing to act.
    Always false while no cutover date is configured."""

    # --- FR-11: the state people put it in ------------------------------
    #
    # The item above is recomputed on every read; everything below is stored
    # against its id in i8_exception_state. An item nobody has touched has no
    # row there and reports the defaults -- OPEN and unassigned -- which is
    # the truth about it rather than a placeholder.

    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"] = "OPEN"

    owner: str | None = None
    """Null means unassigned, which is a real state and the common one."""

    still_detected: bool = True
    """Whether the check still finds this item.

    Always true for an item in the list -- being listed is what detection
    means. It is served because a RESOLVED item may be listed when the caller
    asks for one by id or by status, and then the distinction between "someone
    resolved it" and "the data moved" is the whole story."""

    history: list[ExceptionHistoryEntry] = []
    """Oldest first, the order a history is read in. Empty for an item nobody
    has touched."""


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
    """Which exception types I08 actually raises, so a caller can tell an empty
    count from an unimplemented check. ``MISSING_SESSION_ID`` is declared and
    not raised: it needs the session id read back off the reservation, and
    RESB.BEDNR is not exposed on ReservationItemSet yet."""

    acquisitions_checked: int = 0
    """New 80-series purchase lines the UNJUSTIFIED_ACQUISITION check ran over."""

    justification_window_days: int = 0
    """How far from a purchase a NEW_ACQUISITION justification may sit and
    still cover it."""

    justification_cutover_date: date | None = None
    """The cutover the acquisition counts were measured against. Null means
    none is configured."""


class ExceptionFacets(I8Model):
    """The values the queue's filters can take, for a paged screen's dropdowns.

    Taken over every item the queue carries -- after ``excludeType``, which
    takes a check out of the queue altogether -- and before any filter, so a
    dropdown always offers every value it can be switched to.
    """

    plants: list[PlantReference]
    types: list[str]
    """The types present among the items. ``meta.typesRaised`` says which checks
    run; this says which of them found something."""


class ExceptionResponse(I8Model):
    items: list[ExceptionQueueItem]
    page: int
    page_size: int
    total: int
    meta: ExceptionMeta
    facets: ExceptionFacets
    reference_date: date


# --- Coding candidates (W5.5) ---------------------------------------------


class CodingCandidateLine(I8Model):
    """One document line whose free text mentioned repair."""

    source: str
    """PO, PR or MOVEMENT -- which free-text field the words came from.

    Served because ``document``/``documentItem`` alone are ambiguous: a
    purchase order item and a requisition item look alike and are opened in
    different SAP transactions."""

    document: str
    """EBELN for a PO, BANFN for a PR, MBLNR for a movement."""

    document_item: str
    """EBELP, BNFPO or ZEILE. Not called ``item``: the three number
    differently, and a bare ``item`` reads as a PO item everywhere else."""

    plant: PlantReference | None = None
    short_text: str
    """**The text the verdict was reached on.** Served with every candidate so a
    cataloguer can check the call without going back to SAP. A flag nobody can
    audit is a flag nobody will act on."""

    matched_keywords: list[str]
    """Which configured keywords this line hit, so "why was this picked up?"
    needs no explanation."""

    raised_at: date | None = None
    """ERDAT on a PO line, BADAT on a requisition, BUDAT on a movement --
    when the words were written, whichever document carries them."""

    item_category: str | None = None
    """PSTYP on a PO or PR line. Always null on a movement, which has none."""


class CodingCandidateTwin(I8Model):
    """An 80-series material carrying the SAME text as this candidate.

    The strongest evidence the screen produces, and it owes nothing to the
    model: the identical description, on the same physical part, coded both
    ways in one extract. Where this is present the finding is an observation
    about SAP rather than a language judgement.
    """

    material_id: str
    shared_text: str


class CodingCandidatePrice(I8Model):
    """What the material costs new at one plant: MBEW's moving average price.

    One per plant the candidate's lines are from. Never a zero standing in for
    an unknown -- a missing price is null, with ``missing`` saying why.
    """

    plant: str
    price: Decimal | None = None
    """VERPR / PEINH, in ZAR. Null whenever ``missing`` is set."""

    valuation_type: str | None = None
    """``NEW`` when the material is split-valuated at this plant (the NEW row
    was read); null when it is not split and its one row was read."""

    missing: str | None = None
    """Why there is no price: ``TOKEN_VALUE`` (MBEW holds a placeholder such
    as R1.00), ``NO_NEW_VALUATION`` (split-valuated with no NEW row) or
    ``NOT_IN_MBEW`` (no row at this plant). Null when there is a price."""

    mbew_value: Decimal | None = None
    """The value on the MBEW row read -- on ``TOKEN_VALUE``, the placeholder,
    so the screen can quote it."""


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

    new_item_prices: list[CodingCandidatePrice] = []
    """What the part costs new, one entry per plant in ``plants``."""


class CodingCandidateMeta(I8Model):
    """Every count, with the thing it counts named."""

    sources: list[str]
    """Which free-text sources this run read -- PO, PR, MOVEMENT. Every count
    below is relative to it, so two runs cannot be compared without it. Set by
    ``I8_CODING_CANDIDATE_SOURCES``; MOVEMENT is off until measured."""

    lines_with_text: int
    """Lines carrying any free text across those sources -- the denominator."""

    lines_with_text_by_source: dict[str, int] = {}
    lines_with_repair_language: int
    lines_by_source: dict[str, int] = {}
    """Repair-language hits per source. The number to read when deciding
    whether a source earns its keep."""

    deleted_requisitions_dropped: int = 0
    """PR lines ignored because LOEKZ is set -- a deleted requisition is not a
    request for anything."""

    converted_requisitions_dropped: int = 0
    """PR lines ignored because the PO they became is in this same run. One
    request, counted once; the PO is kept because it is the document that
    exists in SAP now."""

    verdicts_reused: int = 0
    """Verdicts served from the store rather than bought from the model again,
    because the material's text has not changed since it was judged."""

    lines_already_eighty_series: int
    """Repair language on a material that IS coded repairable -- the convention
    worked. Measured at 6."""

    lines_without_material: int
    """Free-text service purchases with no material number at all. **Measured at
    174, which is 59% of the matches.** They cannot be mis-coded because there is
    nothing to re-code, so they are never candidates -- but they are repair spend
    happening entirely outside the material master, which is its own finding."""

    lines_screened: int
    materials_found: int
    materials_screened: int
    was_truncated: bool
    """True when a limit stopped the run short. Reported rather than implied: a
    silent cap reads as "we checked everything" when it did not."""

    corroborated: int
    new_item_price_counts: dict[str, int] = {}
    """Material-plants by price state over the whole screen: ``PRICED``,
    ``TOKEN_VALUE``, ``NO_NEW_VALUATION``, ``NOT_IN_MBEW``, or
    ``LOOKUP_FAILED`` on every one when the MBEW read failed or timed out."""

    by_verdict: dict[str, int]
    keywords: list[str]
    provider: str
    model: str

    staged_rows_kept: dict[str, int] = {}
    """Rows each staged copy of a view kept, per view. Empty when the screen
    read the views directly."""

    staged_rows_scanned: dict[str, int] = {}
    """Rows each staged copy was narrowed FROM. Read with ``stagedRowsKept``:
    the pair is how selective the keyword narrowing actually is, and the whole
    screen's speed depends on the first being a small fraction of the second."""


class CodingCandidateResponse(I8Model):
    items: list[CodingCandidateItem]
    total: int
    meta: CodingCandidateMeta


# --- Diagnostics ----------------------------------------------------------


class SnapshotInfo(I8Model):
    reference_date: date
    built_at: datetime
    build_seconds: float
    source_loaded_at: datetime | None = None
    """When the raw tables behind this snapshot were last loaded -- the newest
    successful ``ingestion_run`` across ``i8.service.SOURCE_TABLES``.

    Served because the UI had no way to tell one source from another and said
    so wrongly: its caption named the July extract in static text, and went on
    naming it after a CSV full pull had replaced every table underneath. A
    screen may report how fresh its data is; it may not assert where it came
    from. None when nothing has ever recorded a load."""

    # Not named `register`: that shadows BaseModel.register and pydantic
    # warns about it at import time.
    repair_register: RegisterMeta
    universe: UniverseMeta
    rules: dict[str, str | int]
    """The configuration actually in force, echoed back so a surprising number
    can be traced to a setting without reading the deployment."""


UniverseDetail.model_rebuild()


# --- W7.2 / FR-6: does a repairable unit already exist? --------------------


class RepairEvidenceItem(I8Model):
    """One open repair, as the assistant is allowed to describe it.

    ``dispatched`` is served rather than assumed: zero of the 788 open repair
    lines in this extract carry a dispatch movement, so "a repair is open" must
    not be rendered as "the vendor has it".
    """

    purchasing_document: str
    item: str
    quantity: Decimal
    raised_at: date | None = None
    due_date: date | None = None
    days_overdue: int | None = None
    vendor: str | None = None
    vendor_name: str | None = None
    status: str
    dispatched: bool


class RepairableUnitResponse(I8Model):
    """FR-6's answer, with the evidence behind it.

    ``stockOnHand`` is nullable and null is NOT zero -- it means no MARD row
    exists for this material at this plant. ``stockIsUnknown`` is served
    alongside it so the UI never has to infer that from a missing number.
    """

    material_id: str
    plant: str | None = None
    is_repairable_material: bool
    exists: bool
    sources: list[str]
    stock_on_hand: Decimal | None = None
    stock_is_unknown: bool
    stock_locations: int
    open_repair_lines: int
    quantity_under_repair: Decimal
    soonest_due_date: date | None = None
    overdue_lines: int
    headline: str
    caveats: list[str]
    evidence: list[RepairEvidenceItem]
    reference_date: date


class ExceptionEventRequest(I8Model):
    """Append one entry to an exception's history (FR-11).

    One request shape for all three actions, because they are one operation on
    one append-only table. There is no PATCH anywhere in this application: a
    status is not a column to be edited, it is the last entry that set one.

    ====================  ============================================
    ``kind``              what else to send
    ====================  ============================================
    ``ASSIGNED``          ``owner`` -- or null to unassign, which is
                          an action and gets its own entry
    ``STATUS``            ``status``, and ideally ``text`` saying why
    ``COMMENT``           ``text``
    ====================  ============================================

    ``AUTO_RESOLVED`` is not accepted: only the queue writes those, because
    only a check can know it has stopped finding something.
    """

    kind: Literal["ASSIGNED", "STATUS", "COMMENT"]
    status: Literal["OPEN", "IN_PROGRESS", "RESOLVED", "CLOSED"] | None = None
    owner: str | None = Field(default=None, max_length=128)
    text: str = Field(default="", max_length=2000)
    """What was said, or why the status moved. Optional on a status change and
    worth sending: a history of bare transitions explains nothing later."""
