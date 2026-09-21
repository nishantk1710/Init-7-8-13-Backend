"""Request and response models for /api/assistant.

camelCase on the wire, snake_case in Python, via the same alias generator the
I08 schemas use -- the frontend consumes both and should not have to remember
which module a field came from.

One shape rule worth stating: **quantities are strings.** A Decimal serialised
as a JSON number goes through a float on the way back in, and 2.1 returns as
2.0999999999999996. These values reach an append-only record that a compliance
engine reads, so they travel as text and are parsed as Decimals.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field as PydanticField
from pydantic.alias_generators import to_camel


class AssistantModel(BaseModel):
    """Base: snake_case in Python, camelCase on the wire."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# --- the conversation ------------------------------------------------------


class ChoiceModel(AssistantModel):
    value: str
    label: str
    description: str | None = None


class FieldModel(AssistantModel):
    name: str
    label: str
    type: str
    required: bool
    options: list[ChoiceModel] = []
    help_text: str | None = None
    default: Any = None


class StepModel(AssistantModel):
    """One thing the assistant says, and what it expects back.

    ``facts`` is the assessment card rendered above the question. ``footnote``
    holds the caveats -- shown under the question rather than folded into it,
    because a sentence that hedges every clause is unreadable.
    """

    id: str
    kind: Literal["message", "choice", "form", "terminal"]
    prompt: str
    choices: list[ChoiceModel] = []
    fields: list[FieldModel] = []
    facts: dict[str, Any] | None = None
    footnote: str | None = None
    session_id: str | None = None


class StartSessionRequest(AssistantModel):
    """What the BAdI pop-up (or the platform) sends to open the assistant.

    Note what is **not** here: the requester. Identity is taken from the caller
    -- today an ``X-Actor-Id`` header, tomorrow an Entra token -- and never from
    the body. A session whose owner is self-declared is not an audit record.
    """

    material_id: str
    plant: str
    quantity: str | None = PydanticField(
        default=None,
        description=(
            "What the requester was about to reserve, if known. Optional: the "
            "pop-up may fire before a quantity is entered, and a defaulted zero "
            "would be indistinguishable from a real one."
        ),
    )
    origin: Literal["BADI", "PLATFORM"] = "PLATFORM"


class RoutingModel(AssistantModel):
    """Why this material got the flow it got -- or got none.

    Served even when no session was minted. A requester who sees nothing asks
    why, and "the assistant has nothing to say about this part" is an answer.
    """

    flow: Literal["i08", "i13", "none"]
    material_id: str
    plant: str
    eighty_series: bool
    material_scope: str
    mrp_type: str | None = None
    also_matched: str | None = None
    reason: str


class StartSessionResponse(AssistantModel):
    """``session`` and ``step`` are null together, and only when out of scope.

    That is a **200**, not an error. The assistant has no opinion about a
    consumable, and minting a session to record silence would fill an
    append-only table with it.
    """

    routing: RoutingModel
    session_id: str | None = None
    expires_at: datetime | None = None
    step: StepModel | None = None


class AnswerRequest(AssistantModel):
    """The answer to the current step.

    A free-form map because a form step's fields are defined by the script, not
    by this schema. It is validated against the step it answers
    (``app.assistant.turns.validate``), which is the only place that knows what
    was asked.
    """

    answer: dict[str, Any] = {}


class AnswerResponse(AssistantModel):
    session_id: str
    step: StepModel


# --- reading a session back ------------------------------------------------


class TurnModel(AssistantModel):
    sequence: int
    step_id: str
    step_kind: str
    question: str
    answer: dict[str, Any] | None = None
    actor: str
    answered_at: datetime


class PlanModel(AssistantModel):
    """A captured consumption plan.

    ``reservationNumber`` is null until FR-8 links it, which is the normal state
    and not a gap: the assistant runs while the reservation is being created, so
    it has no number yet.
    """

    id: str
    material: str
    plant: str
    purpose: str
    planned_quantity: str
    window_start: date | None = None
    window_end: date | None = None
    cost_centre: str | None = None
    order_number: str | None = None
    status: str
    reservation_number: str | None = None
    reservation_item: str | None = None
    captured_by: str
    captured_at: datetime


class SuggestionModel(AssistantModel):
    """``suggestedQuantity`` is null when no suggestion could be made.

    Null is **not** zero: "we suggest nothing" and "we suggest none" are
    opposite instructions.
    """

    id: str
    material: str
    plant: str
    requested_quantity: str
    suggested_quantity: str | None = None
    accepted_quantity: str
    suggestion_reason: str
    months_of_cover: str | None = None
    cover_ceiling_months: str
    lookback_months: int
    min_history_consumptions: int
    consumption_count: int
    is_override: bool
    suggested_at: datetime


class JustificationModel(AssistantModel):
    id: str
    session_id: str | None = None
    exception_id: str | None = None
    kind: str
    reason_category: str
    free_text: str
    material_id: str
    plant: str
    author: str
    recorded_at: datetime


class SessionTraceResponse(AssistantModel):
    """The FR-8 demo: one session, everything it produced.

    ``assessment`` is the advice **as served**, replayed from what was stored
    rather than recomputed. Recomputing would answer a different question -- the
    register and the stock both move -- and the point of the record is that it
    does not.
    """

    session_id: str
    flow: str
    outcome: Literal["OPEN", "COMPLETED", "ABANDONED"]
    material_id: str
    plant: str
    requested_quantity: str | None = None
    requester: str
    origin: str
    issued_at: datetime
    expires_at: datetime
    expired: bool
    routing_reason: str
    assessment: dict[str, Any]
    narrative: str | None = None
    turns: list[TurnModel] = []
    plans: list[PlanModel] = []
    quantity_suggestions: list[SuggestionModel] = []
    justifications: list[JustificationModel] = []
    linkage_note: str


class SessionSummary(AssistantModel):
    session_id: str
    flow: str
    outcome: str
    material_id: str
    plant: str
    requester: str
    origin: str
    issued_at: datetime
    expires_at: datetime
    turns: int


class SessionListResponse(AssistantModel):
    items: list[SessionSummary]
    total: int
    note: str


# --- justifications, both initiatives --------------------------------------


class JustificationRequest(AssistantModel):
    """``author`` is absent on purpose -- it comes from the caller."""

    kind: Literal["NEW_ACQUISITION", "QUANTITY_OVERRIDE", "PLAN_BREACH", "NO_PLAN"]
    reason_category: str
    free_text: str
    material_id: str
    plant: str
    session_id: str | None = None
    exception_id: str | None = None


class JustificationListResponse(AssistantModel):
    items: list[JustificationModel]
    total: int
    reason_categories: list[str]
    note: str
