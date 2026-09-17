"""I07 recommendation, approval and adoption persistence.

Five tables:

* ``i7_recommendation``           the current state of one material-plant's proposal
* ``i7_recommendation_version``   an immutable snapshot, written on every ADJUST
* ``i7_approval_ledger``          append-only audit trail of every workflow action
* ``i7_sap_execution_evidence``   manual VZI execution evidence (never a SAP call)
* ``i7_sap_adoption``             read-only reconciliation result

**The recommendation row is mutable; the ledger is not.** A recommendation's
``status`` and ``chain_index`` change as it moves through approval -- that is
the whole point of a workflow table. Every *event* that produced a state change
is instead written once to the ledger and never touched again, which is what
makes "what actually happened, in order" reconstructable even after the
recommendation itself has moved on.

Portable constructs only: no ``JSONB``, no ``ARRAY``, matching every earlier
phase.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

QUANTITY = Numeric(18, 6)


class Recommendation(Base):
    """The current state of one material-plant's stocking recommendation."""

    __tablename__ = "i7_recommendation"
    __table_args__ = (
        UniqueConstraint(
            "sap_material_number",
            "sap_plant_code",
            "feature_run_id",
            "forecast_run_id",
            "inventory_run_id",
            "oar_run_id",
            "policy_id",
            "policy_version",
            "formula_version",
            name="uq_i7_recommendation_inputs",
        ),
        Index("ix_i7_recommendation_status", "status"),
        Index("ix_i7_recommendation_material_plant", "sap_material_number", "sap_plant_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[str] = mapped_column(String(64), index=True)
    """A stable, human-readable identity for one material-plant's *current*
    proposal -- not a database-unique key. The uniqueness that actually
    matters is the upstream-input tuple below: rerunning the pipeline with new
    Phase 3/4/5/6 runs produces a new row with the *same* recommendation_id,
    which is correct (it is still "the recommendation for this material-plant"),
    and it is the caller's job to read the newest one when more than one
    exists rather than the database's job to forbid it."""

    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    # --- Provenance: which upstream runs produced this row --------------
    feature_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    forecast_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inventory_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oar_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    formula_version: Mapped[str] = mapped_column(String(64))

    # --- Path and classification ------------------------------------------
    is_oar: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    demand_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    history_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- Current vs recommended -------------------------------------------
    current_safety_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    current_rop: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    current_max_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    recommended_safety_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    recommended_rop: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    recommended_max_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    # --- Demand and inventory method metadata --------------------------------
    baseline_model: Mapped[str | None] = mapped_column(String(32), nullable=True)
    forecast_rate: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    lead_time_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    safety_stock_method: Mapped[str | None] = mapped_column(String(32), nullable=True)
    max_stock_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)

    # --- OAR-specific ------------------------------------------------------
    oar_neighbour_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    oar_best_similarity: Mapped[Decimal | None] = mapped_column(Numeric(9, 6), nullable=True)

    oar_similarity_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    """``AVAILABLE`` / ``NOT_AVAILABLE``. Whether Phase 6 found >=1 eligible
    neighbour -- distinct from whether the recommendation itself is
    reviewable. A material can have available similarity evidence and still
    be NOT_EVALUABLE because no neighbour could lend a Phase 5 value."""

    oar_estimate_status: Mapped[str | None] = mapped_column(String(48), nullable=True)
    """Phase 6's own ``EstimateStatus`` value, preserved even when the
    recommendation is blocked -- the similarity work is never discarded."""

    # --- Conversion eligibility -------------------------------------------
    conversion_eligibility: Mapped[str | None] = mapped_column(String(16), nullable=True)
    conversion_trigger: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conversion_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    consumption_count_12m: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """SOP 3.1.1 indicator 1's own count, MSEG issue transactions minus
    reversals, trailing 12 months. Structured, not just embedded in
    ``conversion_detail`` -- a client can read/filter/sort on it without
    parsing free text."""

    consumption_count_threshold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    """The threshold the count above was compared against (policy-configured,
    not hardcoded -- see ``ConversionTriggerPolicy.consumption_count_threshold``)."""

    production_impact: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """SOP 3.1.1 indicator 2's own boolean. ``NULL`` means unresolved (tier set
    not configured, or criticality unavailable), never a silent ``False``."""

    i13_hod_approved: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    """SOP 3.1.1 indicator 3's own boolean. ``NULL`` means no I13 ledger is
    available to answer the question, never a silent ``False``."""

    # --- Blocking / quality -------------------------------------------------
    blocking_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    factors_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Rendered factors, one per line -- the same content as
    ``RecommendationFactor`` tuples, kept as text per the no-JSONB rule."""

    calculation_trace: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Expected impact ----------------------------------------------------
    impact_status: Mapped[str] = mapped_column(String(48))
    safety_stock_delta: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    rop_delta: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    max_stock_delta: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    # --- Workflow state -----------------------------------------------------
    status: Mapped[str] = mapped_column(String(32), index=True)
    chain_index: Mapped[int] = mapped_column(Integer, default=0)
    adjustment_count: Mapped[int] = mapped_column(Integer, default=0)
    current_version: Mapped[int] = mapped_column(Integer, default=1)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    def __repr__(self) -> str:
        return (
            f"<Recommendation {self.recommendation_id} "
            f"{self.sap_material_number}/{self.sap_plant_code} status={self.status}>"
        )


class RecommendationVersion(Base):
    """An immutable snapshot, written whenever ADJUST changes a value.

    The current row above is the live state; each row here is a point-in-time
    copy of the values that were true before an adjustment replaced them, so an
    approver three steps later can see exactly what changed and when.
    """

    __tablename__ = "i7_recommendation_version"
    __table_args__ = (
        UniqueConstraint(
            "recommendation_id", "version", name="uq_i7_recommendation_version_key"
        ),
        Index("ix_i7_recommendation_version_rec", "recommendation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    recommendation_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)

    recommended_safety_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    recommended_rop: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    recommended_max_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    reason: Mapped[str] = mapped_column(String(500))
    changed_by: Mapped[str] = mapped_column(String(64))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ApprovalLedgerEntry(Base):
    """One immutable approval action. Never updated, never deleted."""

    __tablename__ = "i7_approval_ledger"
    __table_args__ = (
        Index("ix_i7_approval_ledger_rec", "recommendation_id", "timestamp"),
        Index("ix_i7_approval_ledger_actor", "actor_id"),
        Index("ix_i7_approval_ledger_action", "action"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    recommendation_id: Mapped[str] = mapped_column(String(64), index=True)
    recommendation_version: Mapped[int] = mapped_column(Integer)

    actor_id: Mapped[str] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(16))

    previous_status: Mapped[str] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32))

    comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<ApprovalLedgerEntry {self.recommendation_id} {self.action} by {self.actor_role}>"


class SapExecutionEvidence(Base):
    """Manual VZI SAP execution evidence. I07 never calls SAP; this table only
    records what a human reports happened outside the system."""

    __tablename__ = "i7_sap_execution_evidence"
    __table_args__ = (Index("ix_i7_sap_execution_evidence_rec", "recommendation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    recommendation_id: Mapped[str] = mapped_column(String(64), index=True)

    approved_safety_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    approved_rop: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)
    approved_max_stock: Mapped[Decimal | None] = mapped_column(QUANTITY, nullable=True)

    executed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sap_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_status: Mapped[str] = mapped_column(String(16), index=True)
    evidence_comment: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SapAdoptionResult(Base):
    """One read-only adoption reconciliation result."""

    __tablename__ = "i7_sap_adoption"
    __table_args__ = (Index("ix_i7_sap_adoption_rec", "recommendation_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    recommendation_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)

    expected_fields: Mapped[str | None] = mapped_column(String(500), nullable=True)
    observed_fields: Mapped[str | None] = mapped_column(String(500), nullable=True)
    matched_fields: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mismatched_fields: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
