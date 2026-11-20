"""Request and response models for /api/assistant/requests.

Same conventions as :mod:`app.api.assistant.schemas`: camelCase on the wire,
quantities as strings.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import Field as PydanticField

from app.api.assistant.schemas import AssistantModel, LinkedReservationModel, SessionDeclarationModel


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


class UnitPriceModel(AssistantModel):
    """The plant's replacement price as I07 reads it (MBEW VERPR / PEINH), in ZAR."""

    price: str | None = None
    """Null: no usable price -- ``missing`` says why, ``note`` in words."""
    currency: str | None = None
    missing: str | None = None
    note: str | None = None


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
    unit_price: UnitPriceModel | None = None
    """OAR lines only: what a saving on this line is valued at."""


class ReasonCategoryModel(AssistantModel):
    value: str
    label: str


class PreviewResponse(AssistantModel):
    lines: list[PreviewLineModel]
    max_lines: int
    reason_categories: list[ReasonCategoryModel]
    """The justification categories submit accepts -- the configured list, so
    the screen offers exactly what the conversation would."""
    fault_categories: list[str] = []
    """The configured fault categories an I08 line's declaration accepts."""
    recommendations: list[ReasonCategoryModel] = []
    """The three assessments a declaration may record, with their labels."""


# --- create ----------------------------------------------------------------------


class LineDeclarationModel(AssistantModel):
    """The faulty unit declared on an I08 line -- the conversation's declaration form."""

    quantity: str | int | float = 1
    fault_category: str = PydanticField(max_length=64)
    recommendation: str = PydanticField(max_length=64)

    # Retired 3-Nov-2026, when the form stopped asking for them: still
    # accepted, so a draft saved before then submits, and never passed on.
    # The declarer is the signed-in requester now.
    condition_description: str | None = PydanticField(default=None, max_length=4000)
    attestor_name: str | None = PydanticField(default=None, max_length=128)
    evidence_reference: str | None = PydanticField(default=None, max_length=256)

    def as_answer(self) -> dict[str, Any]:
        """Keyed as the ``i08_declaration`` form's fields."""
        return {
            "quantity": str(self.quantity),
            "fault_category": self.fault_category,
            "recommendation": self.recommendation,
        }


class LineAnswersModel(AssistantModel):
    """One material's answers. Only what its flow asks needs to be present."""

    material_id: str = PydanticField(max_length=40)
    plant: str = PydanticField(max_length=8)
    department: str | None = PydanticField(default=None, max_length=64)
    """Deprecated: send ``department`` on the request. Accepted while it agrees."""
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
    declaration: LineDeclarationModel | None = None
    """I08 only: declare the faulty unit coming off. Absent: nothing declared."""
    required_quantity: str | None = PydanticField(default=None, max_length=20)
    """OAR: the quantity needed, typed before the suggestion was shown. What a
    saving is measured from; absent, none is recorded."""
    quantity_choice: Literal["suggested", "own"] | None = None
    """OAR: which number was locked. ``quantity`` must then be that number."""


class CreateRequestBody(AssistantModel):
    idempotency_key: str = PydanticField(min_length=8, max_length=64)
    """One per draft. Resubmitting the same draft returns the same request."""
    plant: str = PydanticField(max_length=8)
    requested_for: str | None = PydanticField(default=None, max_length=128)
    department: str | None = PydanticField(default=None, max_length=64)
    """The request's one department code; every line is recorded against it."""
    department_note: str | None = PydanticField(default=None, max_length=200)
    """Optional free text beside the department code; never checked."""
    origin: Literal["BADI", "PLATFORM"] = "PLATFORM"
    lines: list[LineAnswersModel] = PydanticField(min_length=1, max_length=50)


# --- reading back ----------------------------------------------------------------


class JustificationBriefModel(AssistantModel):
    kind: str
    reason_category: str
    free_text: str


class SavingModel(AssistantModel):
    """What the quantity suggestion saved on one OAR line -- an estimate at submit."""

    required_quantity: str
    suggested_quantity: str | None = None
    final_quantity: str
    quantity_choice: Literal["suggested", "own"]
    units_saved: str | None = None
    """Null: no suggestion could be made, so nothing is counted."""
    unit_price: str | None = None
    currency: str | None = None
    value_saved: str | None = None
    """Null without a price; ``price_note`` says why."""
    price_note: str | None = None


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
    department_name: str | None = None
    department_note: str | None = None
    decision: str | None = None
    planned_quantity: str | None = None
    suggested_quantity: str | None = None
    accepted_quantity: str | None = None
    window_start: date | None = None
    window_end: date | None = None
    purpose: str | None = None
    justification: JustificationBriefModel | None = None
    saving: SavingModel | None = None
    """OAR lines submitted with a required quantity (from 3 Nov 2026)."""
    linked_reservations: list[LinkedReservationModel] = []
    declaration: SessionDeclarationModel | None = None
    """Repairable lines: the condition declaration made on the line -- the
    newest, should there be more than one -- as the session trace shows it
    (4-Nov-2026). Null when none was made, and always on an OAR line, which has
    a window and a purpose instead."""


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
    department: str | None = None
    """The request's department. Null for a request from before 21 Oct whose
    lines named different ones -- ``departments`` lists them."""
    department_name: str | None = None
    """``department``'s name from the department list, if it has one."""
    department_note: str | None = None
    """Free text typed beside the department, if any."""
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
    department: str | None = None
    department_name: str | None = None
    departments: list[str]


class RequestListResponse(AssistantModel):
    items: list[RequestSummaryModel]


# --- upload ----------------------------------------------------------------------


class UploadResponse(AssistantModel):
    """An uploaded template, read and checked. Nothing is stored: each material
    is then previewed as one added by hand. The department is not in the file
    (TL, 20 Nov): it is picked on the request itself."""

    plant: str
    materials: list[str]
    """In file order, duplicates removed."""
    row_count: int
    duplicates: list[str]
    max_lines: int


# --- link reservation ------------------------------------------------------------


class LinkJobStepModel(AssistantModel):
    """One step of a Link reservation job, as the progress window shows it."""

    key: str
    label: str
    # pending | active | done | failed | skipped
    status: str
    started_at: str | None = None
    finished_at: str | None = None
    detail: str | None = None


class LinkJobResponse(AssistantModel):
    """A Link reservation job: where it is, what each step did, how it ended."""

    id: int
    request_id: str
    # queued | running | succeeded | failed | abandoned
    status: str
    # The step being worked on: queue | delta | link | i13 | i08 | done
    step: str
    steps: list[LinkJobStepModel]
    requested_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    links: int | None = None
    error: str | None = None
    created: bool
    """False when the press found this request's job already queued or running."""
