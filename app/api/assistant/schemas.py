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

    Note what is **not** here: who is operating the assistant. That identity is
    taken from the caller -- today an ``X-Actor-Id`` header, tomorrow an Entra
    token -- and never from the body. A session whose *author* is self-declared
    is not an audit record.

    ``requestedFor`` is not an exception to that rule. It names the person the
    part is for, typed by whoever is operating the assistant, and it lands in a
    different column from the author for exactly that reason.

    Note also what is **no longer** here: ``quantity``. The number that matters
    is the planned quantity captured inside the conversation, against a stated
    purpose and a window. Asking for one at the door was asking the same question
    twice, and the answer given first was the one nobody had thought about.
    """

    material_id: str = PydanticField(max_length=40)
    plant: str = PydanticField(max_length=8)
    department: str | None = PydanticField(
        default=None,
        max_length=64,
        description=(
            "Which department the part is for -- the requester's, not the "
            "operator's. Optional: the BAdI pop-up carries a material and a "
            "plant and cannot supply this, so a session opened from SAP "
            "legitimately has none."
        ),
    )
    requested_for: str | None = PydanticField(
        default=None,
        max_length=128,
        description=(
            "Who the part is for, as typed by whoever is operating the "
            "assistant. Free text and NOT identity -- nobody verified it. "
            "Optional for the same reason department is."
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


class NarrativeModel(AssistantModel):
    """A model-written sentence, and the prompt behind it.

    The provenance is not optional decoration. This programme is human-gated and
    audited, and "the model said so" is not an acceptable account of where a
    sentence came from -- so the prompt id, its version and the deployment travel
    with the text to whoever is reading it.
    """

    text: str
    prompt_id: str | None = None
    prompt_version: int | None = None
    model: str | None = None


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
    narrative: NarrativeModel | None = None
    """The model-written phrasing of the advice, where one was served.

    Null whenever the narrative layer is off, unconfigured or failed -- all of
    which are normal. It is served **beside** ``step.facts`` and never instead of
    them: the deterministic assessment is the answer of record, and a client that
    rendered this in its place would be showing phrasing where a number belongs.

    It was stored on the session from the day the narrative was written and never
    returned here, so nobody in a live conversation had ever seen one. Only the
    trace showed it, to whoever read the audit record afterwards -- which is the
    one person it was not written for."""


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

    ``sessionId`` is the plan's origin, and FR-4 is explicit that a plan is
    stored *against* it -- "traceability from a plan back to the advice that
    shaped it". It was stored on the record and left off this model, so a caller
    reading a plan could not reach the conversation that produced it even though
    the list route already filters by it. With the reservation link blocked on
    ``RESB.BEDNR``, this is currently the ONLY link a plan has to anything.
    """

    id: str
    session_id: str
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
    department: str | None = None
    requested_for: str | None = None
    """Who the part was for. Null for a session opened before the field existed,
    or from SAP, which cannot supply one."""
    requested_quantity: str | None = None
    """Null on every session minted since the entry point stopped asking.
    Sessions from before that carry a real value, which is why the field stays.
    Null has always meant "not stated" here and never zero."""
    requester: str
    """Who **operated** the assistant. Served because this is the FR-8 evidence
    view and an audit record without its author is not one -- but no screen
    displays it: one coordinator opens every session, so it says the same thing
    on every row."""
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
    department: str | None = None
    requested_for: str | None = None
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
    reason_category: str = PydanticField(max_length=64)
    free_text: str
    material_id: str = PydanticField(max_length=40)
    plant: str = PydanticField(max_length=8)
    session_id: str | None = PydanticField(default=None, max_length=32)
    exception_id: str | None = PydanticField(default=None, max_length=120)


class JustificationListResponse(AssistantModel):
    items: list[JustificationModel]
    total: int
    reason_categories: list[str]
    note: str


# --- the free-text box (section 4.6) ---------------------------------------


class AskRequest(AssistantModel):
    question: str


class AskResponse(AssistantModel):
    """A deterministic answer, or a plain statement that there is none.

    ``answered`` is false for a question this assistant does not cover, and
    ``text`` then says so and lists what it does. A free-text box that silently
    does nothing teaches people it is broken; one that guesses teaches them it
    is unreliable.

    ``sources`` names the endpoints every number came from. Nothing here is
    generated -- no model is involved in this path at all.
    """

    intent: str
    answered: bool
    text: str
    sources: list[str] = []
    data: dict[str, Any] = {}
    suggestions: list[str] = []
    note: str
