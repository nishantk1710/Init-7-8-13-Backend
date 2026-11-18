"""SAP adoption reconciliation, read-only.

Calls the existing Phase 7 evaluators directly -- no SAP client is imported
here, and none exists to import. ``RawChangeDocumentProvider`` reads the raw
``raw_cdhdr``/``raw_cdpos`` extract tables (real data, no SAP call), scoped to
FR-9's configurable monitoring window (``PolicyDocument().adoption.
monitoring_window_days``, 7 days by default -- the one place the length is set).

**The window starts at the SAP handover**, not when the recommendation was
generated and not while it waits for approval: the day its final approval put it
at ``SAP_EXECUTION_PENDING`` (``_handoff_dates``). A recommendation that has not
been handed over has no window yet, is Pending, and reads no change history at
all -- so a slow approval never uses up the time SAP has to act.

**Checked every day, not once at the end** (``run_daily_adoption_check``, called
by the nightly timer): a matching SAP change is picked up the day it appears.
While the window is open a recommendation stays Pending until a change matches
the approved value; changes that do not match yet are shown but do not make it
Not adopted until the window has closed. When it has closed, Not adopted is
said only if SAP's change history reaches the window's last day and holds no
matching change; if the history is incomplete the answer stays Unknown. On the current extract every
result is still UNKNOWN, because that extract's CDPOS table contains no
MATERIAL/MARC change rows at all -- see ``sap_change_documents.py`` for the
verification.

Every evaluated result is also persisted to ``i7_sap_adoption``
(``SapAdoptionResult``) -- FR-9's "recommendation ledger" -- as a side effect
of being viewed through either endpoint below -- and by the daily check --
upserted by ``recommendation_id`` so repeated views or runs of the same
recommendation update the one row rather than accumulating history (the ledger
records the latest known reconciliation, not an append-only log of every
request)."""

import logging

from dataclasses import dataclass
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.i7.deps import get_session, load_latest_recommendation
from app.api.i7.recommendations import _apply_filters, _latest_only
from app.initiatives.i7.kpi_formulas.expression import evaluate
from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.recommendations.adoption import (
    NoSapStateAvailable,
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
    AdoptionStatus,
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


logger = logging.getLogger(__name__)

HANDED_OFF_STATUSES: tuple[str, ...] = (
    LifecycleStatus.SAP_EXECUTION_PENDING.value,
    LifecycleStatus.SAP_EXECUTED.value,
    LifecycleStatus.ADOPTED.value,
    LifecycleStatus.PARTIALLY_ADOPTED.value,
    LifecycleStatus.NOT_ADOPTED.value,
)
"""Every state a recommendation can be in once its final approval has handed it to SAP."""

_ID_CHUNK = 500


def _provider_for(
    session: Session, anchor: date | None, policy: PolicyDocument
) -> RawChangeDocumentProvider:
    """One provider per recommendation, windowed to FR-9's monitoring period
    starting on that recommendation's own SAP handover date -- never a single
    session-wide provider, since each recommendation's window starts on a
    different date."""
    return RawChangeDocumentProvider(
        session,
        recommendation_date=anchor,
        monitoring_window_days=policy.adoption.monitoring_window_days,
    )


def _handoff_dates(session: Session, recommendation_ids: list[str]) -> dict[str, date]:
    """The date each recommendation was handed over for SAP implementation, in one query.

    The newest ledger entry where the action was an approval that ended in
    ``SAP_EXECUTION_PENDING`` -- the final step of the chain, which is what hands the
    recommendation to VZI to change in SAP. An earlier role's approval ends in
    ``PENDING_APPROVAL`` and is not it. Nothing before this entry (generation, review,
    waiting in the approval chain) counts towards the monitoring window.

    (This used to look for ``APPROVED``, a state the workflow never produces: final approval
    goes straight to ``SAP_EXECUTION_PENDING``. So no date was ever found and every window
    started at ``generated_at``.)
    """
    if not recommendation_ids:
        return {}
    rows = session.execute(
        select(ApprovalLedgerEntry.recommendation_id, func.max(ApprovalLedgerEntry.timestamp))
        .where(
            ApprovalLedgerEntry.recommendation_id.in_(recommendation_ids),
            ApprovalLedgerEntry.action == ApprovalAction.APPROVE.value,
            ApprovalLedgerEntry.new_status == LifecycleStatus.SAP_EXECUTION_PENDING.value,
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


def _not_started_detail(row: Recommendation, window_days: int | None) -> str:
    if row.status == LifecycleStatus.REJECTED.value:
        return "Rejected: it will not be handed over to SAP."
    days = f"{window_days}-day " if window_days is not None else ""
    return f"Waiting for final approval and handover to SAP. The {days}SAP check has not started."


def _build_view(
    session: Session,
    row: Recommendation,
    policy: PolicyDocument,
    handed_off_on: date | None,
    today: date | None = None,
) -> _AdoptionView:
    """One recommendation's adoption, as of ``today``.

    ``handed_off_on`` is the day the final approval handed it to SAP (``_handoff_dates``).
    ``None`` means it has not been: no window has started, SAP's change history is not read
    (an unbounded read would pick up changes from before the recommendation existed), and the
    row is Pending.
    """
    today = today or datetime.now(timezone.utc).date()
    window_days = policy.adoption.monitoring_window_days

    if handed_off_on is None:
        result, is_conversion = _evaluate_row(row, NoSapStateAvailable())
        return _AdoptionView(
            result=result,
            is_conversion=is_conversion,
            reports=(),
            detail=_not_started_detail(row, window_days),
            check_type=check_type_label((), is_conversion),
            approved_on=None,
            window_ends_on=None,
            window_open=True,
            pending=True,
        )

    provider = _provider_for(session, handed_off_on, policy)
    result, is_conversion = _evaluate_row(row, provider)
    changes = provider.field_changes(row.sap_material_number, row.sap_plant_code)
    reports = build_field_reports(result, changes, handed_off_on)

    window_ends_on = handed_off_on + timedelta(days=window_days) if window_days is not None else None
    window_open = window_ends_on is None or today <= window_ends_on

    note: str | None = None
    if not window_open and result.expected and result.status in (AdoptionStatus.UNKNOWN, AdoptionStatus.NOT_ADOPTED):
        # The window is over and nothing matches. That is "not adopted" only if SAP's history was
        # complete for the whole window; a history that stops early could simply not have the change.
        if provider.history_covers(window_ends_on):
            if result.status is AdoptionStatus.UNKNOWN:
                result = result._replace(status=AdoptionStatus.NOT_ADOPTED)
            note = "SAP change history covers the whole window, and no matching change was found."
        else:
            result = result._replace(status=AdoptionStatus.UNKNOWN)
            note = (
                f"SAP change history is not complete through {window_ends_on.isoformat()}, "
                "so adoption cannot be confirmed."
            )

    if window_open and result.status is AdoptionStatus.NOT_ADOPTED:
        # SAP changed something, but nothing that matches the approved values *yet*. While the
        # window is open the change may still be corrected or completed, so it stays Pending;
        # the changes found are still listed in the detail. Only a closed window calls it.
        result = result._replace(status=AdoptionStatus.UNKNOWN)
        note = "SAP changed, but no change matches the approved values yet; the check window is still open."

    detail = plain_detail(
        result, reports, anchor_name="SAP handover", window_days=window_days, window_open=window_open
    )
    if note:
        detail = f"{detail}\n{note}"

    return _AdoptionView(
        result=result,
        is_conversion=is_conversion,
        reports=reports,
        detail=detail,
        check_type=check_type_label(reports, is_conversion),
        approved_on=handed_off_on,
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


def _adopted_on(view: "_AdoptionView") -> date | None:
    """When SAP made the change that confirmed this adoption: the latest change date among the fields that matched, from CDHDR/CDPOS.
    ``None`` unless the result is ADOPTED, or when the extract's date could not be read."""
    if view.result.status is not AdoptionStatus.ADOPTED:
        return None
    dates = [report.changed_on for report in view.reports if report.matched and report.changed_on is not None]
    return max(dates) if dates else None


def _persist_result(
    session: Session,
    recommendation_id: str,
    result: AdoptionResult,
    detail: str,
    evaluated_at: datetime | None = None,
    adopted_on: date | None = None,
) -> None:
    """Upsert FR-9's ledger entry for one recommendation -- get-then-update-
    or-insert, the same pattern ``save_report`` uses for the quarterly report
    (see ``reporting/repository.py``), never a database-specific ``ON
    CONFLICT`` (the portability rule rules that out here too)."""
    # ``.first()`` of the newest, not ``scalar_one_or_none``: the table has no unique key on
    # ``recommendation_id``, so a race between two writers could once have left two rows, and
    # that must not make every later check of this recommendation fail.
    existing = session.execute(
        select(SapAdoptionResult)
        .where(SapAdoptionResult.recommendation_id == recommendation_id)
        .order_by(SapAdoptionResult.id.desc())
    ).scalars().first()

    fields = dict(
        status=result.status.value,
        expected_fields=", ".join(f"{k}={v}" for k, v in result.expected),
        observed_fields=", ".join(f"{k}={v}" for k, v in result.observed),
        matched_fields=", ".join(result.matched_fields),
        mismatched_fields=", ".join(result.mismatched_fields),
        detail=detail[:500],
        evaluated_at=evaluated_at or datetime.now(timezone.utc),
        adopted_on=adopted_on,
    )
    if existing is None:
        session.add(SapAdoptionResult(recommendation_id=recommendation_id, **fields))
    else:
        for key, value in fields.items():
            setattr(existing, key, value)
    session.commit()


@dataclass(frozen=True)
class AdoptionRunSummary:
    """What one daily check did, for the nightly log."""

    checked: int = 0
    adopted: int = 0
    partially_adopted: int = 0
    not_adopted: int = 0
    pending: int = 0
    """Checked, and still waiting: no matching SAP change yet (including no change history at all)."""
    skipped: int = 0
    """Left alone: already checked today, already final, or no handover date on record."""
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"{self.checked} checked ({self.adopted} adopted, {self.partially_adopted} partially adopted, "
            f"{self.not_adopted} not adopted, {self.pending} pending), {self.skipped} skipped, {self.failed} failed"
        )


def _already_final(existing: SapAdoptionResult, window_ends_on: date | None, today: date) -> str | None:
    """Why this recommendation needs no more checking today, or ``None`` when it does.

    This is what makes a run safe to repeat -- the next tick, a second instance, a restart: a result
    written today is as current as a new one, an ADOPTED result cannot get better, and once the
    window has closed a verdict written after it is the last word -- unless that verdict is
    UNKNOWN, which means SAP's change history did not yet reach the window's end. That one is
    checked again each day until a newer extract lets it be decided.
    """
    if existing.status == AdoptionStatus.ADOPTED.value:
        return "already adopted"
    if existing.evaluated_at is not None:
        checked_on = existing.evaluated_at.date()
        if checked_on >= today:
            return "already checked today"
        if (
            window_ends_on is not None
            and window_ends_on < today
            and checked_on > window_ends_on
            and existing.status != AdoptionStatus.UNKNOWN.value
        ):
            return "final result recorded after the window closed"
    return None


def run_daily_adoption_check(
    session_factory, today: date | None = None
) -> AdoptionRunSummary:
    """Check SAP's change history for every recommendation that has been handed over to SAP.

    Run once a day by the nightly timer, so a matching change is found the day it is made instead
    of when someone next opens the page or the window ends. Read-only towards SAP; the only write
    is the one ``i7_sap_adoption`` row per recommendation.

    * Only recommendations a final approval has handed over (status ``SAP_EXECUTION_PENDING`` or
      later, with that approval on the ledger) have a window, so only they are looked at. Ones
      still in approval stay Pending and cost nothing.
    * Each is judged by exactly the rule the Adoption page uses (``_build_view``), so a daily result
      and a page view never disagree.
    * Repeating the run changes nothing: see ``_already_final``. One recommendation failing is logged
      and counted, and does not stop the rest.
    """
    today = today or datetime.now(timezone.utc).date()
    policy = PolicyDocument()
    window_days = policy.adoption.monitoring_window_days
    # The time of day is now; the date is the run's own (the same thing outside tests), so "checked today" means the day being run.
    stamped_at = datetime.combine(today, datetime.now(timezone.utc).timetz())
    counts = {"checked": 0, "adopted": 0, "partially_adopted": 0, "not_adopted": 0, "pending": 0, "skipped": 0, "failed": 0}

    with session_factory() as session:
        statement = _latest_only(
            _apply_filters(
                select(Recommendation),
                status=None,
                plant=None,
                material=None,
                demand_class=None,
                is_oar=None,
                confidence=None,
                criticality=None,
                generated_from=None,
                generated_to=None,
                actionable=True,
            )
        ).where(Recommendation.status.in_(HANDED_OFF_STATUSES))
        rows = session.execute(statement).scalars().all()
        ids = [row.recommendation_id for row in rows]
        # In chunks: SQL Server allows about 2,100 parameters in one statement.
        handed_off: dict[str, date] = {}
        latest: dict[str, SapAdoptionResult] = {}
        for start in range(0, len(ids), _ID_CHUNK):
            chunk = ids[start : start + _ID_CHUNK]
            handed_off.update(_handoff_dates(session, chunk))
            for result in session.execute(
                select(SapAdoptionResult)
                .where(SapAdoptionResult.recommendation_id.in_(chunk))
                .order_by(SapAdoptionResult.id)
            ).scalars():
                latest[result.recommendation_id] = result  # the newest id wins

        for row in rows:
            recommendation_id = row.recommendation_id
            handed_off_on = handed_off.get(recommendation_id)
            if handed_off_on is None:
                logger.warning(
                    "adoption check: %s is at %s but no final approval is on the ledger; no window to check",
                    recommendation_id, row.status,
                )
                counts["skipped"] += 1
                continue
            window_ends_on = handed_off_on + timedelta(days=window_days) if window_days is not None else None
            existing = latest.get(recommendation_id)
            if existing is not None and _already_final(existing, window_ends_on, today):
                counts["skipped"] += 1
                continue
            try:
                view = _build_view(session, row, policy, handed_off_on, today)
                _persist_result(session, recommendation_id, view.result, view.detail, stamped_at, _adopted_on(view))
            except Exception:  # noqa: BLE001 -- one bad recommendation must not stop the others
                session.rollback()
                logger.exception("adoption check failed for %s", recommendation_id)
                counts["failed"] += 1
                continue
            counts["checked"] += 1
            status = view.result.status
            if status is AdoptionStatus.ADOPTED:
                counts["adopted"] += 1
            elif status is AdoptionStatus.PARTIALLY_ADOPTED:
                counts["partially_adopted"] += 1
            elif status is AdoptionStatus.NOT_ADOPTED:
                counts["not_adopted"] += 1
            else:
                counts["pending"] += 1

    summary = AdoptionRunSummary(**counts)
    logger.info("adoption check for %s: %s", today.isoformat(), summary)
    return summary


@router.get(
    "/recommendations/{recommendation_id}/adoption",
    response_model=AdoptionResponse,
    summary="Get SAP adoption reconciliation status",
    description="ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED / UNKNOWN, "
    "computed read-only through the existing Phase 7 adoption evaluators, "
    "scoped to FR-9's configurable monitoring window, which starts when the "
    "final approval hands the recommendation to SAP, and persisted to the recommendation ledger "
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
        session, row, policy, _handoff_dates(session, [recommendation_id]).get(recommendation_id)
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

    _persist_result(session, recommendation_id, result, detail, adopted_on=_adopted_on(view))

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
            # Only material-plants with something to adopt: an OAR one eligible for the Min-Max
            # conversion, or a stock item with a changed reorder point, safety stock or maximum
            # stock. The vast majority of the catalogue never reaches a calculated value (see the
            # upstream criticality/history/lead-time data gaps), and a not-eligible OAR or an
            # unchanged stock item has nothing for an adoption check to reconcile against.
            actionable=True,
        )
    )

    total = session.execute(select(func.count()).select_from(base.subquery())).scalar_one()

    statement = (
        base.order_by(Recommendation.generated_at.desc(), Recommendation.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = session.execute(statement).scalars().all()

    policy = PolicyDocument()
    handed_off = _handoff_dates(session, [row.recommendation_id for row in rows])
    items = []
    for row in rows:
        view = _build_view(session, row, policy, handed_off.get(row.recommendation_id))
        _persist_result(session, row.recommendation_id, view.result, view.detail, adopted_on=_adopted_on(view))
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


@dataclass(frozen=True)
class AdoptionCounts:
    """The adoption ledger's counts over the recommendations that have something to adopt."""

    tracked: int
    adopted: int
    partially_adopted: int
    not_adopted: int

    @property
    def known(self) -> int:
        return self.adopted + self.partially_adopted + self.not_adopted

    @property
    def pending(self) -> int:
        """Everything SAP has no evidence about is pending, whether or not anyone has opened it: an UNKNOWN
        ledger row says the same thing as no row at all."""
        return max(self.tracked - self.known, 0)


def adoption_counts(
    session: Session,
    *,
    plant: str | None = None,
    demand_type: str | None = None,
    demand_class: str | None = None,
    criticality_level: str | None = None,
    value_change: Literal["released", "tied_up"] | None = None,
) -> AdoptionCounts:
    def tracked(*columns):
        """The newest recommendation per material-plant that has something to adopt -- the same
        set GET /recommendations?actionable=true lists."""
        return _latest_only(
            _apply_filters(
                select(*columns),
                status=None,
                plant=plant,
                material=None,
                demand_class=demand_class,
                is_oar=None,
                confidence=None,
                criticality=None,
                generated_from=None,
                generated_to=None,
                demand_type=demand_type,
                criticality_level=criticality_level,
                value_change=value_change,
                actionable=True,
            )
        )

    total_tracked = session.execute(
        select(func.count()).select_from(tracked(Recommendation.id).subquery())
    ).scalar_one()
    by_status = dict(
        session.execute(
            select(SapAdoptionResult.status, func.count())
            .where(SapAdoptionResult.recommendation_id.in_(tracked(Recommendation.recommendation_id)))
            .group_by(SapAdoptionResult.status)
        ).all()
    )
    return AdoptionCounts(
        tracked=total_tracked,
        adopted=by_status.get("ADOPTED", 0),
        partially_adopted=by_status.get("PARTIALLY_ADOPTED", 0),
        not_adopted=by_status.get("NOT_ADOPTED", 0),
    )


@router.get(
    "/recommendations/adoption/summary",
    response_model=AdoptionSummary,
    summary="Portfolio-wide SAP adoption counts, over every recommendation with values",
    description="Counts every material-plant with a recommended value (the newest "
    "recommendation for each, optionally for one plant) -- the same set the "
    "Adoption Tracking list shows. ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED come "
    "from the reconciliation ledger (i7_sap_adoption); everything with no SAP "
    "change evidence is counted as unknown_count, shown to people as Pending. "
    "ADOPTED / PARTIALLY_ADOPTED / NOT_ADOPTED are the results already in the "
    "ledger; this endpoint reads no SAP change history itself, so it stays fast. "
    "Feeds the Inventory Planning overview's adoption card and the quarterly "
    "report's SAP Adoption section.",
)
def get_adoption_summary(
    session: Annotated[Session, Depends(get_session)],
    plant: Annotated[str | None, Query(description="One SAP plant code; omit for every plant.")] = None,
    demand_type: Annotated[
        Literal["oar", "stock_item"] | None,
        Query(alias="type", description="oar, or stock_item (everything that is not OAR); omit for both."),
    ] = None,
    demand_class: Annotated[str | None, Query(description="The stored demand class.")] = None,
    criticality_level: Annotated[
        Literal["Critical", "High", "Medium", "Low"] | None,
        Query(description="The display level: CRITICAL / IMPACT / INSURANCE; Low is NORMAL, OBSOLETE or no tier."),
    ] = None,
    value_change: Annotated[
        Literal["released", "tied_up"] | None,
        Query(
            description="released / tied_up: count only recommendations whose Value change releases working capital "
            "(ROP goes down) or ties more up (ROP goes up)."
        ),
    ] = None,
) -> AdoptionSummary:
    counts = adoption_counts(
        session,
        plant=plant,
        demand_type=demand_type,
        demand_class=demand_class,
        criticality_level=criticality_level,
        value_change=value_change,
    )
    adopted, partially_adopted, not_adopted = counts.adopted, counts.partially_adopted, counts.not_adopted
    pending = counts.pending

    # The rate and the card's count are the formulas in force (the built-in ones unless somebody edited them
    # on the Calculation page). A formula that cannot be worked out -- by default, a rate with nothing known
    # to divide by -- gives None, never a fabricated 0%: an all-pending portfolio must read "not yet measured".
    formulas = load_active(session)
    figures = {
        "adopted": Decimal(adopted),
        "partially_adopted": Decimal(partially_adopted),
        "not_adopted": Decimal(not_adopted),
        "pending": Decimal(pending),
        "tracked": Decimal(counts.tracked),
    }
    rate = evaluate(formulas.parsed["adoption_rate"], figures)
    adoption_rate = round(float(rate * 100), 1) if isinstance(rate, Decimal) else None
    card = evaluate(formulas.parsed["adopted_card"], figures)
    counted_as_adopted = int(round(card)) if isinstance(card, Decimal) else None

    return AdoptionSummary(
        total_evaluated=counts.tracked,
        by_status=[
            AdoptionStatusCount(status="ADOPTED", count=adopted),
            AdoptionStatusCount(status="PARTIALLY_ADOPTED", count=partially_adopted),
            AdoptionStatusCount(status="NOT_ADOPTED", count=not_adopted),
            AdoptionStatusCount(status="UNKNOWN", count=pending),
        ],
        adopted_count=adopted,
        partially_adopted_count=partially_adopted,
        not_adopted_count=not_adopted,
        unknown_count=pending,
        adoption_rate_percentage=adoption_rate,
        counted_as_adopted=counted_as_adopted,
    )
