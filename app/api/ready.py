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
from pydantic import BaseModel, ConfigDict, Field

from app.core.ai import AINotConfiguredError, get_llm
from app.core.criticality import (
    CriticalityNotConfiguredError,
    CriticalityNotLoadedError,
    get_criticality_source,
)
from app.core.db import DatabaseNotConfiguredError, check_connection
from app.core.logging import get_logger
from app.core.storage import StorageNotConfiguredError, get_storage

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

OK = "ok"
NOT_CONFIGURED = "not_configured"
UNAVAILABLE = "unavailable"
#: Criticality only: configured, but its optional data (ZMM065) is not loaded.
#: Every lookup answers "unknown" meanwhile, so it does not fail readiness.
NOT_LOADED = "not_loaded"


class ReadinessResponse(BaseModel):
    # Every other field here is one word, so this model never needed an alias.
    # `ai_provider` is the first two-word field, and the rest of the API serves
    # camelCase -- so it is aliased rather than being the one snake_case key a
    # client has to special-case. populate_by_name keeps the Python-side
    # keyword argument readable.
    model_config = ConfigDict(populate_by_name=True)

    status: str
    database: str
    storage: str
    ai: str
    ai_provider: str = Field("", alias="aiProvider")
    """WHICH provider answered -- ``stub``, ``foundry`` or ``openai``.

    Reported because ``ai: ok`` on its own is not an answer anybody can act on.
    The stub's check_connection() returns immediately and always passes, by
    design, so a deployment with no model configured is indistinguishable from
    a working one by status alone. That cost a day of "which provider is the
    deployed app using?" during the 09-Oct FRS audit, which could not be
    settled without reading Azure app settings.

    Named, not inferred, and it is not a secret: the endpoint already says
    whether a database and a storage account are reachable, and a provider name
    carries no endpoint, key or deployment.
    """

    criticality: str
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


def _check_ai() -> tuple[str, str | None, str]:
    """Whether the configured AI provider is reachable, and WHICH one it is.

    The stub answers instantly and always passes, so a machine with no provider
    configured reports ready rather than degraded -- that is a working state
    here, not a broken one. It is also why the name is returned alongside the
    status: "ok" from the stub and "ok" from Foundry mean very different things
    to anyone reading this to find out whether the AI features actually work.

    The name is read even when the check fails, because "foundry, unavailable"
    is a far more useful line in a log than "unavailable".
    """
    name = ""
    try:
        provider = get_llm()
        name = getattr(provider, "name", "") or ""
        provider.check_connection()
    except AINotConfiguredError as exc:
        return NOT_CONFIGURED, str(exc), name
    except Exception as exc:
        logger.warning("Readiness: AI check failed (provider=%s): %s", name or "?", exc)
        return UNAVAILABLE, type(exc).__name__, name
    return OK, None, name


def _check_criticality() -> tuple[str, str | None]:
    """Whether the configured criticality source can answer.

    Shared by I07, I08 and I13, so an outage here is a platform-wide one rather
    than one initiative's problem -- which is the reason it is reported
    separately from the database it happens to read.
    """
    try:
        get_criticality_source().check_connection()
    except CriticalityNotLoadedError as exc:
        return NOT_LOADED, str(exc)
    except CriticalityNotConfiguredError as exc:
        return NOT_CONFIGURED, str(exc)
    except Exception as exc:
        logger.warning("Readiness: criticality check failed: %s", exc)
        return UNAVAILABLE, type(exc).__name__
    return OK, None


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness check (touches the database, storage, AI provider and criticality source)",
    responses={503: {"model": ReadinessResponse, "description": "A dependency is not ready"}},
)
def get_ready(response: Response) -> ReadinessResponse:
    database, database_detail = _check_database()
    storage, storage_detail = _check_storage()
    ai, ai_detail, ai_provider = _check_ai()
    criticality, criticality_detail = _check_criticality()

    if database == OK and storage == OK and ai == OK and criticality in (OK, NOT_LOADED):
        # Unloaded ZMM065 is optional data, not an outage: ready, and said so.
        return ReadinessResponse(
            status="ready", database=OK, storage=OK, ai=OK, ai_provider=ai_provider,
            criticality=criticality,
            detail=criticality_detail if criticality == NOT_LOADED else None,
        )

    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    # Every dependency is checked even when the first already failed: one
    # request should report everything that is wrong, not send the reader back
    # for a second round after they fix the first thing.
    details = [
        d for d in (database_detail, storage_detail, ai_detail, criticality_detail) if d
    ]
    return ReadinessResponse(
        status="not_ready",
        database=database,
        storage=storage,
        ai=ai,
        ai_provider=ai_provider,
        criticality=criticality,
        detail="; ".join(details) or None,
    )
