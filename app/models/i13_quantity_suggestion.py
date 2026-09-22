"""W7.4: persisted reservation-time quantity suggestions and the
justifications requesters give for keeping a different quantity.

**Not a mart.** Every other I13 serving table here (``i13_watch_metric_mart``,
``i13_consumption_attribution``) is a derived view refreshed by delete-then-
insert, because it can always be recomputed from the raw extracts. These two
tables cannot: a suggestion row records that a specific figure was put in
front of a specific person at a specific moment, and it later accumulates
their decision (``accepted``) and their words (``QuantityJustificationRecord``).
A refresh that deleted and re-inserted would destroy exactly the evidence
FRS §8 needs to count a saving. So the write pattern here is the one
``i13_act_exception`` uses -- insert once, update in place, append-only
children -- and there is no refresh function anywhere for these tables.

Same portability rule as the rest of ``app/models``: portable constructs
only, so the schema creates and queries identically on Postgres (local dev)
and Azure SQL (the deployed target).

Grain is one row per suggestion *issued*, not per (material, plant): the
same material can be requested repeatedly, and each request is a separate
decision with its own basis and its own outcome.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

# Same portable fixed-point type the W6.3 mart uses for every SAP-derived
# quantity -- 18 integer digits, 6 fractional, on both Postgres NUMERIC and
# SQL Server DECIMAL. The engine quantizes to this scale before persisting
# (see app/initiatives/i13/quantity_suggestion.py), so nothing is silently
# truncated on the way in.
_QTY = Numeric(18, 6)


class QuantitySuggestionRecord(Base):
    """One suggestion issued, with its inputs and the config it was computed
    under. The field meanings live on ``app.initiatives.i13
    .quantity_suggestion.QuantitySuggestion``, which this mirrors."""

    __tablename__ = "i13_quantity_suggestion"

    suggestion_id: Mapped[str] = mapped_column(String(40), primary_key=True)

    # W7.5's ChatSession, once the reservation assistant issues one. Nullable
    # until then -- a suggestion computed directly from the BAdI deep link is
    # a complete, valid record on its own.
    session_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    material: Mapped[str] = mapped_column(String(40), index=True)
    plant: Mapped[str] = mapped_column(String(10), index=True)

    # The reservation the decision belongs to, where the caller knows it.
    # W6.6 keys a quantity-override exception on this when present (see
    # act/detection.quantity_override_business_key), falling back to the
    # session id, so carrying it here is what keeps one decision mapped to
    # one exception rather than colliding with the next request for the same
    # material.
    reservation_number: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    reservation_item: Mapped[str | None] = mapped_column(String(10), nullable=True)

    requester_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    requested_quantity: Mapped[Decimal] = mapped_column(_QTY)
    suggested_quantity: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    """NULL where the engine declined -- never the requested quantity echoed
    back, which a consumer would read as agreement."""

    direction: Mapped[str] = mapped_column(String(16), index=True)
    reason_code: Mapped[str] = mapped_column(String(32), index=True)
    reason_text: Mapped[str] = mapped_column(String(2000))

    # Where reason_text's words came from: DETERMINISTIC or MODEL, plus the
    # prompt version and deployment when a model wrote it. The figures are
    # never the model's (see quantity_suggestion_reason.py); this is here so
    # an AI-phrased sentence stays traceable to the exact prompt behind it.
    reason_source: Mapped[str] = mapped_column(String(16))
    reason_prompt_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    reason_prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason_model: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # --- Input snapshot, from the W6.3 mart row read at suggestion time ---
    average_monthly_consumption: Mapped[Decimal] = mapped_column(_QTY)
    stock_on_hand: Mapped[Decimal] = mapped_column(_QTY)
    open_po_quantity: Mapped[Decimal] = mapped_column(_QTY)
    consumption_count_12m: Mapped[int] = mapped_column(Integer)

    # --- Basis ---
    plan_window_months: Mapped[Decimal] = mapped_column(_QTY)
    resulting_cover_months: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    plan_need_quantity: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    net_need_quantity: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    ceiling_quantity: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)

    # --- Config snapshot: what the thresholds WERE when this was computed.
    # Retuning the ceiling later must not silently rewrite the reasoning
    # behind a suggestion already made and possibly already accepted.
    cover_ceiling_months: Mapped[Decimal | None] = mapped_column(_QTY, nullable=True)
    minimum_history_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lookback_months: Mapped[int] = mapped_column(Integer)

    # --- Outcome (FRS §8 savings attribution) ---
    accepted: Mapped[bool | None] = mapped_column(Boolean, nullable=True, index=True)
    """NULL means undecided, and is not the same as False. A benefit is
    counted only where the requester accepted, so "they said no" and "they
    have not answered yet" must never collapse into one state."""
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(String(40), nullable=True)

    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"<QuantitySuggestionRecord {self.suggestion_id} {self.material}/{self.plant} "
            f"{self.direction} reason={self.reason_code} accepted={self.accepted}>"
        )


class QuantityJustificationRecord(Base):
    """Why a requester kept a quantity different from the suggestion.

    Reuses the ``reason_category`` + ``free_text`` shape W6.6's requester
    confirmation already uses (``i13_act_confirmation``), so the initiative
    has one justification vocabulary rather than two that drift.

    Append-only, like every other W6.6 child table: a requester who revises
    their reasoning adds a row, and the earlier one stays readable. The
    latest row is the current justification.
    """

    __tablename__ = "i13_quantity_justification"

    justification_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    suggestion_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("i13_quantity_suggestion.suggestion_id"), index=True
    )

    reason_category: Mapped[str] = mapped_column(String(60))
    free_text: Mapped[str] = mapped_column(String(2000))

    actor_id: Mapped[str] = mapped_column(String(40))
    """From ``get_current_actor`` -- a request header today, Entra claims
    after W1.6, with no change needed here."""

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"<QuantityJustificationRecord {self.suggestion_id} category={self.reason_category}>"
