"""Readiness endpoint -- can this instance actually serve requests?

Deliberately separate from ``/health``. Liveness answers "is the process up?"
and must never touch a dependency; readiness answers "are its dependencies
reachable?" and exists to touch them. Merging the two makes a database blip look
like a dead process, and a platform health probe will then restart a container
that was fine.

Each dependency reports one of three states, because each needs a different fix:

    ok             -- reachable
    not_configured -- its setting is empty
    unavailable    -- configured, but the check failed

The overall ``status`` is ``ready`` only when every dependency is ``ok``.
"""

from fastapi import APIRouter, Response, status
from pydantic import BaseModel

from app.core.db import DatabaseNotConfiguredError, check_connection
from app.core.logging import get_logger
from app.core.storage import StorageNotConfiguredError, get_storage

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

OK = "ok"
NOT_CONFIGURED = "not_configured"
UNAVAILABLE = "unavailable"


class ReadinessResponse(BaseModel):
    status: str
    database: str
    storage: str
    detail: str | None = None


def _check_database() -> tuple[str, str | None]:
    try:
        check_connection()
    except DatabaseNotConfiguredError as exc:
        return NOT_CONFIGURED, str(exc)
    except Exception as exc:
        # The message can carry host and user, so log it and return the type
        # only. A readiness probe is often reachable more widely than the API.
        logger.warning("Readiness: database check failed: %s", exc)
        return UNAVAILABLE, type(exc).__name__
    return OK, None


def _check_storage() -> tuple[str, str | None]:
    try:
        get_storage().check_connection()
    except StorageNotConfiguredError as exc:
        return NOT_CONFIGURED, str(exc)
    except Exception as exc:
        logger.warning("Readiness: storage check failed: %s", exc)
        return UNAVAILABLE, type(exc).__name__
    return OK, None


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness check (touches the database and storage)",
    responses={503: {"model": ReadinessResponse, "description": "A dependency is not ready"}},
)
def get_ready(response: Response) -> ReadinessResponse:
    database, database_detail = _check_database()
    storage, storage_detail = _check_storage()

    if database == OK and storage == OK:
        return ReadinessResponse(status="ready", database=OK, storage=OK)

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # Both dependencies are checked even when the first already failed: one
    # request should report everything that is wrong, not send the reader back
    # for a second round after they fix the first thing.
    details = [d for d in (database_detail, storage_detail) if d]
    return ReadinessResponse(
        status="not_ready",
        database=database,
        storage=storage,
        detail="; ".join(details) or None,
    )
