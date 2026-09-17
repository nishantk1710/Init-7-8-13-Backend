"""I07 pipeline run API schemas.

Four run types exist, in four separate tables with independently-incrementing
ids (``i7_feature_run``, ``i7_forecast_run``, ``i7_inventory_run``,
``i7_oar_run``): a bare integer is ambiguous without knowing which table it
names, so ``run_type`` is part of the identity everywhere a run is addressed,
never inferred.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class RunType(StrEnum):
    FEATURE = "feature"
    FORECAST = "forecast"
    INVENTORY = "inventory"
    OAR = "oar"


class RunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_type: RunType
    run_id: int
    status: str
    policy_id: str
    policy_version: int
    started_at: datetime
    finished_at: datetime | None


class RunListResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[RunSummary]


class RunDetail(BaseModel):
    """Fields vary by run type; only those the row actually has are
    populated. Never invents a field a given run type does not persist."""

    model_config = ConfigDict(frozen=True)

    run_type: RunType
    run_id: int
    status: str
    policy_id: str
    policy_version: int
    formula_version: str | None = None
    algorithm_version: str | None = None
    feature_run_id: int | None = None
    forecast_run_id: int | None = None
    inventory_run_id: int | None = None
    target_quantile: str | None = None
    service_level_configured: bool | None = None
    max_stock_strategy: str | None = None
    embedding_model_version: str | None = None
    records_written: int | None = None
    """The run's own output count -- ``features_built`` / ``forecasts_written``
    / ``calculations_written`` / ``targets_evaluated``, whichever applies."""

    error: str | None = None
    started_at: datetime
    finished_at: datetime | None
