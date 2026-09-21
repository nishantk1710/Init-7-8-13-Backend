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
from app.models.ingest_watermark import IngestWatermark
from app.models.ingestion import IngestionRun
from app.models.serving import MaterialPlant

__all__ = ["Base", "IngestionRun", "IngestWatermark", "MaterialPlant"]
