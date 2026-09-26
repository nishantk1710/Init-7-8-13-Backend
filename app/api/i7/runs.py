"""Read-only I07 pipeline run information.

Lists and reads the existing run tables (``i7_feature_run``,
``i7_forecast_run``, ``i7_inventory_run``, ``i7_oar_run``) exactly as Phases
3-6 wrote them. This module runs no pipeline stage -- it never calls
``build_features`` / ``run_forecasting`` / ``run_inventory_calculations`` /
``run_oar_similarity``; those are triggered by an operator or a scheduled job,
never by a GET request.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session
from app.models.i7_features import FeatureBuildRun
from app.models.i7_forecast import ForecastRun
from app.models.i7_inventory import InventoryRun
from app.models.i7_oar import OarRun
from app.schemas.i7.errors import not_found
from app.schemas.i7.runs import RunDetail, RunListResponse, RunSummary, RunType

router = APIRouter(tags=["i7-runs"])

_MODELS: dict[RunType, type] = {
    RunType.FEATURE: FeatureBuildRun,
    RunType.FORECAST: ForecastRun,
    RunType.INVENTORY: InventoryRun,
    RunType.OAR: OarRun,
}

_RECORD_COUNT_FIELD: dict[RunType, str] = {
    RunType.FEATURE: "features_built",
    RunType.FORECAST: "forecasts_written",
    RunType.INVENTORY: "calculations_written",
    RunType.OAR: "targets_evaluated",
}


def _to_summary(run_type: RunType, row) -> RunSummary:
    return RunSummary(
        run_type=run_type,
        run_id=row.id,
        status=row.status,
        policy_id=row.policy_id,
        policy_version=row.policy_version,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def _to_detail(run_type: RunType, row) -> RunDetail:
    return RunDetail(
        run_type=run_type,
        run_id=row.id,
        status=row.status,
        policy_id=row.policy_id,
        policy_version=row.policy_version,
        formula_version=getattr(row, "formula_version", None),
        algorithm_version=getattr(row, "algorithm_version", None),
        feature_run_id=getattr(row, "feature_run_id", None),
        forecast_run_id=getattr(row, "forecast_run_id", None),
        inventory_run_id=getattr(row, "inventory_run_id", None),
        target_quantile=(
            str(row.target_quantile) if getattr(row, "target_quantile", None) is not None else None
        ),
        service_level_configured=getattr(row, "service_level_configured", None),
        max_stock_strategy=getattr(row, "max_stock_strategy", None),
        embedding_model_version=getattr(row, "embedding_model_version", None),
        records_written=getattr(row, _RECORD_COUNT_FIELD[run_type], None),
        error=row.error,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


@router.get(
    "/runs",
    response_model=RunListResponse,
    summary="List I07 pipeline runs",
    description="The most recent runs of each pipeline stage (feature, "
    "forecast, inventory, OAR). Read-only -- lists existing run rows, never "
    "triggers a new run.",
)
def list_runs(
    session: Annotated[Session, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=50, description="Most recent runs per run type")] = 10,
) -> RunListResponse:
    items: list[RunSummary] = []
    for run_type, model in _MODELS.items():
        rows = session.execute(
            select(model).order_by(model.id.desc()).limit(limit)
        ).scalars().all()
        items.extend(_to_summary(run_type, row) for row in rows)
    return RunListResponse(items=items)


@router.get(
    "/runs/{run_type}/{run_id}",
    response_model=RunDetail,
    summary="Get one I07 run",
    description="Run ids are type-specific: the four run tables have "
    "independent id sequences, so run_type is required to disambiguate.",
    responses={404: {"description": "Run not found"}},
)
def get_run(
    run_type: RunType, run_id: int, session: Annotated[Session, Depends(get_session)]
) -> RunDetail:
    model = _MODELS[run_type]
    row = session.get(model, run_id)
    if row is None:
        raise not_found(
            "RUN_NOT_FOUND", "Run was not found.", run_type=run_type.value, run_id=run_id
        )
    return _to_detail(run_type, row)
