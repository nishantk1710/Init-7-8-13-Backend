"""Liveness endpoint.

Deliberately dependency-free: it must not touch a database, SAP, Azure, an LLM
or authentication, and it must work with no credentials configured. Its only
job is to prove the process is up and serving -- locally and on Azure App
Service.
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import get_settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str


@router.get("/health", response_model=HealthResponse, summary="Liveness check")
def get_health() -> HealthResponse:
    return HealthResponse(status="ok", service=get_settings().app_name)
