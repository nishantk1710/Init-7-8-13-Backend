"""I07 OAR similarity persistence.

Three tables: one per run, one row per selected neighbour, one row per target's
overall result. Neighbours are stored individually -- Phase 7 needs the full
evidence list to explain a recommendation, not just the winning score.

Portable constructs only: no ``JSONB``, no ``ARRAY``. Traces are short
delimited strings, matching every earlier phase.
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

SCORE = Numeric(9, 6)
QUANTITY = Numeric(18, 6)


class OarRun(Base):
    """One execution of the OAR similarity engine."""

    __tablename__ = "i7_oar_run"
    __table_args__ = (
        UniqueConstraint(
            "feature_run_id",
            "inventory_run_id",
            "policy_id",
            "policy_version",
            "algorithm_version",
            "embedding_model_version",
            name="uq_i7_oar_run_inputs",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    status: Mapped[str] = mapped_column(String(32), index=True)

    feature_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inventory_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_id: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[int] = mapped_column(Integer)
    algorithm_version: Mapped[str] = mapped_column(String(64))

    embedding_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_model_version: Mapped[str] = mapped_column(String(64))
    """``none`` when the embedding library is unavailable -- distinct from the
    real model's own version string, so a run made without text similarity is
    never mistaken for one that had it."""

    structured_weight: Mapped[Decimal] = mapped_column(SCORE)
    text_weight: Mapped[Decimal] = mapped_column(SCORE)
    business_weight: Mapped[Decimal] = mapped_column(SCORE)
    top_k: Mapped[int] = mapped_column(Integer)
    minimum_history_months: Mapped[int] = mapped_column(Integer)

    targets_evaluated: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<OarRun {self.id} status={self.status} targets={self.targets_evaluated}>"


class OarTargetResult(Base):
    """One cold-start target's overall outcome."""

    __tablename__ = "i7_oar_target"
    __table_args__ = (
        UniqueConstraint(
            "oar_run_id", "sap_material_number", "sap_plant_code",
            name="uq_i7_oar_target_key",
        ),
        Index("ix_i7_oar_target_status", "status", "confidence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    oar_run_id: Mapped[int] = mapped_column(Integer, index=True)
    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    status: Mapped[str] = mapped_column(String(32), index=True)
    confidence: Mapped[str] = mapped_column(String(16))

    candidates_considered: Mapped[int] = mapped_column(Integer, default=0)
    eligible_candidates: Mapped[int] = mapped_column(Integer, default=0)
    neighbour_count: Mapped[int] = mapped_column(Integer, default=0)
    best_similarity: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)

    rejection_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)
    """``reason=count;reason=count`` -- the rejection profile for this target's
    candidate population, so a systemic gap (missing criticality) is visible
    per target, not only in the run-level aggregate."""

    estimate_status: Mapped[str] = mapped_column(String(48), index=True)
    estimate_label: Mapped[str | None] = mapped_column(String(32), nullable=True)
    safety_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inventory_eligible_neighbours: Mapped[int] = mapped_column(Integer, default=0)
    inventory_ineligible_neighbours: Mapped[int] = mapped_column(Integer, default=0)
    estimate_trace: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class OarNeighbour(Base):
    """One selected neighbour for one target."""

    __tablename__ = "i7_oar_neighbour"
    __table_args__ = (
        UniqueConstraint(
            "oar_run_id",
            "sap_material_number",
            "sap_plant_code",
            "neighbour_material",
            "neighbour_plant",
            name="uq_i7_oar_neighbour_key",
        ),
        Index(
            "ix_i7_oar_neighbour_target",
            "oar_run_id",
            "sap_material_number",
            "sap_plant_code",
            "rank",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    oar_run_id: Mapped[int] = mapped_column(Integer, index=True)
    sap_material_number: Mapped[str] = mapped_column(String(40))
    sap_plant_code: Mapped[str] = mapped_column(String(8))

    neighbour_material: Mapped[str] = mapped_column(String(40))
    neighbour_plant: Mapped[str] = mapped_column(String(8))
    rank: Mapped[int] = mapped_column(Integer)

    combined_similarity: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)
    structured_similarity: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)
    text_similarity: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)
    business_similarity: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)
    score_completeness: Mapped[Decimal | None] = mapped_column(SCORE, nullable=True)

    same_circuit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    same_material_group: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    criticality: Mapped[str | None] = mapped_column(String(16), nullable=True)
    history_months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    safety_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rop: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inventory_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    """Whether this neighbour's Phase 5 values may contribute to the weighted
    estimate -- distinct from being an eligible similarity candidate."""

    def __repr__(self) -> str:
        return (
            f"<OarNeighbour {self.sap_material_number}/{self.sap_plant_code} "
            f"-> {self.neighbour_material}/{self.neighbour_plant} rank={self.rank}>"
        )
