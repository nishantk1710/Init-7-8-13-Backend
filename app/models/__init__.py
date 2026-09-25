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
from app.models.i7_forecast import Forecast, ForecastBacktestPath, ForecastRun, SegmentModelDecision
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
from app.models.i13_act_exception import (
    ActConfirmationRecord,
    ActExceptionEventRecord,
    ActExceptionRecord,
    ActNotificationRecord,
)
from app.models.i13_consumption_attribution import ConsumptionAttributionRecord
from app.models.i13_quantity_suggestion import QuantityJustificationRecord, QuantitySuggestionRecord
from app.models.i13_reclassification import ReclassificationCandidateMart
from app.models.i13_session_link import SessionReservationLink, UatReservationSgtxt
from app.models.i13_watch_mart import WatchMetricMart
from app.models.ingest_watermark import IngestWatermark
from app.models.ingestion import IngestionRun
from app.models.serving import MaterialPlant

# --- Initiative-owned tables ----------------------------------------------
#
# These live in their own packages, per the scope note above, but they must be
# imported HERE or Alembic autogenerate cannot see them and their tables
# silently never get a migration.
#
# Imported as MODULES rather than `from ... import RepairAttestation`, and that
# is not a style choice -- it is what breaks a genuine import cycle. An
# initiative model imports Base from app.models.base, and importing any
# submodule of a package runs that package's __init__ first. So:
#
#     app.initiatives.i8.models      starts loading
#       -> from app.models.base import Base
#         -> runs THIS file
#           -> from app.initiatives.i8.models import RepairAttestation
#              ... which is half-loaded and has no RepairAttestation yet. Boom.
#
# `import x.y.z` only binds the module object, which already exists in
# sys.modules by then, so it succeeds from either direction. The import is for
# the side effect of registering the table with Base.metadata; the names are
# imported from their own modules, not from here.
import app.initiatives.i8.models  # noqa: F401,E402  (registers i8_attestation)

# The W7 assistant spine. Shared rather than initiative-owned -- one session
# namespace serves both I08 FR-8 and I13 FR-4, because the BAdI hands the
# session ID to one field on one reservation and two namespaces would make it
# unreadable. Imported as a module for the same import-cycle reason as above.
import app.assistant.models  # noqa: F401,E402  (registers assistant_session and friends)

__all__ = [
    "ActConfirmationRecord",
    "ActExceptionEventRecord",
    "ActExceptionRecord",
    "ActNotificationRecord",
    "Base",
    "ConsumptionAttributionRecord",
    "IngestionRun",
    "QuantityJustificationRecord",
    "QuantitySuggestionRecord",
    "SessionReservationLink",
    "UatReservationSgtxt",
    "ReclassificationCandidateMart",
    "WatchMetricMart",
]
