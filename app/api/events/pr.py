"""Purchase Requisition event listener.

SAP's CPI iFlow posts here when a PR is created. The ABAP side declares::

    banfn      TYPE eban-banfn   CHAR 10
    created_on TYPE datum        DATS  8
    created_by TYPE sy-uname     CHAR 12
    created_at TYPE sy-uzeit     TIMS  6
    message    TYPE sy-msgv1     CHAR 50

all of which serialise to JSON strings, e.g.::

    {
        "BANFN":      "1000000567",
        "CREATED_ON": "2026-09-16",
        "CREATED_BY": "VSUNEEL",
        "CREATED_AT": "13:14:03",
        "MESSAGE":    "PR Created Successfully"
    }

Still a connectivity stub in what it *does*: it records the event and returns
202. It does not persist, classify, call SAP, or route into I07/I08/I13.

WHY THIS READS THE RAW BODY INSTEAD OF TAKING A MODEL PARAMETER

Declaring the model as the parameter is the idiomatic FastAPI spelling, and it
is what this endpoint used to do. It rejected SAP's events with 422.

The reason is that FastAPI parses the body *before* the model is consulted, and
that parse requires a JSON content type. ABAP's ``cl_http_client`` sends
``text/plain`` unless the caller sets the header, so a perfectly good JSON body
never reached the model at all -- every tolerance the model was carefully given
was defeated one layer higher up. Reading the body here and parsing it ourselves
puts the decision back where the tolerance lives.
"""

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/events", tags=["events"])

# Enough of a bad body to identify the sender and the format, not so much that
# one malformed post floods the log.
MAX_LOGGED_BODY_BYTES = 2_000


class PREvent(BaseModel):
    """The PR-created event, as SAP sends it.

    Every field is optional and unknown fields are kept, because the iFlow is
    not versioned -- SAP can add or drop a field without telling us. Refusing
    the event over a missing one would lose a real purchase requisition to a
    schema quibble, and a PR we never heard about is worse than a PR we heard
    about incompletely. Whatever is absent is reported in the log instead.

    The date and time stay ``str``. Declaring them as ``date`` and ``time``
    would make SAP's formatting our validation problem: DATS arrives as
    ``20260916`` and TIMS as ``131403``, neither of which is ISO, and a genuine
    event would 422 instead of arriving.

    ``coerce_numbers_to_str`` covers the other half of that. ABAP serialisers
    differ on whether a NUMC-like field is quoted, and a bare ``1000000567``
    would otherwise be rejected for being the wrong JSON type -- a distinction
    with no meaning to anyone waiting on the requisition.
    """

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        coerce_numbers_to_str=True,
    )

    banfn: str | None = Field(
        default=None,
        alias="BANFN",
        description="Purchase requisition number (ABAP eban-banfn, CHAR 10).",
    )
    created_on: str | None = Field(
        default=None,
        alias="CREATED_ON",
        description="Creation date as SAP sends it (ABAP DATS, e.g. 20260916).",
    )
    created_by: str | None = Field(
        default=None,
        alias="CREATED_BY",
        description="SAP user id of the creator (ABAP sy-uname, CHAR 12).",
    )
    created_at: str | None = Field(
        default=None,
        alias="CREATED_AT",
        description="Creation time as SAP sends it (ABAP TIMS, e.g. 131403).",
    )
    message: str | None = Field(
        default=None,
        alias="MESSAGE",
        description="SAP's own status text (ABAP sy-msgv1, CHAR 50).",
    )


class PREventAccepted(BaseModel):
    status: str


def _reject(reason: str, content_type: str, raw: bytes) -> HTTPException:
    """Log a body we could not read, and build the reply that says why.

    A 422 raised by FastAPI never reaches this module, which is why the earlier
    failures were invisible from our side and had to be guessed at from SAP's
    end. Every rejection now leaves a line naming the content type and showing
    the start of the body, so the next one is diagnosable from the log alone.
    """
    logger.warning(
        "PR event REJECTED (%s). Content-Type: %s, %d bytes. Body starts: %r",
        reason,
        content_type,
        len(raw),
        raw[:MAX_LOGGED_BODY_BYTES],
    )
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)


@router.post(
    "/pr",
    response_model=PREventAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Accept a PR event (stub)",
    responses={400: {"description": "Body could not be read as a JSON object."}},
    # Taking the raw Request would otherwise erase the request body from the
    # generated schema, and /docs is how SAP reads the contract.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": PREvent.model_json_schema()}},
        }
    },
)
async def receive_pr_event(request: Request) -> PREventAccepted:
    raw = await request.body()
    content_type = request.headers.get("content-type", "(none)")

    if not raw.strip():
        raise _reject("Empty request body; expected a JSON object.", content_type, raw)

    # Parsed without consulting the content type on purpose. A JSON body sent
    # as text/plain is still a JSON body, and refusing it would fail a real
    # event over a header rather than over its contents.
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise _reject(f"Body is not valid JSON: {exc}", content_type, raw) from exc

    if not isinstance(parsed, dict):
        raise _reject(
            f"Expected a JSON object, received a JSON {type(parsed).__name__}.",
            content_type,
            raw,
        )

    # From here the event is accepted whatever its fields look like. It is a
    # readable object, so it can be recorded, and a recorded event we do not
    # fully understand beats a rejected one we have lost.
    try:
        event = PREvent.model_validate(parsed)
        payload: dict[str, Any] = event.model_dump(by_alias=True, exclude_none=True)
        banfn = event.banfn
    except ValidationError as exc:
        logger.warning(
            "PR event has a field we could not read; accepting it anyway: %s", exc
        )
        payload = parsed
        banfn = parsed.get("BANFN") or parsed.get("banfn")

    logger.info("PR event received: %s", payload)

    # BANFN is the only field that says *which* requisition this is. Without it
    # the event cannot be matched to anything downstream, so it is worth a
    # louder line than the one above -- but it is still accepted, not refused.
    if not banfn:
        logger.warning("PR event has no BANFN; the requisition cannot be identified")

    return PREventAccepted(status="received")
