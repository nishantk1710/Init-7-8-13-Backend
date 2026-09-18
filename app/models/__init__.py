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
from app.models.i13_act_exception import (
    ActConfirmationRecord,
    ActExceptionEventRecord,
    ActExceptionRecord,
    ActNotificationRecord,
)
from app.models.i13_consumption_attribution import ConsumptionAttributionRecord
from app.models.i13_reclassification import ReclassificationCandidateMart
from app.models.i13_watch_mart import WatchMetricMart
from app.models.ingestion import IngestionRun

__all__ = [
    "ActConfirmationRecord",
    "ActExceptionEventRecord",
    "ActExceptionRecord",
    "ActNotificationRecord",
    "Base",
    "ConsumptionAttributionRecord",
    "IngestionRun",
    "ReclassificationCandidateMart",
    "WatchMetricMart",
]
