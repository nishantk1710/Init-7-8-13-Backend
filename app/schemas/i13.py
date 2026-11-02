"""Pydantic response contracts for the Initiative 13 API.

Routes convert internal domain models (``app.initiatives.i13.models``) to
these before returning -- raw SAP records never reach the API layer.
"""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.initiatives.i13.models import (
    AcquiredVsPlanStatus,
    AgingBand,
    AttributionStatus,
    ConsumptionAttributionSource,
    ConsumptionAttributionStatus,
    ExceptionStatus,
    ExceptionType,
    GiLinkStatus,
    GrLinkStatus,
    LifecycleStatus,
    PrPoLinkStatus,
    ReservationPrLinkStatus,
)
from app.initiatives.i13.ledger_compat import LedgerUtilisationStatus, LinkageStatus, ProcurementStatus
from app.initiatives.i13.stock_issue_matching import IssueMatch
from app.shared.material_scope import MaterialScope


class DataSourceStatusResponse(BaseModel):
    """Postgres ingestion status per raw extract table (from ``ingestion_run``)
    -- replaces the old LIVE/MOCK SapGateway diagnostic, which no longer
    applies now that every I13 read goes through Postgres."""

    model_config = ConfigDict(from_attributes=True)

    table: str
    row_count: int
    status: str
    loaded_at: datetime | None


class MovementMetricsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material: str
    plant: str

    last_movement_date: date | None
    days_since_last_movement: int | None

    last_issue_date: date | None
    days_since_last_issue: int | None

    consumption_count_12m: int
    consumption_qty_12m: Decimal

    inventory_turns: Decimal | None
    inventory_turns_reason: str | None

    aging_band: AgingBand

    calculated_at: datetime


class PartialLedgerEntryResponse(BaseModel):
    """W6.1: PR -> PO -> GR -> GI, without the reservation leg (see W6.2)."""

    model_config = ConfigDict(from_attributes=True)

    ledger_id: str
    material: str
    plant: str

    pr_number: str | None
    pr_item: str | None
    po_number: str | None
    po_item: str | None

    pr_quantity: Decimal | None
    ordered_quantity: Decimal | None
    received_quantity: Decimal
    issued_quantity: Decimal | None

    first_gr_date: date | None
    last_gr_date: date | None
    first_issue_date: date | None
    last_issue_date: date | None

    lifecycle_status: LifecycleStatus
    pr_po_link_status: PrPoLinkStatus
    gr_link_status: GrLinkStatus
    gi_link_status: GiLinkStatus
    gi_link_reason: str | None


class ProcurementChainDiagnosticsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pr_items_total: int
    pr_items_with_no_po: int
    pr_items_with_single_po: int
    pr_items_with_multiple_po: int

    po_items_total: int
    po_items_with_no_pr_reference: int
    po_items_with_unresolved_pr_reference: int

    duplicate_pr_keys: list[tuple[str, str]]
    duplicate_po_keys: list[tuple[str, str]]


class ReservationLedgerEntryResponse(BaseModel):
    """W6.2: Reservation -> PR -> PO -> GR -> GI."""

    model_config = ConfigDict(from_attributes=True)

    ledger_id: str
    reservation_number: str
    reservation_item: str

    material: str

    material_description: str | None = None

    """MAKT / ZMM065 name, where either has one -- see app.shared.material_names."""
    plant: str
    reservation_quantity: Decimal
    requirement_date: date | None

    pr_number: str | None
    pr_item: str | None
    po_number: str | None
    po_item: str | None

    ordered_quantity: Decimal | None
    received_quantity: Decimal | None
    issued_quantity: Decimal

    first_gr_date: date | None
    last_gr_date: date | None
    expected_delivery_date: date | None = None
    """EKET-EINDT of the PO line's next schedule line still to deliver (the
    last one when all are delivered); null with no PO line or no EKET row.
    Attached at the route from ``app.initiatives.i13.expected_delivery``."""
    first_issue_date: date | None
    last_issue_date: date | None

    procurement_issued_quantity: Decimal | None
    direct_store_issued_quantity: Decimal | None

    lifecycle_status: LifecycleStatus
    reservation_pr_link_status: ReservationPrLinkStatus
    gr_link_status: GrLinkStatus
    gi_link_status: GiLinkStatus
    gi_link_reason: str | None

    material_scope: MaterialScope
    reservation_deleted: bool = False

    attribution_status: AttributionStatus | None = None
    attribution_evidence: str | None = None

    #: The assistant session this reservation's item text (SGTXT) names.
    session_id: str | None = None
    #: RESB.SGTXT as loaded (or as the UAT overlay says).
    sgtxt: str | None = None
    #: A reservation that exists only in the UAT overlay (simulated).
    uat_simulated: bool = False
    #: True when nothing is issued and the reservation was required before
    #: ``movement_history_start``: it may have been issued before the movement
    #: history the platform holds, so "issued 0" is not known, not zero.
    issued_before_history: bool = False
    #: The earliest goods movement held; null when the snapshot did not record it.
    movement_history_start: date | None = None


class UtilisationLedgerEntryResponse(BaseModel):
    """Compatibility contract for ``GET /api/i13/ledger`` -- see
    ``app.initiatives.i13.ledger_compat`` for why this still exists and what
    it's built from now."""

    model_config = ConfigDict(from_attributes=True)

    ledger_id: str
    material: str
    material_description: str | None = None
    """MAKT / ZMM065 name, where either has one -- see app.shared.material_names."""
    plant: str

    reservation_number: str | None
    reservation_item: str | None

    pr_number: str | None
    pr_item: str | None

    po_number: str | None
    po_item: str | None

    received_quantity: Decimal
    issued_quantity: Decimal
    open_quantity: Decimal
    ordered_quantity: Decimal | None = None
    """EKPO-MENGE of the PO line, so a screen can say "0 of 6 received" --
    ordered minus received on an open line is what the assistant's open-PO
    quantity adds up. Null with no PO line, or off the stored snapshot.
    Attached at the route (``SqlSnapshot.ordered_quantities_for``)."""

    first_gr_date: date | None
    latest_gr_date: date | None
    first_gi_date: date | None
    latest_gi_date: date | None
    expected_delivery_date: date | None = None
    """EKET-EINDT of the PO line's next schedule line still to deliver (the
    last one when all are delivered); null with no PO line or no EKET row.
    Attached at the route from ``app.initiatives.i13.expected_delivery``."""

    procurement_status: ProcurementStatus
    utilisation_status: LedgerUtilisationStatus
    linkage_status: LinkageStatus

    data_source: str

    attribution_status: AttributionStatus | None = None
    attribution_evidence: str | None = None

    issue_match: IssueMatch | None = None
    """How ``issued_quantity`` was found: RESERVATION (the reservation that
    raised the PR, exact), OLDEST_DELIVERY_FIRST (stock issued out of the
    material's deliveries, oldest first) or BEFORE_ISSUE_HISTORY (delivered
    before the goods-movement history starts). See
    ``app.initiatives.i13.stock_issue_matching``."""
    delivery_count: int = 0
    """Goods receipts (EKBE 101, net of 102) on the PO line."""
    next_planned_issue_date: date | None = None
    """RESB-BDTER of the earliest open reservation expected to draw on this
    line's stock still left."""
    next_planned_issue_quantity: Decimal | None = None


class ConsumptionAttributionResponse(BaseModel):
    """W6.4: deterministic ownership/accountability attribution for one W6.2
    ``ReservationLedgerEntry``."""

    model_config = ConfigDict(from_attributes=True)

    ledger_id: str

    material: str
    plant: str

    reservation_number: str
    reservation_item: str

    requester_id: str | None
    order_number: str | None
    cost_centre: str | None

    status: ConsumptionAttributionStatus
    source: ConsumptionAttributionSource
    evidence: str

    cost_centre_attribution_enabled: bool

    attributed_at: datetime


class WatchMetricResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material: str

    material_description: str | None = None

    """MAKT / ZMM065 name, where either has one -- see app.shared.material_names."""
    plant: str
    material_scope: MaterialScope

    stock_on_hand: Decimal | None
    open_po_quantity: Decimal
    average_monthly_consumption: Decimal
    months_of_cover: Decimal | None
    projected_months_of_cover: Decimal | None
    months_of_cover_reason: str | None

    last_movement_date: date | None
    days_since_last_movement: int | None
    last_issue_date: date | None
    days_since_last_issue: int | None
    consumption_count_12m: int
    consumed_qty_12m: Decimal
    inventory_turns: Decimal | None
    inventory_turns_reason: str | None
    aging_band: AgingBand

    gr_not_issued_flag: bool
    gr_not_issued_days_since_gr: int | None
    gr_not_issued_relevant_gr_date: date | None
    gr_not_issued_threshold_days: int
    gr_not_issued_received_quantity: Decimal
    gr_not_issued_issued_quantity: Decimal
    gr_not_issued_outstanding_quantity: Decimal

    acquired_vs_plan_status: AcquiredVsPlanStatus
    planned_quantity: Decimal | None
    received_quantity: Decimal
    issued_quantity: Decimal
    acquired_vs_plan_variance_quantity: Decimal | None
    acquired_vs_plan_variance_percentage: Decimal | None

    calculated_at: datetime


class ExceptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    type: ExceptionType
    status: ExceptionStatus

    material: str
    plant: str

    reservation_number: str | None
    pr_number: str | None
    po_number: str | None

    owner_id: str | None
    owner_name: str | None

    created_at: datetime
    due_at: datetime | None
    days_overdue: int | None

    reason: str
    evidence: str


class ReclassificationCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material: str
    plant: str
    as_of_date: date
    consumption_count_12m: int
    consumption_threshold: int
    consumed_more_than_threshold: bool
    critical_impact_indicator: bool | None
    hod_justified_request_indicator: bool | None
    data_available: bool
    candidate_flag: bool
    generated_at: datetime
    candidate_reasons: list[str]


class ReconciliationSourceResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_name: str
    computed_count: int
    reference_count: int | None
    absolute_difference: int | None
    percentage_difference: Decimal | None
    within_tolerance: bool | None
    status: str


class Zmm065MismatchResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material: str
    plant: str
    report_band: str
    platform_band: str
    report_last_issue_date: date | None
    platform_last_issue_date: date | None
    reason: str


class Zmm065ValidationResponse(BaseModel):
    """FR-6 aging reconciliation against ZMM065, as of the report's run date."""

    model_config = ConfigDict(from_attributes=True)

    report_date: date | None
    rows_in_report: int
    compared: int
    agreed: int
    agreement_pct: float | None
    excluded_non_aging: dict[str, int]
    mismatch_reasons: dict[str, int]
    #: The first ``DETAIL_LIMIT`` disagreements; the counts above are complete.
    mismatches: list[Zmm065MismatchResponse]


class GrReceiptCheckResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    post_date: date
    material: str
    po_number: str
    po_item: str
    status: str
    plant: str | None


class Gr30DayValidationResponse(BaseModel):
    """FR-6 receipt-by-receipt confirmation of the 30-Day GR Report."""

    model_config = ConfigDict(from_attributes=True)

    report_date: date | None
    window_start: date | None
    rows_in_report: int
    confirmed: int
    plants: dict[str, int]
    platform_receipts_in_window: int
    unconfirmed: list[GrReceiptCheckResponse]


class Zmm065SourceResponse(BaseModel):
    """Where one plant's ZMM065 rows came from: a monthly upload, or the
    seeded July workbook when that plant has no upload."""

    model_config = ConfigDict(from_attributes=True)

    plant: str
    source: str
    row_count: int
    upload_id: int | None = None
    report_month: date | None = None
    report_date: date | None = None
    file_name: str | None = None
    uploaded_by: str | None = None
    uploaded_at: datetime | None = None


class Zmm065UploadResponse(BaseModel):
    """One uploaded ZMM065 monthly report."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    plant: str
    report_month: date
    report_date: date | None
    file_name: str
    sheet_name: str
    row_count: int
    uploaded_by: str
    uploaded_at: datetime
    #: Whether validation reads this upload when no month is chosen -- the
    #: latest upload for its plant.
    is_current: bool = False


class Zmm065UploadResultResponse(BaseModel):
    """What ``POST /validation/zmm065/uploads`` stored, and what it skipped."""

    upload: Zmm065UploadResponse
    skipped_out_of_scope: int
    skipped_blank: int
    #: True when this upload superseded an earlier one for the same plant and month.
    replaced_earlier: bool


class ValidationResponse(BaseModel):
    tolerance_pct: Decimal
    results: list[ReconciliationSourceResult]
    #: Null when the report is not loaded; its result rows then read
    #: REFERENCE_UNAVAILABLE.
    zmm065: Zmm065ValidationResponse | None = None
    gr_30_day: Gr30DayValidationResponse | None = None
    #: The month asked for, or null for "latest per plant".
    zmm065_report_month: date | None = None
    #: Per plant, which ZMM065 the reconciliation used.
    zmm065_sources: list[Zmm065SourceResponse] = []


class I13SummaryResponse(BaseModel):
    total_oar_positions: int
    fast_moving_count: int
    slow_moving_count: int
    non_moving_count: int
    gr_not_issued_30_day_count: int
    plan_breach_count: int
    no_plan_count: int
    reclassification_candidate_count: int
    valuation_is_mocked: bool
    #: Generated reference plans behind the plan-based counts; 0 unless
    #: I13_REFERENCE_PLANS_ENABLED is on. See I13Summary.reference_plan_count.
    reference_plan_count: int


class SnapshotStatusResponse(BaseModel):
    """What the I13 read routes are serving from (app/initiatives/i13/snapshot.py)."""

    status: str
    enabled: bool
    version: int | None = None
    reference_date: date | None = None
    built_at: datetime | None = None
    build_seconds: float | None = None
    fingerprint: str | None = None
    building_since: datetime | None = None
    rebuilding: bool = False
    last_error: str | None = None


class GrniEntryResponse(BaseModel):
    """One PO line received at least the GR-not-issued threshold ago with
    stock not yet issued (FRS FR-6) -- the per-line form of WATCH's flag.
    ``outstanding_quantity`` is what is still in stock out of the line's
    deliveries; ``issue_match`` says how the issued part was found."""

    ledger_id: str
    material: str
    material_description: str | None = None
    """MAKT / ZMM065 name, where either has one -- see app.shared.material_names."""
    plant: str
    material_scope: str
    pr_number: str | None
    pr_item: str | None = None
    po_number: str
    po_item: str
    reservation_number: str | None = None
    """Only when the line's PR was raised by a reservation (issue_match RESERVATION)."""
    reservation_item: str | None = None
    received_quantity: Decimal
    issued_quantity: Decimal
    outstanding_quantity: Decimal
    first_gr_date: date | None
    last_gr_date: date
    days_since_gr: int
    threshold_days: int
    delivery_count: int = 0
    first_issue_date: date | None = None
    last_issue_date: date | None = None
    issue_match: IssueMatch | None = None
    next_planned_issue_date: date | None = None
    next_planned_issue_quantity: Decimal | None = None
    utilisation_status: LedgerUtilisationStatus


class MonthlyConsumptionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    month: str
    issued_quantity: Decimal
    issue_count: int
    received_quantity: Decimal


class UsagePatternResponse(BaseModel):
    """One material-plant's month-by-month goods issues and receipts.

    ``months`` covers every month in the delivered history (``history_months``
    on the list response's header), with zero months filled in, so a gap in
    consumption reads as a gap rather than disappearing.
    """

    material: str

    material_description: str | None = None

    """MAKT / ZMM065 name, where either has one -- see app.shared.material_names."""
    plant: str
    material_scope: str
    aging_band: str | None
    stock_on_hand: Decimal | None
    average_monthly_consumption: Decimal | None
    months_of_cover: Decimal | None
    issued_quantity_total: Decimal
    issue_count_total: int
    active_months: int
    last_issue_month: str | None
    months: list[MonthlyConsumptionResponse]


class SessionLinkResponse(BaseModel):
    """A reservation item whose item text (SGTXT) names an assistant session."""

    model_config = ConfigDict(from_attributes=True)

    session_id: str
    reservation_number: str
    reservation_item: str
    material: str
    plant: str
    source: str
    sgtxt: str | None = None
    first_seen_at: datetime


class SessionComplianceResponse(BaseModel):
    """FR-4: OAR reservations required since go-live, by what their SGTXT says."""

    go_live_date: date
    reservations: int
    covered: int
    session_without_plan: int
    invalid_session: int
    missing_session: int


class UatStatusResponse(BaseModel):
    enabled: bool
    go_live_date: date


class UatReservationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reservation_number: str
    reservation_item: str
    material: str
    plant: str
    simulated: bool
    requirement_date: date | None
    requirement_quantity: Decimal | None
    sgtxt: str
    original_sgtxt: str | None
    session_id: str | None
    created_by: str
    created_at: datetime


class UatCandidateResponse(BaseModel):
    """An existing reservation (loaded extract) the session ID could be stamped on."""

    reservation_number: str
    reservation_item: str
    requirement_date: date | None
    reservation_quantity: Decimal
    sgtxt: str | None
    session_id: str | None
    lifecycle_status: str


class UatSimulateRequest(BaseModel):
    session_id: str


class UatSimulateRequestBody(BaseModel):
    """Simulate the one SAP reservation for a whole multi-material request."""

    request_id: str


class UatStampRequest(BaseModel):
    session_id: str
    reservation_number: str
    reservation_item: str


# --- Summary report (app/initiatives/i13/report/) ---------------------------


class SummaryReportAction(BaseModel):
    action: str
    owner: str


class SummaryReportNarrative(BaseModel):
    headline: str
    findings: list[str]
    risks: list[str]
    actions: list[SummaryReportAction]
    #: MODEL or DETERMINISTIC -- whether an AI model wrote the words.
    source: str
    prompt_id: str | None = None
    prompt_version: int | None = None
    model: str | None = None
    #: Why the template was used, when it was.
    notes: list[str] = []


class SummaryReportListItem(BaseModel):
    id: str
    generated_at: datetime
    generated_by: str
    plant: str | None
    data_as_of: date | None
    headline: str
    narrative_source: str


class SummaryReportResponse(SummaryReportListItem):
    #: Every figure the report states -- see app/initiatives/i13/report/facts.py
    #: for the shape. Stored as generated, never recomputed.
    facts: dict
    narrative: SummaryReportNarrative


class LedgerCountsResponse(BaseModel):
    """The procurement view's header, over every line the filters match."""

    total: int
    by_linkage_status: dict[str, int]


class WatchCountsResponse(BaseModel):
    """The dashboard's bars and tab badges, over every WATCH row the filters
    match -- not over a page of them."""

    total: int
    by_acquired_vs_plan_status: dict[str, int]
    by_aging_band: dict[str, int]


class ActExceptionCountsResponse(BaseModel):
    """The dashboard's exception-status bar, over the whole queue."""

    total: int
    by_status: dict[str, int]
    #: Exceptions with a requester to route to; the rest reach nobody.
    with_requester: int
