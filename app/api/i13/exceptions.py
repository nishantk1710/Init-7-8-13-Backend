"""GET /api/i13/exceptions."""

from pathlib import Path

from fastapi import APIRouter, Depends, Query

from app.api.i13.deps import get_data_dir
from app.initiatives.i13.config import I13Config, get_i13_config
from app.initiatives.i13.exceptions import build_exception_queue
from app.integrations.sap.gateway import SapGateway, get_sap_gateway
from app.schemas.i13 import ExceptionResponse

router = APIRouter()


@router.get("/exceptions", response_model=list[ExceptionResponse])
def list_exceptions(
    plant: str | None = Query(None),
    material: str | None = Query(None),
    exception_type: str | None = Query(None),
    exception_status: str | None = Query(None, alias="status"),
    gateway: SapGateway = Depends(get_sap_gateway),
    config: I13Config = Depends(get_i13_config),
    data_dir: Path = Depends(get_data_dir),
) -> list[ExceptionResponse]:
    items = build_exception_queue(gateway, config, data_dir)
    if plant:
        items = [item for item in items if item.plant == plant]
    if material:
        items = [item for item in items if item.material == material]
    if exception_type:
        items = [item for item in items if item.type.value == exception_type.upper()]
    if exception_status:
        items = [item for item in items if item.status.value == exception_status.upper()]
    return [ExceptionResponse.model_validate(item) for item in items]
