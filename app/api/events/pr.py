"""Purchase Requisition event listener.

SAP's CPI iFlow posts here when a PR is created. The payload SAP confirmed::

    {
        "BANFN":      "1000000567",
        "CREATED_ON": "2026-09-16",
        "CREATED_BY": "VSUNEEL",
        "CREATED_AT": "13:14:03",
        "MESSAGE":    "PR Created Successfully"
    }

Still a connectivity stub in what it *does*: it records the event and returns
202. It does not persist, classify, call SAP, or route into I07/I08/I13.
"""

from typing import Any

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


class PREvent(BaseModel):
    """The PR-created event, as SAP sends it.

    Every field is optional and unknown fields are kept, because the iFlow is
    not versioned -- SAP can add or drop a field without telling us. Refusing
    the event over a missing one would lose a real purchase requisition to a
    schema quibble, and a PR we never heard about is worse than a PR we heard
    about incompletely. Whatever is absent is reported in the log instead.

    The date and time stay ``str`` for the same reason. Declaring them as
    ``date`` and ``time`` would make SAP's formatting our validation problem:
    one unexpected format and a genuine event 422s instead of arriving.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    banfn: str | None = Field(
        default=None,
        alias="BANFN",
        description="Purchase requisition number.",
    )
    created_on: str | None = Field(
        default=None,
        alias="CREATED_ON",
        description="Creation date, as SAP formats it (e.g. 2026-09-16).",
    )
    created_by: str | None = Field(
        default=None,
        alias="CREATED_BY",
        description="SAP user id of the creator (e.g. VSUNEEL).",
    )
    created_at: str | None = Field(
        default=None,
        alias="CREATED_AT",
        description="Creation time, as SAP formats it (e.g. 13:14:03).",
    )
    message: str | None = Field(
        default=None,
        alias="MESSAGE",
        description="SAP's own status text.",
    )


class PREventAccepted(BaseModel):
    status: str


@router.post(
    "/pr",
    response_model=PREventAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Accept a PR event (stub)",
)
def receive_pr_event(event: PREvent) -> PREventAccepted:
    # ``by_alias`` so the log reads in SAP's field names rather than our Python
    # ones. When we and CPI disagree about what was sent, the two sides are
    # then directly comparable instead of needing a mental translation.
    #
    # ``exclude_none`` so the line shows what actually arrived: a field SAP
    # omitted is absent here too, rather than appearing as a null we invented.
    payload: dict[str, Any] = event.model_dump(by_alias=True, exclude_none=True)

    logger.info("PR event received: %s", payload)

    # BANFN is the only field that says *which* requisition this is. Without it
    # the event cannot be matched to anything downstream, so it is worth a
    # louder line than the one above -- but it is still accepted, not refused.
    if event.banfn is None:
        logger.warning("PR event has no BANFN; the requisition cannot be identified")

    return PREventAccepted(status="received")
