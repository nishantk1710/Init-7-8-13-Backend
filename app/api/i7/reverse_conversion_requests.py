"""Min-Max -> OAR: ask for a conversion, and approve it.

``POST /reverse-conversion/requests`` raises a request for an eligible material-plant of the newest stored run;
``POST .../{id}/actions`` moves it through the same fixed chain an OAR conversion follows (Inventory Controller ->
Engineering Head -> Plant Head -> Commercial Head). ``GET /reverse-conversion/requests`` says where a
material-plant's newest request stands and what happened to it.

Approval is a recorded decision. Nothing here calls SAP; the last APPROVE ends at SAP_EXECUTION_PENDING and the
change in SAP is made by a person, exactly as for an OAR conversion.

Separate from ``reverse_conversion.py`` (the read-only list) so that file stays free of the approval code.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import Actor, get_current_actor
from app.core.db import get_db
from app.core.security import APPROVAL_STAGE_ROLES, ALL_ROLES, CurrentUser, Roles, require_roles
from app.initiatives.i7.notifications import APPROVAL_ROLE_TO_ENTRA
from app.initiatives.i7.recommendations.types import WorkflowError
from app.initiatives.i7.reverse_approval import service
from app.initiatives.i7.reverse_conversion.snapshot import find_stored
from app.schemas.i7.errors import ApiError, conflict, forbidden
from app.schemas.i7.reverse_requests import (
    ReverseLedgerOut,
    ReverseRequestAction,
    ReverseRequestCreate,
    ReverseRequestDecline,
    ReverseRequestDetail,
    ReverseRequestOut,
)

router = APIRouter(prefix="/reverse-conversion/requests", tags=["i7-reverse-conversion"])

#: Anyone who may use the app and is not a view-only user may ask for a conversion, as for an OAR one.
_REQUESTER_ROLES = sorted(ALL_ROLES - {Roles.VIEWER, Roles.SAP_EVENTS_WRITE})


@router.get(
    "",
    response_model=ReverseRequestDetail,
    summary="Where a material-plant's conversion request stands",
    description="The newest Min-Max to OAR conversion request for one material-plant and its approval history, or "
    "`current: null` when none was ever raised.",
)
def get_request(
    session: Annotated[Session, Depends(get_db)],
    material: Annotated[str, Query(min_length=1, max_length=40)],
    plant: Annotated[str, Query(min_length=1, max_length=8)],
) -> ReverseRequestDetail:
    request = service.latest_request(session, material, plant)
    if request is None:
        return ReverseRequestDetail(current=None, history=[])
    return ReverseRequestDetail(
        current=ReverseRequestOut.of(request),
        history=[ReverseLedgerOut.of(entry) for entry in service.history(session, request.id)],
    )


@router.post(
    "",
    response_model=ReverseRequestOut,
    status_code=201,
    summary="Ask for a material-plant to be converted from Min-Max to OAR",
    description="Raises a request for an eligible (Convert to OAR) material-plant of the newest stored run and enters "
    "the approval chain at its first role. 404 when the material-plant is not in the run, 409 when it is not "
    "eligible or a request is already open for it. Calls no SAP API.",
    responses={404: {"description": "Not in the newest run"}, 409: {"description": "Not eligible, or a request is open"}},
)
def create_request(
    body: ReverseRequestCreate,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    _user: Annotated[CurrentUser, Depends(require_roles(*_REQUESTER_ROLES))],
) -> ReverseRequestOut:
    found = find_stored(session, body.material, body.plant)
    if found is None:
        raise ApiError(404, "REVERSE_ROW_NOT_FOUND", "That material-plant is not in the newest Min-Max to OAR recommendation.")
    run, stored = found
    try:
        request = service.submit_request(session, stored, run, actor_id=actor.id, comment=body.comment)
    except service.RequestError as exc:
        raise conflict(exc.code, exc.message) from exc
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_STATE", str(exc)) from exc
    session.commit()
    return ReverseRequestOut.of(request)


@router.post(
    "/decline",
    response_model=ReverseRequestOut,
    status_code=201,
    summary="Decline to convert a material-plant, without sending it for approval",
    description="For a planner who will not convert an eligible material: records the decision straight away instead of "
    "sending it through the chain only to have an approver reject it. A non-empty comment is required. Ends in REJECTED "
    "with one immutable ledger entry and calls no SAP API; the material can be asked for again later.",
    responses={404: {"description": "Not in the newest run"}, 409: {"description": "Not eligible, a request is open, or no comment"}},
)
def decline_request(
    body: ReverseRequestDecline,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    _user: Annotated[CurrentUser, Depends(require_roles(*_REQUESTER_ROLES))],
) -> ReverseRequestOut:
    found = find_stored(session, body.material, body.plant)
    if found is None:
        raise ApiError(404, "REVERSE_ROW_NOT_FOUND", "That material-plant is not in the newest Min-Max to OAR recommendation.")
    run, stored = found
    try:
        request = service.decline_request(session, stored, run, actor_id=actor.id, comment=body.comment)
    except service.RequestError as exc:
        raise conflict(exc.code, exc.message) from exc
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_ACTION", str(exc)) from exc
    session.commit()
    return ReverseRequestOut.of(request)


@router.post(
    "/{request_id}/actions",
    response_model=ReverseRequestOut,
    summary="Apply an approval action to a conversion request",
    description="APPROVE / REJECT / SEND_BACK / HOLD / RELEASE_HOLD, by the pending role only (ADJUST does not apply). "
    "No stage can be skipped; REJECT, SEND_BACK and HOLD need a comment. The last APPROVE moves the request to "
    "SAP_EXECUTION_PENDING and calls no SAP API. Every action writes one immutable ledger entry.",
    responses={404: {"description": "Request not found"}, 409: {"description": "Invalid action for this role or stage"}},
)
def apply_request_action(
    request_id: int,
    body: ReverseRequestAction,
    session: Annotated[Session, Depends(get_db)],
    actor: Annotated[Actor, Depends(get_current_actor)],
    user: Annotated[CurrentUser, Depends(require_roles(*sorted(APPROVAL_STAGE_ROLES)))],
) -> ReverseRequestOut:
    claimed = APPROVAL_ROLE_TO_ENTRA.get(body.actor_role)
    if claimed is None or not user.has_any(claimed):
        raise forbidden(
            "ROLE_NOT_HELD", f"You do not hold the {body.actor_role} approval role.", actor_role=str(body.actor_role)
        )
    from app.models.i7_reverse import ReverseConversionRequest

    request = session.get(ReverseConversionRequest, request_id)
    if request is None:
        raise ApiError(404, "REVERSE_REQUEST_NOT_FOUND", "No such conversion request.")
    try:
        service.apply_action(
            session, request, actor_id=actor.id, actor_role=body.actor_role, action=body.action, comment=body.comment
        )
    except WorkflowError as exc:
        raise conflict("INVALID_WORKFLOW_ACTION", str(exc)) from exc
    session.commit()
    return ReverseRequestOut.of(request)
