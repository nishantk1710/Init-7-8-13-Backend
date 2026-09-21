"""Persistence models.

Every model module must be imported here. Alembic autogenerate compares the
database against ``Base.metadata``, and a model that nothing imports is absent
from that metadata -- so its table silently never gets a migration.

Scope note: the raw SAP landing tables belong here, in the shared foundation,
not inside the initiative packages. Three initiatives read the same extracts;
three private copies of an MSEG table would diverge within a week. Tables
*derived* from those, and anything specific to one initiative, belong to that
initiative.
"""

from app.models.base import Base
from app.models.i7_features import FeatureBuildRun, MaterialFeature
from app.models.i7_forecast import Forecast, ForecastRun, SegmentModelDecision
from app.models.i7_inventory import InventoryCalculation, InventoryRun
from app.models.i7_oar import OarNeighbour, OarRun, OarTargetResult
from app.models.i7_recommendation import (
    ApprovalLedgerEntry,
    Recommendation,
    RecommendationVersion,
    SapAdoptionResult,
    SapExecutionEvidence,
)
from app.models.i7_policy import PolicyVersion
from app.models.i7_reporting import QuarterlyReportRecord
from app.models.i7_staging import (
    StagedConsumption,
    StagedMaterial,
    StagedMaterialPlant,
    StagedPurchaseOrder,
    StagingRejection,
    StagingRun,
)
from app.models.ingestion import IngestionRun

__all__ = [
    "Base",
    "FeatureBuildRun",
    "Forecast",
    "ForecastRun",
    "IngestionRun",
    "InventoryCalculation",
    "InventoryRun",
    "MaterialFeature",
    "OarNeighbour",
    "OarRun",
    "OarTargetResult",
    "ApprovalLedgerEntry",
    "Recommendation",
    "RecommendationVersion",
    "SapAdoptionResult",
    "SapExecutionEvidence",
    "PolicyVersion",
    "QuarterlyReportRecord",
    "SegmentModelDecision",
    "StagedConsumption",
    "StagedMaterial",
    "StagedMaterialPlant",
    "StagedPurchaseOrder",
    "StagingRejection",
    "StagingRun",
]
