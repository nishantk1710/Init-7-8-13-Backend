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
    ExceptionStatus,
    ExceptionType,
    LedgerUtilisationStatus,
    LinkageStatus,
    ProcurementStatus,
)
from app.integrations.sap.source_mode import SourceMode


class DataSourceStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entity_set: str
    mode: SourceMode
    row_count: int
    available: bool
    fetched_at: datetime


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


class UtilisationLedgerEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ledger_id: str
    material: str
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

    first_gr_date: date | None
    latest_gr_date: date | None
    first_gi_date: date | None
    latest_gi_date: date | None

    procurement_status: ProcurementStatus
    utilisation_status: LedgerUtilisationStatus
    linkage_status: LinkageStatus

    data_source: str

    attribution_status: AttributionStatus | None = None
    attribution_evidence: str | None = None


class WatchMetricResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    material: str
    plant: str

    months_of_cover: Decimal | None
    months_of_cover_reason: str | None

    days_since_last_movement: int | None
    consumption_count_12m: int
    consumed_qty_12m: Decimal
    inventory_turns: Decimal | None
    inventory_turns_reason: str | None
    aging_band: AgingBand

    gr_not_issued_flag: bool
    gr_not_issued_days_since_gr: int | None
    gr_not_issued_received_quantity: Decimal
    gr_not_issued_issued_quantity: Decimal
    gr_not_issued_outstanding_quantity: Decimal

    acquired_vs_plan_status: AcquiredVsPlanStatus
    planned_quantity: Decimal | None
    received_quantity: Decimal
    issued_quantity: Decimal


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
    consumption_count_12m: int
    consumed_more_than_threshold: bool
    critical_impact_indicator: bool | None
    hod_justified_request_indicator: bool | None
    data_available: bool
    candidate_flag: bool
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


class ValidationResponse(BaseModel):
    tolerance_pct: Decimal
    results: list[ReconciliationSourceResult]


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
