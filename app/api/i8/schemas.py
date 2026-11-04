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

    usable_stock: Decimal | None = None
    """Units in a usable condition (new, refurbished) at this plant (28-Oct-2026).
    Null when the part has no condition rows: unknown, never zero."""

    damaged_stock: Decimal | None = None
    """Units in a damaged condition at this plant. Null when unknown."""


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


class RepairChain(I8Model):
    """One repair PO line SAP still has open. Mirrors the frontend's RepairChain.

    Since 19-Oct-2026 the register serves no other kind: a line SAP has closed
    ("Delivery completed"), deleted or blocked is not in it, and goods receipts
    are not read -- so there is no receipt status, receipt date or received
    quantity here.

    Since 27-Oct-2026 the register can also serve an open repair PR with no PO
    yet (``lineKind`` ``PR``), behind ``I8_REPAIR_REQUESTS_ENABLED``."""

    id: str
    """``{EBELN}-{EBELP}``, or ``PR-{BANFN}-{BNFPO}`` on a PR row. The register
    key, as one string."""

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

    declaration_status: Literal["Required", "Completed", "Flagged", "Historical"] = (
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
    """Net order price of the repair line (EKPO.NETPR) -- what this repair
    costs, in ``repairCostCurrency``. Null when the line has no price, a zero,
    or a placeholder (then ``repairCostPlaceholder`` carries it)."""
    repair_cost_currency: str | None = None
    """EKKO.WAERS, the currency of ``repairCost`` (``ZAR``, ``USD``): a PO is
    priced in the vendor's currency and nothing here converts it. Null when the
    PO has no header or no currency -- unknown, not rand."""
    repair_cost_placeholder: Decimal | None = None
    """The PO's net price when it is a placeholder (above zero, at or below
    ``I8_REPAIR_COST_TOKEN_MAX``, R1.00), which ``repairCost`` then leaves
    null. Null otherwise."""

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

    # --- repair PRs with no PO yet (27-Oct-2026) ------------------------

    line_kind: Literal["PO", "PR"] = "PO"
    """``PR`` marks an open repair PR with no PO yet, served only while
    ``I8_REPAIR_REQUESTS_ENABLED`` is on. Its id is ``PR-{BANFN}-{BNFPO}``,
    ``repairPo`` and the vendor are null, ``repairStatus`` is ``PR Raised`` and
    ``overdueStatus`` ``NOT_ORDERED``; days count from the PR date."""

    requester_name: str | None = None
    """The department name for ``requester``, when the department list has the
    code. Null when it does not -- the code is shown alone, never a guess.
    On a PO row ``requester`` is the department of the repair PR it was
    ordered from (27-Oct-2026)."""

    declaration_link: Literal["BOUND", "REFERENCE", "WINDOW"] | None = None
    """How the covering declaration was matched: it names this line or its
    repair PR (``BOUND``), the R-reference on the repair order
    (``REFERENCE``), or material + plant + date window (``WINDOW``). Null when
    nothing covers the row."""

    # --- 28-Oct-2026: the stock split and the assistant visit ------------

    usable_stock: Decimal | None = None
    """Usable units (new, refurbished) of this material at this plant, from
    the universe. Null when the condition is unknown or the pair has no
    universe row."""

    damaged_stock: Decimal | None = None
    """Damaged units of this material at this plant. Null when unknown."""

    stock_condition_known: bool | None = None
    """True when the stock record says which units are usable and which are
    damaged; false when only the total is known; null when the pair has no
    universe row at all."""

    declaration_session_id: str | None = None
    """The assistant session the covering declaration was made in, when it was
    made in one. Null for a declaration recorded on a register screen, and
    when nothing covers the row."""

    declaration_session_reference: str | None = None
    """How that session is quoted on screen (4-Nov-2026): ``R.../n`` for a line
    of a request, the session ID for an older single-material session. Null
    exactly when ``declaration_session_id`` is."""


class RegisterMeta(I8Model):
    """The register's counts. Every line counted is open in SAP; the
    ``excluded*`` figures say how many repair lines SAP has deleted, closed or
    blocked, or holds on a request for quotation, each counted under the first
    of the four that applies."""

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

    excluded_rfq_lines: int = 0
    """Item-category-3 lines on a request for quotation (EKKO.BSART), which
    orders nothing."""

    # The repair PRs with no PO yet, counted whether or not they are served
    # (I8_REPAIR_REQUESTS_ENABLED): the lines above stay PO-only. Each PR left
    # out is counted once, under the first of the three that applies.
    open_repair_requests: int = 0
    """Open repair PRs with no PO yet -- 108 on the 15-Oct extract."""

    requisitions_excluded_deleted: int = 0
    """Repair PRs whose item SAP has deleted (EBAN.LOEKZ)."""

    requisitions_excluded_live_po: int = 0
    """Repair PRs a live PO already points to -- those are register lines."""

    requisitions_excluded_reservation_closed: int = 0
    """Repair PRs whose repair-order line is deleted or final-issued."""


class VendorOption(I8Model):
    """One vendor a register or queue can be narrowed to."""

    vendor: str
    """The code, which is what the ``vendor`` filter takes."""

    name: str | None = None
    """LFA1's name, when one was delivered. A dropdown shows it and falls back
    to the code -- the rule the Vendor columns follow."""


class DepartmentOption(I8Model):
    """One department a register can be narrowed to (3-Nov-2026)."""

    value: str
    """The code -- the repair PR's EBAN.AFNAM -- which the ``department``
    filter takes."""

    name: str | None = None
    """The name from the department list the assistant keeps, when it has the
    code. A dropdown shows it and falls back to the code."""


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

    departments: list[DepartmentOption] = []
    """The departments that raised the listed repairs."""


class RegisterViewCounts(I8Model):
    """How many of the filtered lines each quick view of the register holds.

    Counted after every filter and before ``view``, so the screen's quick views
    can each show their number whichever one is selected.
    """

    all: int
    overdue: int
    undeclared: int
    """Never counts a PR row: there is nothing to declare against until phase 2."""
    at_vendor: int
    not_ordered: int = 0
    """Repair PRs with no PO yet. 0 while ``I8_REPAIR_REQUESTS_ENABLED`` is off."""


class RegisterResponse(I8Model):
    items: list[RepairChain]
    page: int
    page_size: int
    total: int
    meta: RegisterMeta
    facets: RegisterFacets
    reference_date: date
    view_counts: RegisterViewCounts | None = None


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


# --- Attestation and declarations (W5.3) ------------------------------------


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
    attestor_name: str | None = None
    """The name the person typed (27-Oct-2026); null on older rows."""
    amendment_reason: str | None = None
    repair_document_type: Literal["PO", "PR"] | None = None
    """Null when unbound; 'PO' for bound rows written before the column."""
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
    """The department code that raised the repair PR (the PO row's PR, since
    27-Oct-2026; the PO line's own requisitioner when that PR was not read).
    A CODE rather than a name; ``requesterName`` names it when known."""

    requester_name: str | None = None
    """The department name for ``requester``. Null when unknown -- the code is
    shown alone, never a guess."""

    repair_document_type: Literal["PO", "PR"] = "PO"
    """``PR`` for an open repair PR with no PO yet (id ``D-PR-{BANFN}-{BNFPO}``,
    ``relatedRepairId`` ``PR-{BANFN}-{BNFPO}``, ``repairPo`` null). Declare on
    it with ``repairDocumentType`` ``PR`` and ``pr`` as the document."""

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
    status: Literal["Required", "Completed", "Flagged", "Historical"]
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
    """Required only since 27-Oct-2026 -- the rows waiting for somebody to
    declare. A Flagged row has its declaration; its finding is an exception."""

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
    # bool first: in a str|int union a flag would be served as 1.
    rules: dict[str, bool | str | int]
    """The configuration actually in force, echoed back so a surprising number
    can be traced to a setting without reading the deployment."""


class FeaturesResponse(I8Model):
    """Which I08 features this backend serves (29-Oct-2026).

    ``completeFlowEnabled`` is always true since 3-Nov-2026, when the switch
    was retired; served for a frontend deployed before that release."""

    complete_flow_enabled: bool


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


class RepairRequestEvidenceItem(I8Model):
    """One open repair PR with no PO yet (since 27-Oct-2026). Served only while
    ``I8_REPAIR_REQUESTS_ENABLED`` is on. ``departmentName`` is null when the
    department list does not name the code; show the code."""

    purchase_requisition: str
    item: str
    quantity: Decimal
    requested_at: date | None = None
    days_since_requested: int | None = None
    department: str | None = None
    department_name: str | None = None
    repair_order: str | None = None


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
    # Since 27-Oct-2026, defaulted so an older client reads the old shape.
    # ``stockOnHand`` stays the plant total; ``exists`` counts usable units
    # where the condition is known.
    open_repair_request_lines: int = 0
    """Open repair PRs counted; 0 while the switch is off."""
    request_evidence: list[RepairRequestEvidenceItem] = []
    usable_stock: Decimal | None = None
    """New and refurbished units; null when the condition is unknown."""
    damaged_stock: Decimal | None = None
    stock_condition_known: bool = False
    stock_source: str | None = None
    """``MCHB`` / ``MBEW``; ``MARD`` when only the total is known; null with
    no stock record."""


# --- The job an assistant session was for (27-Oct-2026) --------------------


class JobMovementItem(I8Model):
    posted_on: date | None = None
    quantity: float | None = None


class SessionJobReservation(I8Model):
    """One job reservation line whose text names the session."""

    reservation: str
    item: str
    order: str | None = None
    material: str
    plant: str | None = None
    quantity: float | None = None
    requirement_date: date | None = None
    issued: list[JobMovementItem] = Field(default_factory=list)
    returned: list[JobMovementItem] = Field(default_factory=list)
    """Back to stock: posted on the receipt day, not when the unit came off."""


class SessionJobRepair(I8Model):
    """A repair PR or PO whose repair-order line carries the same reference."""

    row_id: str
    type: Literal["PR", "PO"]
    document: str
    item: str
    status: str


class SessionJobResponse(I8Model):
    session_id: str
    reservations: list[SessionJobReservation] = Field(default_factory=list)
    repairs: list[SessionJobRepair] = Field(default_factory=list)
