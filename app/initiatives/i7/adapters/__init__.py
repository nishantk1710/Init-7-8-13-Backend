"""Source adapters -- the boundary between SAP's shape and I07's.

    July extract (raw_*) ──┐
    live OData ────────────┼──→ adapter ──→ canonical staging ──→ I07 domain
    RFC/BAPI ──────────────┘

Only this package knows SAP field names, extract column labels, movement-type
codes or the ``raw_*`` tables. Everything above it reads canonical contracts, so
Phase 12's live feed adds a module here and changes nothing downstream.

* ``field_map``        extract column -> SAP field -> canonical meaning
* ``ingestion_policy`` configurable ingestion choices (movement types, batching)
* ``validation``       parsing and rejection reasons
* ``extract``          the July/August adapter: raw -> staging
* ``repository``       staging -> canonical contracts, for Phase 3

The adapter transforms data. It makes no inventory decisions: no OAR
classification, no ADI, no forecasting. Fields the OAR rule needs (``DISMM``,
``MSTAE``) are staged as found, and the policy interprets them later.
"""

from app.initiatives.i7.adapters.extract import StagingResult, stage_extract
from app.initiatives.i7.adapters.ingestion_policy import (
    ConsumptionMovementPolicy,
    ExtractIngestionPolicy,
)
from app.initiatives.i7.adapters.repository import (
    consumption_series_for,
    iter_material_attributes,
    material_attributes_for,
    purchase_orders_for,
)

__all__ = [
    "ConsumptionMovementPolicy",
    "ExtractIngestionPolicy",
    "StagingResult",
    "consumption_series_for",
    "iter_material_attributes",
    "material_attributes_for",
    "purchase_orders_for",
    "stage_extract",
]
