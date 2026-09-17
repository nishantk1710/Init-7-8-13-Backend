"""SAP adoption reconciliation API schema.

Computed on read from ``app.initiatives.i7.recommendations.adoption`` -- the
``i7_sap_adoption`` table exists but nothing in Phase 7 writes to it yet
(there is no persisted adoption result to read), and adoption evaluation is
cheap and pure, so recomputing it per request is both correct and simpler than
introducing a write path this phase does not otherwise need. Still read-only:
the evaluator's ``SapStateProvider`` has exactly one implementation today
(``NoSapStateAvailable``), so this endpoint never reaches SAP and always
resolves to UNKNOWN on the current extract.
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
