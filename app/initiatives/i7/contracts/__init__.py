"""Canonical I07 data contracts.

The boundary between "where data came from" and "what I07 does with it".
Everything above this line -- features, classification, forecasting,
calculation, recommendation -- speaks only these types, so the July extract,
live OData and a future RFC feed are interchangeable to the domain.

The rule that keeps this true: **nothing in this package imports
``app.integrations.sap`` or reads a ``raw_*`` table.** Adapters map sources onto
these contracts; the contracts never reach back.
"""

from app.initiatives.i7.contracts.consumption import ConsumptionObservation, ConsumptionSeries
from app.initiatives.i7.contracts.enums import (
    ConfidenceGrade,
    Criticality,
    DataQualityGrade,
    DemandPattern,
    LeadTimeSource,
    PolicyStatus,
    RecommendationStatus,
    RiskLevel,
    ScopeDecision,
)
from app.initiatives.i7.contracts.identity import (
    MaterialIdentity,
    MaterialPlantKey,
    PlantIdentity,
    PolicyVersionRef,
)
from app.initiatives.i7.contracts.leadtime import PurchaseOrderObservation
from app.initiatives.i7.contracts.material import MaterialAttributes
from app.initiatives.i7.contracts.recommendation import (
    CalculationTrace,
    ChampionChallenger,
    ConsumptionPoint,
    LeadTimeProfile,
    ModelProfile,
    OarColdStartGuidance,
    Recommendation,
    RecommendationFactor,
    StockParameters,
    WorkflowStep,
)

__all__ = [
    "CalculationTrace",
    "ChampionChallenger",
    "ConfidenceGrade",
    "ConsumptionObservation",
    "ConsumptionPoint",
    "ConsumptionSeries",
    "Criticality",
    "DataQualityGrade",
    "DemandPattern",
    "LeadTimeProfile",
    "LeadTimeSource",
    "MaterialAttributes",
    "MaterialIdentity",
    "MaterialPlantKey",
    "ModelProfile",
    "OarColdStartGuidance",
    "PlantIdentity",
    "PolicyStatus",
    "PolicyVersionRef",
    "PurchaseOrderObservation",
    "Recommendation",
    "RecommendationFactor",
    "RecommendationStatus",
    "RiskLevel",
    "ScopeDecision",
    "StockParameters",
    "WorkflowStep",
]
