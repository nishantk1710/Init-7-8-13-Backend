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
