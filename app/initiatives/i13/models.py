"""Internal domain models for Initiative 13.

These are backend-internal: API routes convert them to the Pydantic response
models in ``app.schemas.i13`` rather than exposing raw SAP records or these
dataclasses directly.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from app.shared.material_scope import MaterialScope


class AgingBand(str, Enum):
    FAST = "FAST"
    SLOW = "SLOW"
    NON_MOVING = "NON_MOVING"


class AttributionStatus(str, Enum):
    RESERVATION_LINK = "RESERVATION_LINK"
    ORDER_LINK = "ORDER_LINK"
    PROCUREMENT_LINK = "PROCUREMENT_LINK"
    UNATTRIBUTED = "UNATTRIBUTED"


class ConsumptionAttributionStatus(str, Enum):
    """W6.4: consumption *ownership* attribution -- who/which business
    object owns a ledger entry's consumption or unutilised OAR stock. Not to
    be confused with ``AttributionStatus`` above, which only classifies how
    a ledger entry's issued/received *quantity* was evidenced (W6.2's GI/GR
    linkage) -- see ``app.initiatives.i13.consumption_attribution``.
    """

    ATTRIBUTED = "ATTRIBUTED"
    PARTIALLY_ATTRIBUTED = "PARTIALLY_ATTRIBUTED"
    UNATTRIBUTED = "UNATTRIBUTED"
    # Two or more deterministic candidates disagree (e.g. conflicting raw
    # reservation rows) -- the FRS forbids picking one arbitrarily, so this
    # is reported rather than guessed.
    AMBIGUOUS = "AMBIGUOUS"


class ConsumptionAttributionSource(str, Enum):
    RESERVATION = "RESERVATION"
    RESERVATION_ORDER = "RESERVATION_ORDER"
    # Reserved for a future order-repository/AUFK-backed lookup -- not
    # producible today (no order repository exists in this codebase; RESB's
    # own Aufnr field is the only deterministic order reference available
    # today, which resolves as RESERVATION_ORDER instead). Kept so this enum
    # doesn't need to change shape when that source is added.
    ORDER = "ORDER"
    COST_CENTRE = "COST_CENTRE"
    NONE = "NONE"


class AcquiredVsPlanStatus(str, Enum):
    """``ON_PLAN`` is this codebase's name for what the FRS calls
    ``ALIGNED`` -- same state (acquired quantity exactly equals planned
    quantity), kept under its original name for backward compatibility with
    the already-shipped API contract and tests."""

    NO_PLAN = "NO_PLAN"
    BELOW_PLAN = "BELOW_PLAN"
    ON_PLAN = "ON_PLAN"
    ABOVE_PLAN = "ABOVE_PLAN"


class ExceptionType(str, Enum):
    PLAN_BREACH = "PLAN_BREACH"
    NO_PLAN = "NO_PLAN"
    GR_NOT_ISSUED_30_DAY = "GR_NOT_ISSUED_30_DAY"


class ExceptionStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


@dataclass(frozen=True)
class MovementMetrics:
    """W3.5: statistics-independent movement/aging metrics for one
    material+plant, computed directly from goods-movement history (never
    S031/S032). ``last_movement_date`` covers any qualifying movement;
    ``last_issue_date`` covers goods-issue/consumption movements only, and
    is what ``aging_band`` is actually classified on -- a material last
    touched by a goods *receipt* is not FAST merely because something
    happened to it recently. See ``app.initiatives.i13.movement_metrics``.
    """

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


class PrPoLinkStatus(str, Enum):
    """How (or whether) a procurement line's PR reference was resolved."""

    LINKED = "LINKED"
    PR_REFERENCE_UNRESOLVED = "PR_REFERENCE_UNRESOLVED"
    NO_PR_REFERENCE = "NO_PR_REFERENCE"
    NO_PO_YET = "NO_PO_YET"


class GrLinkStatus(str, Enum):
    RECEIVED = "RECEIVED"
    NO_RECEIPTS = "NO_RECEIPTS"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class GiLinkStatus(str, Enum):
    """Reserved ``LINKED`` member: not producible by W6.1 against this
    dataset (see ``procurement_chain.py``'s module docstring for the measured
    reason) -- included so the enum already carries the state W6.2 will
    start populating, rather than W6.2 having to add a new value later."""

    LINKED = "LINKED"
    UNRESOLVED_PENDING_RESERVATION = "UNRESOLVED_PENDING_RESERVATION"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class LifecycleStatus(str, Enum):
    """W6.1's single derived status -- see ``procurement_chain.py``'s
    ``_derive_lifecycle_status`` for exactly how each value is reached."""

    PR_CREATED = "PR_CREATED"
    ORDERED = "ORDERED"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED"
    RECEIVED = "RECEIVED"
    PARTIALLY_ISSUED = "PARTIALLY_ISSUED"
    ISSUED = "ISSUED"


@dataclass(frozen=True)
class PartialLedgerEntry:
    """W6.1: one PR -> PO -> GR -> GI procurement line, from real Postgres
    data, WITHOUT the reservation leg (that's W6.2 -- see
    ``app.initiatives.i13.procurement_chain``).

    One entry per PO item where a PO exists; a PR-only entry (``po_number``
    ``None``) where it doesn't yet. A PR item CAN legitimately produce more
    than one entry -- confirmed real multi-sourcing exists in this dataset
    (one PR item ordered across several POs), not a data defect -- so
    ``pr_number``+``pr_item`` is not a unique key for this model; only
    ``ledger_id`` is.
    """

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
    # None (never 0) when GI linkage is unresolved -- 0 would claim a
    # verified fact ("nothing was issued") this dataset cannot prove yet.
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


@dataclass(frozen=True)
class ProcurementChainDiagnostics:
    """Visibility into unmatched/ambiguous source records (FRS §13/§19.15) --
    never silently discarded, always queryable."""

    pr_items_total: int
    pr_items_with_no_po: int
    pr_items_with_single_po: int
    pr_items_with_multiple_po: int

    po_items_total: int
    po_items_with_no_pr_reference: int
    po_items_with_unresolved_pr_reference: int

    # Raw-extract duplicate SOURCE rows (same natural key appearing twice) --
    # distinct from "PR item legitimately split across multiple POs", which
    # is a real relationship, not a duplicate. Measured empty in this
    # dataset today (see the implementation report) but checked, not assumed.
    duplicate_pr_keys: list[tuple[str, str]] = field(default_factory=list)
    duplicate_po_keys: list[tuple[str, str]] = field(default_factory=list)


class ReservationPrLinkStatus(str, Enum):
    """How (or whether) a reservation's PR reference was resolved --
    the reservation-side analogue of ``PrPoLinkStatus``."""

    LINKED = "LINKED"
    PR_REFERENCE_UNRESOLVED = "PR_REFERENCE_UNRESOLVED"
    NO_PR_REFERENCE = "NO_PR_REFERENCE"
    # SAP MRP consolidated multiple reservations into one PR (measured: rare
    # but real -- 2 PR items in this dataset are each referenced by more than
    # one distinct reservation). No approved deterministic allocation rule
    # exists in this repository, so the PR link is shown but its quantities
    # are deliberately left unpopulated rather than guessed -- see
    # reservation_ledger.py's module docstring.
    CONSOLIDATION_UNRESOLVED = "CONSOLIDATION_UNRESOLVED"


@dataclass(frozen=True)
class ReservationLedgerEntry:
    """W6.2: Reservation -> PR -> PO -> GR -> GI, anchored on
    (reservation_number, reservation_item) -- never material+plant, since
    the same material can carry multiple independent reservations (see
    ``app.initiatives.i13.reservation_ledger``).

    Built by attaching reservation context (this module) onto W6.1's
    existing ``PartialLedgerEntry`` output, never by rebuilding the PR ->
    PO -> GR chain. ``issued_quantity`` here supersedes W6.1's -- it comes
    from the reservation's own RSNUM/RSPOS match against goods-issue
    movements, the deterministic link W6.1 didn't have.
    """

    ledger_id: str
    reservation_number: str
    reservation_item: str

    material: str
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
    first_issue_date: date | None
    last_issue_date: date | None

    # W6.2 §10: an arithmetic split derived from two independently
    # deterministic quantities (received via the PO/GR chain; issued via
    # RSNUM/RSPOS) -- not itself a new SAP fact, and only populated when
    # both quantities are known. See reservation_ledger.py.
    procurement_issued_quantity: Decimal | None
    direct_store_issued_quantity: Decimal | None

    lifecycle_status: LifecycleStatus
    reservation_pr_link_status: ReservationPrLinkStatus
    gr_link_status: GrLinkStatus
    gi_link_status: GiLinkStatus
    gi_link_reason: str | None

    material_scope: MaterialScope


@dataclass(frozen=True)
class AttributionResult:
    ledger_id: str
    status: AttributionStatus
    evidence: str


@dataclass(frozen=True)
class ConsumptionAttribution:
    """W6.4: deterministic ownership/accountability attribution for one W6.2
    ``ReservationLedgerEntry``. See ``app.initiatives.i13.consumption_attribution``
    for exactly how each field is resolved and what each status/source value
    means.
    """

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

    # Whether cost-centre enrichment was configured on when this record was
    # resolved -- audit context distinguishing "disabled" from "enabled but
    # unavailable" after the fact.
    cost_centre_attribution_enabled: bool

    attributed_at: datetime


@dataclass(frozen=True)
class WatchMetric:
    """W6.3: backend-computed WATCH utilisation-mart metrics for one
    material+plant -- months of cover, acquired-vs-plan, 30-day
    goods-received-not-issued, and the W3.5 aging/movement classification.

    Built on top of W6.2's ``ReservationLedgerEntry`` (acquisition detail)
    and W3.5's ``MovementMetrics`` (aging/consumption) -- see ``watch.py``.
    ``material_scope`` lets a caller filter to the W2.4/W6.2 OAR scope
    without WATCH reimplementing that classification.
    """

    material: str
    plant: str
    material_scope: MaterialScope

    # --- Coverage (months of cover) ---
    # ``months_of_cover`` is the FRS's "current_months_of_cover" -- kept
    # under its original name (predates this task) for backward
    # compatibility with the already-shipped API contract and its tests.
    stock_on_hand: Decimal | None
    open_po_quantity: Decimal
    average_monthly_consumption: Decimal
    months_of_cover: Decimal | None
    projected_months_of_cover: Decimal | None
    months_of_cover_reason: str | None

    # --- Movement / aging (W3.5, reused not reimplemented) ---
    last_movement_date: date | None
    days_since_last_movement: int | None
    last_issue_date: date | None
    days_since_last_issue: int | None
    consumption_count_12m: int
    consumed_qty_12m: Decimal
    inventory_turns: Decimal | None
    inventory_turns_reason: str | None
    aging_band: AgingBand

    # --- 30-day goods-received-not-issued ---
    gr_not_issued_flag: bool
    gr_not_issued_days_since_gr: int | None
    gr_not_issued_relevant_gr_date: date | None
    gr_not_issued_threshold_days: int
    gr_not_issued_received_quantity: Decimal
    gr_not_issued_issued_quantity: Decimal
    gr_not_issued_outstanding_quantity: Decimal

    # --- Acquired vs plan ---
    acquired_vs_plan_status: AcquiredVsPlanStatus
    planned_quantity: Decimal | None
    received_quantity: Decimal
    issued_quantity: Decimal
    acquired_vs_plan_variance_quantity: Decimal | None
    acquired_vs_plan_variance_percentage: Decimal | None

    # --- Audit ---
    calculated_at: datetime


@dataclass(frozen=True)
class WatchMartRefreshResult:
    """Outcome of one ``refresh_watch_metrics_mart`` run (see
    ``app.initiatives.i13.watch_mart``) -- what a caller/validation script
    checks to confirm the refresh happened and was idempotent."""

    row_count: int
    oar_only: bool
    as_of: date
    refreshed_at: datetime


@dataclass(frozen=True)
class ReclassificationCandidate:
    """OAR -> Min-Max reclassification evidence. Output only -- I13 never
    calls into I7's recommendation engine; I7 may later consume this."""

    material: str
    plant: str
    consumption_count_12m: int
    consumed_more_than_threshold: bool
    critical_impact_indicator: bool | None
    hod_justified_request_indicator: bool | None
    data_available: bool
    candidate_flag: bool
    candidate_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExceptionQueueItem:
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


@dataclass(frozen=True)
class ReconciliationResult:
    source_name: str
    computed_count: int
    reference_count: int | None
    absolute_difference: int | None
    percentage_difference: Decimal | None
    within_tolerance: bool | None
    status: str
