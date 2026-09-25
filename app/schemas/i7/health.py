"""I07 health schema. Deliberately dependency-free, matching
``app.api.health`` -- it must not touch the database, and must not claim SAP
connectivity that was never checked."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class I07HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    initiative: str
    status: str
    api_version: str
    timestamp: datetime
