"""Submit a Min-Max -> OAR conversion request and move it through the approval chain.

The rules are the OAR conversion's own, taken from the same pure state machine:

* the route is :data:`~app.initiatives.i7.recommendations.types.OAR_APPROVAL_CHAIN`, fixed, whatever the criticality;
* the acting role must be the pending role; no stage is skipped;
* REJECT, SEND_BACK and HOLD need a comment; APPROVE and RELEASE_HOLD do not;
* the last APPROVE ends at SAP_EXECUTION_PENDING and stops there -- no code path reaches SAP;
* every action writes one immutable ledger row, in the same transaction as the change of status.

ADJUST is not offered: an OAR conversion changes how a material is ordered, not a number an approver could edit.

Functions here do not commit; the route that calls them does.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.initiatives.i7.recommendations import workflow
from app.initiatives.i7.recommendations.types import (
    OAR_APPROVAL_CHAIN,
    ApprovalAction,
    ApprovalRole,
    LifecycleStatus,
    WorkflowError,
)
from app.models.i7_reverse import ReverseApprovalLedger, ReverseConversionRequest, ReverseRecommendation, ReverseRun

#: A request in one of these states is still alive (or finished and waiting for SAP): another cannot be raised.
OPEN_STATUSES = frozenset(
    {
        LifecycleStatus.PENDING_APPROVAL.value,
        LifecycleStatus.SENT_BACK.value,
        LifecycleStatus.HELD.value,
        LifecycleStatus.SAP_EXECUTION_PENDING.value,
    }
)

ROUTE: tuple[ApprovalRole, ...] = OAR_APPROVAL_CHAIN

#: The one category a request may be raised for.
ELIGIBLE_CATEGORY = "Recommended for OAR"

SUBMITTER_ROLE = "Requester"
"""The ledger's ``actor_role`` for the person who raises a request: they hold no stage of the chain."""


class RequestError(Exception):
    """A request that cannot be made or found: not eligible, already open, unknown. (A bad approval action is a
    :class:`WorkflowError`.)"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _state(request: ReverseConversionRequest) -> workflow.WorkflowState:
    return workflow.WorkflowState(
        status=LifecycleStatus(request.status),
        chain_index=request.chain_index,
        adjustment_count=request.adjustment_count,
        route=ROUTE,
    )


def pending_role(request: ReverseConversionRequest) -> ApprovalRole | None:
    """The role whose decision is next, or None when nothing is pending."""
    return _state(request).pending_role


def _sync_pending_role(request: ReverseConversionRequest) -> None:
    """Keep the stored ``pending_role`` text in step with the state: lists read it, not the state machine."""
    role = pending_role(request)
    request.pending_role = role.value if role else None


def _ledger(
    session: Session,
    request: ReverseConversionRequest,
    *,
    actor_id: str,
    actor_role: str,
    action: str,
    previous: str,
    comment: str | None,
) -> None:
    session.add(
        ReverseApprovalLedger(
            request_id=request.id,
            actor_id=actor_id[:64],
            actor_role=actor_role[:32],
            action=action[:16],
            previous_status=previous,
            new_status=request.status,
            comment=(comment or None) and comment.strip()[:1000],
            timestamp=workflow.event_timestamp(),
        )
    )


def latest_request(session: Session, material: str, plant: str) -> ReverseConversionRequest | None:
    return session.scalars(
        select(ReverseConversionRequest)
        .where(ReverseConversionRequest.sap_material_number == material, ReverseConversionRequest.sap_plant_code == plant)
        .order_by(ReverseConversionRequest.id.desc())
        .limit(1)
    ).first()


def latest_requests(session: Session, keys: list[tuple[str, str]]) -> dict[tuple[str, str], ReverseConversionRequest]:
    """The newest request of each material-plant in ``keys`` (those that have one)."""
    if not keys:
        return {}
    materials = sorted({material for material, _ in keys})
    wanted = set(keys)
    newest: dict[tuple[str, str], ReverseConversionRequest] = {}
    for request in session.scalars(
        select(ReverseConversionRequest)
        .where(ReverseConversionRequest.sap_material_number.in_(materials))
        .order_by(ReverseConversionRequest.id)
    ):
        key = (request.sap_material_number, request.sap_plant_code)
        if key in wanted:
            newest[key] = request  # ascending ids: the last one written wins
    return newest


def history(session: Session, request_id: int) -> list[ReverseApprovalLedger]:
    return list(
        session.scalars(
            select(ReverseApprovalLedger).where(ReverseApprovalLedger.request_id == request_id).order_by(ReverseApprovalLedger.id)
        )
    )


def submit_request(
    session: Session,
    stored: ReverseRecommendation,
    run: ReverseRun,
    *,
    actor_id: str,
    comment: str | None,
) -> ReverseConversionRequest:
    """Raise a request for a stored recommendation. It must be Convert to OAR, and none may be open for it."""
    if stored.category != ELIGIBLE_CATEGORY:
        raise RequestError("REVERSE_NOT_ELIGIBLE", "Only a material that is eligible to convert to OAR can be requested.")
    existing = latest_request(session, stored.sap_material_number, stored.sap_plant_code)
    if existing is not None and existing.status in OPEN_STATUSES:
        raise RequestError("REVERSE_REQUEST_OPEN", "A conversion request for this material is already in the approval chain.")

    started = workflow.submit(workflow.start(LifecycleStatus.READY_FOR_REVIEW, ROUTE))  # READY_FOR_REVIEW -> PENDING_APPROVAL
    request = ReverseConversionRequest(
        sap_material_number=stored.sap_material_number,
        sap_plant_code=stored.sap_plant_code,
        status=started.status.value,
        chain_index=started.chain_index,
        adjustment_count=started.adjustment_count,
        run_id=run.id,
        anchor_month=run.anchor_month,
        confidence=stored.confidence,
        reason=stored.reason,
        detail=stored.detail,
        stock_value=stored.stock_value,
        submitted_by=actor_id[:64],
        submit_comment=(comment or None) and comment.strip()[:1000],
        submitted_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    _sync_pending_role(request)
    session.add(request)
    session.flush()  # the id
    _ledger(
        session, request, actor_id=actor_id, actor_role=SUBMITTER_ROLE, action="SUBMIT",
        previous=LifecycleStatus.READY_FOR_REVIEW.value, comment=comment,
    )
    return request


def decline_request(
    session: Session,
    stored: ReverseRecommendation,
    run: ReverseRun,
    *,
    actor_id: str,
    comment: str | None,
) -> ReverseConversionRequest:
    """A planner declines to convert an eligible material, without sending it through the chain.

    The rule is the OAR one (``workflow.reject_unsubmitted``): a stated reason is required, it ends in REJECTED
    with nobody pending, and the decision is ledgered. The planner can ask again later; nothing reaches SAP."""
    if stored.category != ELIGIBLE_CATEGORY:
        raise RequestError("REVERSE_NOT_ELIGIBLE", "Only a material that is eligible to convert to OAR can be rejected here.")
    existing = latest_request(session, stored.sap_material_number, stored.sap_plant_code)
    if existing is not None and existing.status in OPEN_STATUSES:
        raise RequestError("REVERSE_REQUEST_OPEN", "A conversion request for this material is already in the approval chain.")

    declined = workflow.reject_unsubmitted(workflow.start(LifecycleStatus.READY_FOR_REVIEW, ROUTE), comment)
    request = ReverseConversionRequest(
        sap_material_number=stored.sap_material_number,
        sap_plant_code=stored.sap_plant_code,
        status=declined.status.value,
        chain_index=declined.chain_index,
        adjustment_count=declined.adjustment_count,
        run_id=run.id,
        anchor_month=run.anchor_month,
        confidence=stored.confidence,
        reason=stored.reason,
        detail=stored.detail,
        stock_value=stored.stock_value,
        submitted_by=actor_id[:64],
        submit_comment=(comment or None) and comment.strip()[:1000],
        submitted_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    _sync_pending_role(request)
    session.add(request)
    session.flush()
    _ledger(
        session, request, actor_id=actor_id, actor_role=SUBMITTER_ROLE, action=ApprovalAction.REJECT.value,
        previous=LifecycleStatus.READY_FOR_REVIEW.value, comment=comment,
    )
    return request


def apply_action(
    session: Session,
    request: ReverseConversionRequest,
    *,
    actor_id: str,
    actor_role: ApprovalRole,
    action: ApprovalAction,
    comment: str | None,
) -> ReverseConversionRequest:
    """Apply one approval action. Raises :class:`WorkflowError` when it is not valid for this role and stage."""
    if action is ApprovalAction.ADJUST:
        raise WorkflowError("ADJUST is not available for a conversion to OAR: approve, send back, hold or reject it")
    previous = request.status
    new = workflow.apply_action(_state(request), action, actor_role, comment)
    request.status = new.status.value
    request.chain_index = new.chain_index
    request.adjustment_count = new.adjustment_count
    request.updated_at = datetime.now(timezone.utc)
    _sync_pending_role(request)
    _ledger(
        session, request, actor_id=actor_id, actor_role=actor_role.value, action=action.value,
        previous=previous, comment=comment,
    )
    return request
