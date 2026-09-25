"""W7.4 quantity-suggestion API contracts.

Routes convert the persisted rows (``app.models.i13_quantity_suggestion``)
to these before returning -- the ORM row shape never reaches the API layer,
matching the rest of Initiative 13's schema convention (see
``app/schemas/i13.py`` and ``app/schemas/i13_act.py``).

Every quantity is ``Decimal``, never ``float``: these figures become purchase
quantities, and a binary-floating-point 0.1 in a spares order is a defect
waiting for someone to notice it.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.initiatives.i13.quantity_suggestion import SuggestionDirection, SuggestionReason


class QuantitySuggestionRequest(BaseModel):
    """A reservation-time quantity decision to evaluate.

    ``plan_window_months`` is supplied by the caller rather than looked up:
    it is the one value W7.4 needs from W7.3's consumption plan, and taking
    it as a parameter is what lets this engine run ahead of the chat flow
    (see ``app.initiatives.i13.quantity_suggestion``). When W7.5 issues
    sessions, the assistant passes its plan's window here and nothing in the
    engine changes.
    """

    material: str = Field(..., min_length=1, max_length=40)
    plant: str = Field(..., min_length=1, max_length=10)
    requested_quantity: Decimal = Field(..., ge=0)
    plan_window_months: Decimal = Field(..., ge=0, description="Months of use the requester stated.")

    session_id: str | None = Field(None, max_length=40, description="W7.5 ChatSession, once one exists.")
    reservation_number: str | None = Field(None, max_length=20)
    reservation_item: str | None = Field(None, max_length=10)
    requester_id: str | None = Field(None, max_length=40)


class QuantitySuggestionResponse(BaseModel):
    """One issued suggestion, its basis, and the config it was computed
    under. The config snapshot is part of the response, not a lookup the
    client repeats: a suggestion has to be explicable against the thresholds
    that were in force when it was made, not today's."""

    model_config = ConfigDict(from_attributes=True)

    suggestion_id: str
    session_id: str | None
    material: str
    plant: str
    reservation_number: str | None
    reservation_item: str | None
    requester_id: str | None

    requested_quantity: Decimal
    suggested_quantity: Decimal | None
    direction: SuggestionDirection
    reason_code: SuggestionReason
    reason_text: str
    reason_source: str
    reason_prompt_id: str | None
    reason_prompt_version: int | None
    reason_model: str | None

    average_monthly_consumption: Decimal
    stock_on_hand: Decimal
    open_po_quantity: Decimal
    consumption_count_12m: int

    plan_window_months: Decimal
    resulting_cover_months: Decimal | None
    plan_need_quantity: Decimal | None
    net_need_quantity: Decimal | None
    ceiling_quantity: Decimal | None

    cover_ceiling_months: Decimal | None
    minimum_history_count: int | None
    lookback_months: int

    accepted: bool | None
    accepted_at: datetime | None
    accepted_by: str | None

    calculated_at: datetime
    created_at: datetime | None


class QuantityJustificationRequest(BaseModel):
    reason_category: str = Field(..., min_length=1, max_length=60)
    free_text: str = Field(..., min_length=1, max_length=2000)


class QuantityJustificationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    justification_id: int
    suggestion_id: str
    reason_category: str
    free_text: str
    actor_id: str
    created_at: datetime


class QuantitySuggestionDetailResponse(QuantitySuggestionResponse):
    justifications: list[QuantityJustificationResponse] = []


class QuantityAcceptanceRequest(BaseModel):
    """FRS §8 counts a benefit only where the requester accepted the
    suggestion, so acceptance is recorded explicitly and never inferred from
    a matching quantity."""

    accepted: bool
