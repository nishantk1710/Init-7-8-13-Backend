"""Manual SAP execution evidence -- capture only, never a SAP call.

After final approval a recommendation reaches ``SAP_EXECUTION_PENDING``. VZI
then executes the change in SAP using the standard manual transaction, entirely
outside this system. This module records what a human reports happened; it has
no HTTP client, no OData call, no way to reach SAP at all.
"""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.initiatives.i7.recommendations.types import ExecutionStatus, LifecycleStatus
from app.models.i7_recommendation import Recommendation, SapExecutionEvidence


def record_execution_evidence(
    session: Session,
    recommendation: Recommendation,
    *,
    executed_by: str | None,
    execution_status: ExecutionStatus,
    sap_reference: str | None = None,
    evidence_comment: str | None = None,
) -> SapExecutionEvidence:
    """Record what VZI reports about a manual SAP transaction.

    Only a recommendation at ``SAP_EXECUTION_PENDING`` may receive evidence --
    recording execution against anything earlier would claim SAP was touched
    for a change nobody finished approving.
    """
    if recommendation.status != LifecycleStatus.SAP_EXECUTION_PENDING.value:
        raise ValueError(
            f"cannot record execution evidence for a recommendation in state "
            f"{recommendation.status}; only SAP_EXECUTION_PENDING may receive it"
        )

    evidence = SapExecutionEvidence(
        recommendation_id=recommendation.recommendation_id,
        approved_safety_stock=recommendation.recommended_safety_stock,
        approved_rop=recommendation.recommended_rop,
        approved_max_stock=recommendation.recommended_max_stock,
        executed_by=executed_by,
        execution_timestamp=datetime.now(timezone.utc)
        if execution_status is ExecutionStatus.EXECUTED
        else None,
        sap_reference=sap_reference,
        execution_status=execution_status.value,
        evidence_comment=evidence_comment,
    )
    session.add(evidence)

    if execution_status is ExecutionStatus.EXECUTED:
        recommendation.status = LifecycleStatus.SAP_EXECUTED.value

    return evidence
