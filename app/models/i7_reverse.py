"""Min-Max -> OAR: the stored recommendation and its approval requests.

The pipeline's ``reverse_conversion`` stage works the recommendation out once per run and keeps it here, so the
page reads a fixed snapshot (with a "last rebuilt" time) instead of recalculating on every visit, and so a
planner's "Convert to OAR" request has a stable row to point at.

Four tables, none shared with the OAR -> Min-Max recommendation (``i7_recommendation``): that feature's pages,
KPIs and notifications read its own table, so nothing here can move a number there.

* ``i7_reverse_run``            one row per execution of the stage (also records a skipped or failed one);
* ``i7_reverse_recommendation`` one row per material-plant per run: category, reason, confidence, the figures;
* ``i7_reverse_conversion_request`` one planner request to convert a material-plant to OAR, with its place in
  the approval chain (the same chain, and the same state machine, as an OAR conversion);
* ``i7_reverse_approval_ledger`` every approval action, immutable (append-only; a SQL Server trigger refuses
  UPDATE and DELETE).

Read-only toward SAP: nothing here, and nothing that uses it, writes to SAP.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

RUN_SUCCEEDED = "succeeded"
RUN_SKIPPED = "skipped"
"""Nothing was written: no usable calculation month. The previous snapshot stays in use."""
RUN_FAILED = "failed"


class ReverseRun(Base):
    """One execution of the ``reverse_conversion`` pipeline stage."""

    __tablename__ = "i7_reverse_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    pipeline_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The pipeline run that triggered it, when there was one."""

    anchor_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    """First day of the latest complete month every window was measured to."""
    anchor_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    latest_staged_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    source_population: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    """Material-plants read for the scope."""
    excluded: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    duplicate_keys: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    rows_written: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    excluded_by_plant: Mapped[str | None] = mapped_column(Text, nullable=True)
    """``1300=2;1500=0`` -- so the per-plant reconciliation can be rebuilt from the stored rows."""

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Why a run was skipped, or what failed. Never row contents."""


class ReverseRecommendation(Base):
    """One material-plant's Min-Max -> OAR verdict in one run."""

    __tablename__ = "i7_reverse_recommendation"
    __table_args__ = (
        UniqueConstraint("run_id", "sap_material_number", "sap_plant_code", name="uq_i7_reverse_rec_run_key"),
        Index("ix_i7_reverse_rec_run_order", "run_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    sort_order: Mapped[int] = mapped_column(Integer)
    """Position in the ranked list (category, confidence, then stock value)."""

    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mrp_type: Mapped[str | None] = mapped_column(String(8), nullable=True)
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)

    category: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(48))
    """The reason code (``sparse_recurring_demand``); its wording lives in the code, not here."""
    detail: Mapped[str] = mapped_column(String(1000))
    decision_rule: Mapped[int] = mapped_column(Integer)
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    points: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The reason as three labelled lines, as JSON ``[[label, text], ...]``; None on a row stored before they existed."""

    issues_latest_12m: Mapped[int] = mapped_column(Integer)
    months_with_use_12m: Mapped[int] = mapped_column(Integer)
    issues_prior_12m: Mapped[int] = mapped_column(Integer)
    first_valid_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_valid_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    months_since_last_movement: Mapped[int | None] = mapped_column(Integer, nullable=True)
    history_span_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lifetime_months_with_use: Mapped[int] = mapped_column(Integer)

    lead_time_days: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    lead_time_bucket: Mapped[str] = mapped_column(String(8))
    current_stock: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    price_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    value_currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    stock_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    minbe: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)
    mabst: Mapped[Decimal | None] = mapped_column(Numeric(18, 3), nullable=True)


class PlanningGroupObservation(Base):
    """Which planning group (Min-Max or OAR) a material-plant was last seen in, and when that last changed.

    SAP gives no history of MRP type, so the pipeline's ``reverse_conversion`` stage keeps its own: on each run it
    compares a material-plant's group with the one stored here and, when it differs, stamps ``changed_on`` with
    the day of the run that noticed. That date starts the conversion cooldown. A material first seen has no
    ``changed_on`` (we do not know when it last changed), so it has no cooldown until a change is seen."""

    __tablename__ = "i7_planning_group_observation"
    __table_args__ = (UniqueConstraint("sap_material_number", "sap_plant_code", name="uq_i7_planning_group_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sap_material_number: Mapped[str] = mapped_column(String(40), index=True)
    sap_plant_code: Mapped[str] = mapped_column(String(8))
    planning_group: Mapped[str] = mapped_column(String(8))
    """``MIN_MAX`` (MRP type VM or VB) or ``OAR`` (ND or PD)."""
    first_seen_on: Mapped[date] = mapped_column(Date)
    changed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    """The day of the run that saw the group change; None while it has never been seen to change."""
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReverseConversionRequest(Base):
    """A planner's request to convert one material-plant from Min-Max to OAR, and where it is in the chain."""

    __tablename__ = "i7_reverse_conversion_request"
    __table_args__ = (Index("ix_i7_reverse_request_key", "sap_material_number", "sap_plant_code", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    status: Mapped[str] = mapped_column(String(32), index=True)
    """A ``LifecycleStatus`` value: PENDING_APPROVAL, SENT_BACK, HELD, REJECTED, SAP_EXECUTION_PENDING."""
    pending_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """The approval role whose decision is next (kept in step with ``status`` and ``chain_index`` by the approval
    service), so a list can show who the request is waiting for without loading the approval machinery."""
    chain_index: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    adjustment_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    # What the approvers were shown, frozen at the time of the request.
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    anchor_month: Mapped[date | None] = mapped_column(Date, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(8), nullable=True)
    reason: Mapped[str] = mapped_column(String(48))
    detail: Mapped[str] = mapped_column(String(1000))
    stock_value: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)

    submitted_by: Mapped[str] = mapped_column(String(64))
    submit_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReverseApprovalLedger(Base):
    """One immutable approval action on a Min-Max -> OAR request. Never updated, never deleted."""

    __tablename__ = "i7_reverse_approval_ledger"
    __table_args__ = (Index("ix_i7_reverse_ledger_request", "request_id", "timestamp"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))
    previous_status: Mapped[str] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32))
    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
