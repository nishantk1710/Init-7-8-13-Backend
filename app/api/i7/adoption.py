"""SAP adoption reconciliation, read-only.

Calls the existing Phase 7 evaluators directly -- no SAP client is imported
here, and none exists to import. ``RawChangeDocumentProvider`` reads the raw
``raw_cdhdr``/``raw_cdpos`` extract tables (real data, no SAP call), scoped to
FR-9's configurable monitoring window (``PolicyDocument().adoption.
monitoring_window_days``, default 15 days per the 2026-09-22 business
direction) after each recommendation's own ``generated_at`` date. On the
current extract every result is still UNKNOWN, because that extract's CDPOS
table contains no MATERIAL/MARC change rows at all -- see
``sap_change_documents.py`` for the verification.

Every evaluated result is also persisted to ``i7_sap_adoption``
(``SapAdoptionResult``) -- FR-9's "recommendation ledger" -- as a side effect
of being viewed through either endpoint below, upserted by
``recommendation_id`` so repeated views of the same recommendation update the
one row rather than accumulating history (the ledger records the latest
known reconciliation, not an append-only log of every request)."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.api.i7.recommendations import _apply_filters, _latest_only
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations.adoption import (
    evaluate_conversion_adoption,
    evaluate_parameter_adoption,
)
from app.initiatives.i7.recommendations.adoption_report import (
    build_field_reports,
    check_type_label,
    is_pending,
    plain_detail,
)
from app.initiatives.i7.recommendations.sap_change_documents import RawChangeDocumentProvider
from app.initiatives.i7.recommendations.types import (
    AdoptionFieldReport,
    AdoptionResult,
    ApprovalAction,
    LifecycleStatus,
)
from app.models.i7_recommendation import ApprovalLedgerEntry, Recommendation, SapAdoptionResult
from app.schemas.i7.adoption import (
    AdoptionFieldRow,
    AdoptionListItem,
    AdoptionListResponse,
    AdoptionResponse,
    AdoptionStatusCount,
    AdoptionSummary,
)

router = APIRouter(tags=["i7-adoption"])

MAX_PAGE_SIZE = 200
"""Same bound as the recommendations list endpoint -- this is a table, not a
bulk export."""


def _provider_for(
    session: Session, anchor: date | None, policy: PolicyDocument
) -> RawChangeDocumentProvider:
    """One provider per recommendation, windowed to FR-9's monitoring period
    starting at that recommendation's own anchor date -- never a single
    session-wide provider, since each recommendation's window starts on a
    different date. The anchor is the approval date when there is one (the
    changes worth tracking are the ones made after someone approved), else the
    recommendation's ``generated_at`` date."""
    return RawChangeDocumentProvider(
        session,
        recommendation_date=anchor,
        monitoring_window_days=policy.adoption.monitoring_window_days,
    )


def _approval_dates(session: Session, recommendation_ids: list[str]) -> dict[str, date]:
    """The date each recommendation reached final approval, in one query.

    The newest ledger entry where the action was an approval that ended in the
    APPROVED state -- the last step of the chain, not an earlier role's approval.
    """
    if not recommendation_ids:
        return {}
    rows = session.execute(
        select(ApprovalLedgerEntry.recommendation_id, func.max(ApprovalLedgerEntry.timestamp))
        .where(
            ApprovalLedgerEntry.recommendation_id.in_(recommendation_ids),
            ApprovalLedgerEntry.action == ApprovalAction.APPROVE.value,
            ApprovalLedgerEntry.new_status == LifecycleStatus.APPROVED.value,
        )
        .group_by(ApprovalLedgerEntry.recommendation_id)
    ).all()
    return {rid: stamp.date() for rid, stamp in rows if stamp is not None}


@dataclass(frozen=True)
class _AdoptionView:
    """One recommendation's adoption result with everything the page shows."""

    result: AdoptionResult
    is_conversion: bool
    reports: tuple[AdoptionFieldReport, ...]
    detail: str
    check_type: str
    approved_on: date | None
    window_ends_on: date | None
    window_open: bool
    pending: bool


def _build_view(
    session: Session, row: Recommendation, policy: PolicyDocument, approved_on: date | None
) -> _AdoptionView:
    anchor = approved_on or (row.generated_at.date() if row.generated_at else None)
    anchor_name = "approval" if approved_on else "recommendation"
    provider = _provider_for(session, anchor, policy)
    result, is_conversion = _evaluate_row(row, provider)
    changes = provider.field_changes(row.sap_material_number, row.sap_plant_code)
    reports = build_field_reports(result, changes, anchor)

    window_days = policy.adoption.monitoring_window_days
    window_ends_on = anchor + timedelta(days=window_days) if anchor and window_days is not None else None
    window_open = window_ends_on is None or datetime.now(timezone.utc).date() <= window_ends_on

    return _AdoptionView(
        result=result,
        is_conversion=is_conversion,
        reports=reports,
        detail=plain_detail(
            result, reports, anchor_name=anchor_name, window_days=window_days, window_open=window_open
        ),
        check_type=check_type_label(reports, is_conversion),
        approved_on=approved_on,
        window_ends_on=window_ends_on,
        window_open=window_open,
        pending=is_pending(result.status, window_open),
    )


def _field_rows(reports: tuple[AdoptionFieldReport, ...]) -> list[AdoptionFieldRow]:
    return [AdoptionFieldRow(**report._asdict()) for report in reports]


def _evaluate_row(row: Recommendation, provider: RawChangeDocumentProvider) -> tuple[AdoptionResult, bool]:
    """The same reconciliation the single-record endpoint runs, factored out
    so the list endpoint computes it identically per row -- one evaluator
    call per recommendation, still read-only, still no SAP call."""
    is_conversion_adoption = bool(row.is_oar and row.conversion_eligibility == "ELIGIBLE")
    if is_conversion_adoption:
        result = evaluate_conversion_adoption(
            row.sap_material_number, row.sap_plant_code, expected_mrp_type="VB",
            provider=provider,
        )
    else:
        result = evaluate_parameter_adoption(
            row.sap_material_number,
            row.sap_plant_code,
            # `is not None`, never bare truthiness -- a real recommended
            # value of 0 (a legitimate "hold none of this" recommendation)
            # is falsy in Python and would otherwise be silently dropped as
            # if it were never calculated, which is a different claim.
            approved_safety_stock=(
                int(row.recommended_safety_stock) if row.recommended_safety_stock is not None else None
            ),
            approved_rop=int(row.recommended_rop) if row.recommended_rop is not None else None,
            approved_max_stock=(
                int(row.recommended_max_stock) if row.recommended_max_stock is not None else None
            ),
            provider=provider,
        )
    return result, is_conversion_adoption


def _persist_result(session: Session, recommendation_id: str, result: AdoptionResult, detail: str) -> None:
    """Upsert FR-9's ledger entry for one recommendation -- get-then-update-
    or-insert, the same pattern ``save_report`` uses for the quarterly report
    (see ``reporting/repository.py``), never a database-specific ``ON
    CONFLICT`` (the portability rule rules that out here too)."""
    existing = session.execute(
        select(SapAdoptionResult).where(SapAdoptionResult.recommendation_id == recommendation_id)
    ).scalar_one_or_none()

    fields = dict(
        status=result.status.value,
        expected_fields=", ".join(f"{k}={v}" for k, v in result.expected),
        observed_fields=", ".join(f"{k}={v}" for k, v in result.observed),
        matched_fields=", ".join(result.matched_fields),
        mismatched_fields=", ".join(result.mismatched_fields),
        detail=detail[:500],
        evaluated_at=datetime.now(timezone.utc),
    )
    if existing is None:
        session.add(SapAdoptionResult(recommendation_id=recommendation_id, **fields))
    else:
        for key, value in fields.items():
            setattr(existing, key, value)
    session.commit()


@router.get(
    "/recommendations/{recommendation_id}/adoption",
    response_model=AdoptionResponse,
    summary="Get SAP adoption reconciliation status",
    description="ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED / UNKNOWN, "
    "computed read-only through the existing Phase 7 adoption evaluators, "
    "scoped to FR-9's configurable monitoring window after the "
    "recommendation's own date, and persisted to the recommendation ledger "
    "(i7_sap_adoption) as a side effect. UNKNOWN means no SAP evidence is "
    "available -- it is never reported as NOT_ADOPTED. No SAP call is made; "
    "the current extract has no staged CDHDR/CDPOS, so every result on this "
    "data is UNKNOWN today.",
    responses={404: {"description": "Recommendation not found"}},
)
def get_adoption(
    recommendation_id: str, session: Annotated[Session, Depends(get_session)]
) -> AdoptionResponse:
    row: Recommendation = load_latest_recommendation(session, recommendation_id)
    policy = PolicyDocument()
    view = _build_view(
        session, row, policy, _approval_dates(session, [recommendation_id]).get(recommendation_id)
    )
    result, is_conversion_adoption = view.result, view.is_conversion

    detail = view.detail
    if is_conversion_adoption and result.status.value == "UNKNOWN":
        # The literal, business-facing phrase for this specific case: no
        # ND/PD -> VB transition has been observed for this material-plant.
        # Never claimed as ADOPTED/NOT_ADOPTED from planning-field evidence
        # alone -- conversion adoption is a distinct check (ND/PD -> VB +
        # MINBE + MABST), not inferred from any other field changing.
        detail = "Awaiting SAP test change: " + detail

    _persist_result(session, recommendation_id, result, detail)

    return AdoptionResponse(
        recommendation_id=recommendation_id,
        status=result.status.value,
        expected=dict(result.expected),
        observed=dict(result.observed),
        matched_fields=list(result.matched_fields),
        mismatched_fields=list(result.mismatched_fields),
        detail=detail,
        check_type=view.check_type,
        fields=_field_rows(view.reports),
        approved_on=view.approved_on,
        window_ends_on=view.window_ends_on,
        window_open=view.window_open,
        pending=view.pending,
        is_conversion_adoption=is_conversion_adoption,
    )


@router.get(
    "/recommendations/adoption",
    response_model=AdoptionListResponse,
    summary="Portfolio-wide SAP adoption reconciliation",
    description="The same read-only ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED "
    "/ UNKNOWN reconciliation as the per-recommendation endpoint, computed for "
    "a filtered, paginated page of recommendations -- for a dedicated "
    "adoption-tracking table rather than one recommendation at a time. Only "
    "material-plants with an actual calculated recommendation (a non-null "
    "recommended reorder point, safety stock or max stock) are listed -- "
    "there is nothing to reconcile adoption of for the far larger set that "
    "never reached a calculated value (see docs on the upstream data gaps). "
    "Supports the same filters as GET /recommendations. Registered before "
    "/{recommendation_id} so 'adoption' is never read as one.",
)
def list_adoption(
    session: Annotated[Session, Depends(get_session)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    status: str | None = None,
    plant: str | None = None,
    material: str | None = None,
    demand_class: str | None = None,
    is_oar: bool | None = None,
    confidence: str | None = None,
    criticality: str | None = None,
) -> AdoptionListResponse:
    # Adoption tracking is a per-material-plant view, not a per-run history --
    # see _latest_only's docstring for why a material-plant can have more
    # than one Recommendation row and why only the newest is eligible here.
    base = _latest_only(
        _apply_filters(
            select(Recommendation),
            status=status,
            plant=plant,
            material=material,
            demand_class=demand_class,
            is_oar=is_oar,
            confidence=confidence,
            criticality=criticality,
            generated_from=None,
            generated_to=None,
        ),
        # Adoption is about approved changes: the newest APPROVED row per
        # material-plant, so a later regeneration cannot hide an approval.
        only_status=LifecycleStatus.APPROVED.value,
    ).where(
        # Only list material-plants a value was actually calculated for --
        # the vast majority of the catalogue never reaches this point (see
        # the upstream criticality/history/lead-time data gaps), and there is
        # nothing for an adoption check to reconcile against for them.
        or_(
            Recommendation.recommended_rop.is_not(None),
            Recommendation.recommended_safety_stock.is_not(None),
            Recommendation.recommended_max_stock.is_not(None),
        ),
    )

    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    statement = (
        base.order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = session.execute(statement).scalars().all()

    policy = PolicyDocument()
    approved_on = _approval_dates(session, [row.recommendation_id for row in rows])
    items = []
    for row in rows:
        view = _build_view(session, row, policy, approved_on.get(row.recommendation_id))
        _persist_result(session, row.recommendation_id, view.result, view.detail)
        items.append(
            AdoptionListItem(
                recommendation_id=row.recommendation_id,
                sap_material_number=row.sap_material_number,
                sap_plant_code=row.sap_plant_code,
                status=view.result.status.value,
                is_conversion_adoption=view.is_conversion,
                detail=view.detail,
                check_type=view.check_type,
                fields=_field_rows(view.reports),
                approved_on=view.approved_on,
                window_ends_on=view.window_ends_on,
                window_open=view.window_open,
                pending=view.pending,
            )
        )

    return AdoptionListResponse(items=items, total=total, page=page, page_size=page_size)


@router.get(
    "/recommendations/adoption/summary",
    response_model=AdoptionSummary,
    summary="Portfolio-wide adoption rate, from the recommendation ledger",
    description="Real COUNT/GROUP BY over i7_sap_adoption -- FR-9's persisted "
    "ledger -- never a live re-evaluation of every recommendation on each "
    "call. Reflects only recommendations someone has actually viewed through "
    "the adoption endpoints so far (the ledger fills in as pages are "
    "viewed), so this number grows as more of the portfolio is evaluated -- "
    "it is not a claim about the whole portfolio's adoption on day one. "
    "Feeds the Inventory Planning overview's adoption-rate card and the "
    "quarterly report's SAP Adoption section.",
)
def get_adoption_summary(session: Annotated[Session, Depends(get_session)]) -> AdoptionSummary:
    total_evaluated = session.execute(select(func.count()).select_from(SapAdoptionResult)).scalar_one()

    by_status_rows = session.execute(
        select(SapAdoptionResult.status, func.count()).group_by(SapAdoptionResult.status)
    ).all()
    counts = {status: count for status, count in by_status_rows}

    adopted = counts.get("ADOPTED", 0)
    partially_adopted = counts.get("PARTIALLY_ADOPTED", 0)
    not_adopted = counts.get("NOT_ADOPTED", 0)
    unknown = counts.get("UNKNOWN", 0)

    # Rate is over KNOWN evidence only -- excluding UNKNOWN rows from the
    # denominator, never counting them as "observed and not adopted". An
    # all-UNKNOWN portfolio (the current state of this extract) must report
    # None, not a fabricated 0%.
    known_count = total_evaluated - unknown
    adoption_rate = round((adopted + partially_adopted) / known_count * 100, 1) if known_count > 0 else None

    return AdoptionSummary(
        total_evaluated=total_evaluated,
        by_status=[AdoptionStatusCount(status=status, count=count) for status, count in by_status_rows],
        adopted_count=adopted,
        partially_adopted_count=partially_adopted,
        not_adopted_count=not_adopted,
        unknown_count=unknown,
        adoption_rate_percentage=adoption_rate,
    )
