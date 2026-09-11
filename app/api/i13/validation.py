"""GET /api/i13/validation -- local reconciliation against reference reports.

No real ZMM065 / 30-Day GR Report export exists in this repository yet, so
reference counts are accepted as optional query params; without them the
result is ``REFERENCE_UNAVAILABLE`` (see ``initiatives/i13/reconciliation.py``).
"""

from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from app.api.i13.deps import get_data_dir
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.exceptions import build_exception_queue
from app.initiatives.i13.ledger import build_utilisation_ledger
from app.initiatives.i13.models import ExceptionType
from app.initiatives.i13.reconciliation import reconcile
from app.integrations.sap.gateway import SapGateway, get_sap_gateway
from app.schemas.i13 import ReconciliationSourceResult, ValidationResponse

router = APIRouter()


@router.get("/validation", response_model=ValidationResponse)
def get_validation(
    zmm065_reference_count: int | None = Query(None, description="Reference row count from the ZMM065 report"),
    gr_30_day_reference_count: int | None = Query(
        None, description="Reference count from the 30-Day GR Report"
    ),
    gateway: SapGateway = Depends(get_sap_gateway),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
) -> ValidationResponse:
    ledger_entries = build_utilisation_ledger(gateway)
    exceptions = build_exception_queue(gateway, config, data_dir)
    gr_not_issued_count = sum(1 for item in exceptions if item.type is ExceptionType.GR_NOT_ISSUED_30_DAY)

    results = [
        reconcile(
            "ZMM065",
            len(ledger_entries),
            zmm065_reference_count,
            tolerance_pct=config.reconciliation.tolerance_pct,
        ),
        reconcile(
            "30-Day GR Report",
            gr_not_issued_count,
            gr_30_day_reference_count,
            tolerance_pct=config.reconciliation.tolerance_pct,
        ),
    ]
    return ValidationResponse(
        tolerance_pct=Decimal(str(config.reconciliation.tolerance_pct)),
        results=[ReconciliationSourceResult.model_validate(result) for result in results],
    )
