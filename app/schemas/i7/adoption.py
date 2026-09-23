"""SAP adoption reconciliation API schema.

Computed on read from ``app.initiatives.i7.recommendations.adoption``, then
persisted to ``i7_sap_adoption`` as a side effect (FR-9's "recommendation
ledger") -- see ``app/api/i7/adoption.py``. Still read-only: on the current
extract the evaluator's real provider (``RawChangeDocumentProvider``) finds
no matching CDHDR/CDPOS evidence for any material-plant, so every result
still resolves to UNKNOWN, but that is a fact about this data delivery, not
a stub in this code path.
"""

from pydantic import BaseModel, ConfigDict


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
    """Portfolio-wide counts straight from the recommendation ledger
    (``i7_sap_adoption``) -- FR-9's persisted evaluation results, not a
    live re-evaluation of every recommendation on each call. Only reflects
    recommendations someone has actually viewed through the adoption
    endpoints so far (the ledger fills in as pages are viewed -- see
    ``app/api/i7/adoption.py``'s module docstring), never a claim about
    portfolio-wide adoption before any evaluation has happened."""

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
