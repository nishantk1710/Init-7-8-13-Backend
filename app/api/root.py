"""Service index at ``/``.

Mounted outside the ``/api`` prefix. Without it a browser hitting the bare host
gets FastAPI's default ``{"detail":"Not Found"}``, which reads as a broken
service. This answers "is it up, and where do I go next?".
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["root"])


class ServiceInfo(BaseModel):
    service: str
    version: str
    status: str
    message: str
    docs_url: str
    health_url: str


@router.get("/", response_model=ServiceInfo, summary="Service index")
def get_root() -> ServiceInfo:
    settings = get_settings()
    return ServiceInfo(
        service=settings.app_name,
        version=settings.app_version,
        status="ok",
        message=f"{settings.app_name} is running. See docs_url for the API.",
        docs_url="/docs",
        health_url=f"{settings.api_prefix}/health",
    )
