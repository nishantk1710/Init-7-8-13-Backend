"""SAP adoption reconciliation API schema.

Computed on read from ``app.initiatives.i7.recommendations.adoption``, then
persisted to ``i7_sap_adoption`` as a side effect (FR-9's "recommendation
ledger") -- see ``app/api/i7/adoption.py``. Still read-only: on the current
extract the evaluator's real provider (``RawChangeDocumentProvider``) finds
no matching CDHDR/CDPOS evidence for any material-plant, so every result
still resolves to UNKNOWN, but that is a fact about this data delivery, not
a stub in this code path.
"""

from datetime import date

from pydantic import BaseModel, ConfigDict


class AdoptionFieldRow(BaseModel):
    """One field an adoption check looked at: what was approved, what SAP held
    before and holds now, and when it changed."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    expected: str | None = None
    before: str | None = None
    now: str | None = None
    changed_on: date | None = None
    changed_time: str | None = None
    days_after: int | None = None
    """Whole days from approval (else the recommendation date) to the change."""

    matched: bool | None = None
    """``None`` when SAP recorded nothing about this field."""


class AdoptionResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    status: str
    """ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED / UNKNOWN -- exactly Phase 7's
    ``AdoptionStatus`` values, never collapsed to a generic failure."""

    expected: dict[str, str]
    observed: dict[str, str]
    matched_fields: list[str]
    mismatched_fields: list[str]
    detail: str

    check_type: str = ""
    fields: list[AdoptionFieldRow] = []
    approved_on: date | None = None
    window_ends_on: date | None = None
    window_open: bool = True
    pending: bool = False
    """UNKNOWN while the tracking window is still open -- waiting for SAP."""

    is_conversion_adoption: bool = False
    """Whether this result is FR-9's OAR-conversion check (ND/PD -> VB with
    MINBE and MABST populated) rather than a normal-path parameter check --
    distinct concepts (see ``adoption.py``'s two evaluator functions), never
    conflated. A caller must not read a parameter-adoption ADOPTED/UNKNOWN
    result as evidence about conversion adoption or vice versa."""


class AdoptionListItem(BaseModel):
    """One recommendation's adoption result, for the portfolio-wide list."""

    model_config = ConfigDict(frozen=True)

    recommendation_id: str
    sap_material_number: str
    sap_plant_code: str
    status: str
    is_conversion_adoption: bool
    detail: str
    check_type: str = ""
    fields: list[AdoptionFieldRow] = []
    approved_on: date | None = None
    window_ends_on: date | None = None
    window_open: bool = True
    pending: bool = False


class AdoptionListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[AdoptionListItem]
    total: int
    page: int
    page_size: int


class AdoptionStatusCount(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
    count: int


class AdoptionSummary(BaseModel):
    """Adoption counts over every recommendation with a value (the newest
    one for each material-plant, optionally for one plant).

    ``total_evaluated`` is that whole set, not only the ones someone has
    opened. ``unknown_count`` is everything SAP has no change evidence for
    -- shown to people as Pending -- so the four counts always add up to
    ``total_evaluated``."""

    model_config = ConfigDict(frozen=True)

    total_evaluated: int
    by_status: list[AdoptionStatusCount]
    adopted_count: int
    partially_adopted_count: int
    not_adopted_count: int
    unknown_count: int
    adoption_rate_percentage: float | None
    """``(adopted_count + partially_adopted_count) / known_count * 100``,
    where ``known_count = total_evaluated - unknown_count`` -- i.e. the rate
    among recommendations SAP evidence actually exists for. ``None`` when
    ``known_count`` is 0 (including when every evaluated row is UNKNOWN, the
    current state of this extract) -- an all-UNKNOWN portfolio must never
    report a 0% rate, which would misrepresent "nothing observed yet" as
    "observed and none adopted"."""
