"""Purchase Requisition event stub.

Connectivity stub only: it accepts a JSON body, logs that an event arrived, and
returns 202. It does not persist, classify, call SAP, or route into I07/I08/I13.

The payload is intentionally generic -- the SAP event contract is not confirmed
yet, so this must not harden into an invented schema.
"""

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


class PREvent(BaseModel):
    """Open envelope: any JSON object is accepted and preserved as-is."""

    model_config = ConfigDict(extra="allow")


class PREventAccepted(BaseModel):
    status: str


@router.post(
    "/pr",
    response_model=PREventAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Accept a PR event (stub)",
)
def receive_pr_event(event: PREvent) -> PREventAccepted:
    payload: dict[str, Any] = event.model_dump()

    # Log the shape at INFO and the contents only at DEBUG: until the SAP
    # contract is agreed we cannot assume the body is free of sensitive fields.
    logger.info("PR event received (fields=%d, keys=%s)", len(payload), sorted(payload))
    logger.debug("PR event payload: %s", payload)

    return PREventAccepted(status="received")
