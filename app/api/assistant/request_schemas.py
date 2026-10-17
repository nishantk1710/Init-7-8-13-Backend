"""Request and response models for /api/assistant/requests.

Same conventions as :mod:`app.api.assistant.schemas`: camelCase on the wire,
quantities as strings.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field as PydanticField

from app.api.assistant.schemas import AssistantModel, LinkedReservationModel


# --- preview ---------------------------------------------------------------------


class PreviewMaterial(AssistantModel):
    material_id: str = PydanticField(max_length=40)
    plant: str = PydanticField(max_length=8)


class PreviewRequest(AssistantModel):
    materials: list[PreviewMaterial] = PydanticField(min_length=1, max_length=50)


class SuggestedModel(AssistantModel):
    quantity: str | None
    """Null: no suggestion can be made -- a reason is then asked for any quantity."""
    reason: str
    window_start: date | None = None
    window_end: date | None = None
    window_months: int | None = None


class PreviewLineModel(AssistantModel):
    material_id: str
    material_description: str | None = None
    plant: str
    kind: Literal["i13", "i08", "out_of_scope", "data_gap", "invalid"]
    reason: str
    facts: dict[str, Any] | None = None
    unit_exists: bool | None = None
    suggestion: SuggestedModel | None = None
    also_matched: str | None = None


class PreviewResponse(AssistantModel):
    lines: list[PreviewLineModel]
    max_lines: int


# --- create ----------------------------------------------------------------------


class LineAnswersModel(AssistantModel):
    """One material's answers. Only what its flow asks needs to be present."""

    material_id: str = PydanticField(max_length=40)
    plant: str = PydanticField(max_length=8)
    department: str | None = PydanticField(default=None, max_length=64)
    decision: Literal["proceed", "not_needed", "use_existing", "proceed_new"] | None = None
    quantity: str | None = PydanticField(default=None, max_length=20)
    window_start: str | None = PydanticField(default=None, max_length=10)
    window_end: str | None = PydanticField(default=None, max_length=10)
    purpose: str | None = PydanticField(default=None, max_length=2000)
    reason_category: str | None = PydanticField(default=None, max_length=64)
    free_text: str | None = PydanticField(default=None, max_length=4000)
    seen_suggested_quantity: str | None = PydanticField(default=None, max_length=20)
    """What the screen showed as the suggestion, null for "none could be made".
    Send it for every OAR line: a figure that moved since is refused (409)."""


class CreateRequestBody(AssistantModel):
    idempotency_key: str = PydanticField(min_length=8, max_length=64)
    """One per draft. Resubmitting the same draft returns the same request."""
    plant: str = PydanticField(max_length=8)
    requested_for: str | None = PydanticField(default=None, max_length=128)
    origin: Literal["BADI", "PLATFORM"] = "PLATFORM"
    lines: list[LineAnswersModel] = PydanticField(min_length=1, max_length=50)


# --- reading back ----------------------------------------------------------------


class JustificationBriefModel(AssistantModel):
    kind: str
    reason_category: str
    free_text: str


class RequestLineModel(AssistantModel):
    line_no: int
    reference: str
    """What goes into the SAP item text (SGTXT): ``<request>/<n>``."""
    session_id: str
    flow: str
    outcome: str
    material_id: str
    material_description: str | None = None
    plant: str
    department: str | None = None
    decision: str | None = None
    planned_quantity: str | None = None
    suggested_quantity: str | None = None
    accepted_quantity: str | None = None
    window_start: date | None = None
    window_end: date | None = None
    purpose: str | None = None
    justification: JustificationBriefModel | None = None
    linked_reservations: list[LinkedReservationModel] = []


class SkippedModel(AssistantModel):
    material_id: str
    plant: str
    kind: str
    reason: str


class RequestResponse(AssistantModel):
    request_id: str
    plant: str
    requested_for: str | None = None
    requester: str
    origin: str
    issued_at: datetime
    departments: list[str]
    lines: list[RequestLineModel]
    skipped: list[SkippedModel]
    replayed: bool = False


class ConflictLineModel(AssistantModel):
    material_id: str
    seen_suggested_quantity: str | None = None
    suggested_quantity: str | None = None


class RequestSummaryModel(AssistantModel):
    request_id: str
    plant: str
    requested_for: str | None = None
    issued_at: datetime
    line_count: int
    skipped_count: int
    departments: list[str]


class RequestListResponse(AssistantModel):
    items: list[RequestSummaryModel]
