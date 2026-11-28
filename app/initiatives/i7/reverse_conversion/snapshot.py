"""Storing the Min-Max -> OAR recommendation, and reading it back.

The pipeline's ``reverse_conversion`` stage (it runs right after ``oar`` and before ``recommendations``) works
the recommendation out once and keeps it in ``i7_reverse_recommendation``. The page reads that stored run, so
what a planner sees is one fixed snapshot with a "last rebuilt" time, and a "Convert to OAR" request has a
stable row to point at.

THE STAGE NEVER HOLDS BACK THE PIPELINE. It is the one stage whose result nothing downstream reads, so a problem
here -- no usable calculation month, a database error, a bug -- is written to ``i7_reverse_run`` (status
``skipped`` or ``failed``, with the reason) and the stage still reports success. The recommendations after it,
and the stockout-risk rebuild, run as they always did. The previous stored run stays in use until a later one
succeeds, and the page shows how old it is.

Read-only toward SAP, and it writes nothing outside the ``i7_reverse_*`` tables.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import get_logger
from app.initiatives.i7.policy.reverse_conversion import (
    ReverseConversionPolicy,
    current_reverse_conversion_policy,
)
from app.initiatives.i7.reverse_conversion import cooldown, rationale
from app.initiatives.i7.reverse_conversion.errors import AnchorError
from app.initiatives.i7.reverse_conversion.service import (
    CONFIDENCE_CODES,
    REASON_BY_CODE,
    RecommendationRow,
    ReverseConversionSource,
    ReverseReport,
    SqlSource,
    assemble_report,
    build_report,
    reason_code,
    resolve_report_anchor,
)
from app.initiatives.i7.reverse_conversion.types import (
    AnchorResolution,
    ConsumptionMetrics,
    ReverseCategory,
    ReverseConfidence,
)
from app.models.i7_reverse import (
    ReverseConversionRequest,
    RUN_FAILED,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    ReverseRecommendation,
    ReverseRun,
)

logger = get_logger(__name__)

#: How many stored runs keep their rows. The newest is what the page reads; the one before it is a fallback.
KEEP_RUNS = 2

#: The pipeline's own word for a stage that did its job. Spelled out here so the reverse feature does not import
#: the pipeline module (which imports every stage).
_PIPELINE_SUCCEEDED = "succeeded"


@dataclass(frozen=True)
class StageOutcome:
    """What the pipeline reads from a stage: a status, an error, a run id and a count."""

    status: str
    error: str | None = None
    run_id: int | None = None
    recommendations_written: int = 0


# --- Writing ----------------------------------------------------------------------------------


def _excluded_by_plant(report: ReverseReport) -> str:
    return ";".join(
        f"{plant}={population - categorised}" for plant, (population, categorised) in report.reconciliation.by_plant.items()
    )


def _row_model(run_id: int, order: int, row: RecommendationRow) -> ReverseRecommendation:
    m = row.metrics
    return ReverseRecommendation(
        run_id=run_id,
        sort_order=order,
        sap_material_number=row.material,
        sap_plant_code=row.plant,
        description=(row.description or None) and row.description[:255],
        mrp_type=row.mrp_type,
        criticality=row.criticality,
        category=row.category.value,
        reason=reason_code(row.reason),
        detail=row.detail[:1000],
        decision_rule=row.rule,
        confidence=row.confidence.value if row.confidence else None,
        points=json.dumps(row.points),
        points_source=row.points_source,
        issues_latest_12m=m.issues_latest_window,
        months_with_use_12m=m.months_with_use_latest_window,
        issues_prior_12m=m.issues_prior_window,
        first_valid_month=m.first_valid_month,
        last_valid_month=m.last_valid_month,
        months_since_last_movement=m.months_since_last_movement,
        history_span_months=m.history_span_months,
        lifetime_months_with_use=m.lifetime_months_with_use,
        lead_time_days=row.lead_time_days,
        lead_time_bucket=row.lead_time_bucket,
        current_stock=row.current_stock,
        unit_price=row.unit_price,
        price_currency=row.price_currency,
        value_currency=row.value_currency,
        stock_value=row.stock_value,
        minbe=row.minbe,
        mabst=row.mabst,
    )


def write_report(session: Session, report: ReverseReport, *, pipeline_run_id: int | None = None) -> ReverseRun:
    """Store ``report`` as a new succeeded run (rows included) and drop the rows of runs beyond the newest two.

    One transaction: a half-written run is never visible. Does not commit -- the caller does."""
    run = ReverseRun(
        status=RUN_SUCCEEDED,
        pipeline_run_id=pipeline_run_id,
        anchor_month=report.anchor.month,
        anchor_source=report.anchor.source,
        latest_staged_month=report.anchor.latest_staged_month,
        policy_version=report.policy.policy_version,
        source_population=report.reconciliation.source_population,
        excluded=report.reconciliation.excluded,
        duplicate_keys=report.reconciliation.duplicate_keys,
        rows_written=len(report.rows),
        excluded_by_plant=_excluded_by_plant(report),
        finished_at=datetime.now(timezone.utc),
    )
    session.add(run)
    session.flush()  # the run id
    session.add_all(_row_model(run.id, order, row) for order, row in enumerate(report.rows))
    session.flush()
    prune_old_runs(session)
    return run


def prune_old_runs(session: Session) -> int:
    """Delete the rows of every succeeded run except the newest :data:`KEEP_RUNS`. The run rows themselves stay:
    they are the history of what was built, and they are small."""
    ids = list(session.scalars(select(ReverseRun.id).where(ReverseRun.status == RUN_SUCCEEDED).order_by(ReverseRun.id.desc())))
    old = ids[KEEP_RUNS:]
    if not old:
        return 0
    return session.execute(delete(ReverseRecommendation).where(ReverseRecommendation.run_id.in_(old))).rowcount or 0


def _record(session_factory, *, status: str, note: str, pipeline_run_id: int | None, policy: ReverseConversionPolicy) -> int | None:
    """Write a skipped or failed run, best effort: losing this row must not turn into a second failure."""
    try:
        with session_factory() as session:
            run = ReverseRun(
                status=status,
                pipeline_run_id=pipeline_run_id,
                policy_version=policy.policy_version,
                finished_at=datetime.now(timezone.utc),
                note=note[:4000],
            )
            session.add(run)
            session.commit()
            return run.id
    except Exception:  # noqa: BLE001 -- the stage must not fail the pipeline
        logger.exception("reverse conversion: could not record the %s run", status)
        return None


def _record_groups(session_factory, policy: ReverseConversionPolicy, today: date) -> None:
    """Note which group (Min-Max or OAR) each material-plant is in now, so a change since the last run is dated.
    Its own transaction, run before anything else: a change must be noticed even when the rest is skipped. A failure
    here is logged and the stage carries on (the cooldown then works from the groups it last saw)."""
    try:
        with session_factory() as session:
            changes = cooldown.record_observations(session, policy, get_settings().i7_oar_mrp_type_set, today)
            session.commit()
        if changes.changed:
            logger.info("reverse conversion: %d material-plant(s) changed between Min-Max and OAR since the last run", len(changes.changed))
    except Exception:  # noqa: BLE001 -- never fails the pipeline
        logger.exception("reverse conversion: could not record planning groups; the cooldown uses the last ones seen")


def _with_written_reasons(report: ReverseReport, workers: int) -> ReverseReport:
    """``report`` with each Convert to OAR reason written by the model where possible (see ``rationale``).

    Wording only -- no category, confidence or figure changes -- and any failure keeps the standard wording."""
    try:
        return replace(report, rows=tuple(rationale.explain_rows(list(report.rows), workers)))
    except Exception:  # noqa: BLE001 -- the reasons are optional; the calculation must still be stored
        logger.exception("reverse conversion reasons failed; the standard wording is kept")
        return report


def run_reverse_conversion(
    *,
    pipeline_run_id: int | None = None,
    session_factory=None,
    source_factory=None,
    today: date | None = None,
) -> StageOutcome:
    """The pipeline stage: calculate the recommendation and store it. Never raises, never fails the pipeline.

    ``session_factory``, ``source_factory`` and ``today`` are seams for tests; production passes none of them."""
    policy = current_reverse_conversion_policy()
    today = today or datetime.now(timezone.utc).date()
    if session_factory is None:
        from app.core.db import get_sessionmaker

        session_factory = get_sessionmaker()
    _record_groups(session_factory, policy, today)
    try:
        with session_factory() as session:
            source: ReverseConversionSource = source_factory(session) if source_factory else SqlSource(session, policy)
            settings = get_settings()
            try:
                anchor = resolve_report_anchor(
                    source,
                    configured_month=settings.i7_reverse_anchor_month,
                    anchor_source=settings.i7_reverse_anchor_source,
                )
            except AnchorError as exc:
                run_id = _record(session_factory, status=RUN_SKIPPED, note=str(exc), pipeline_run_id=pipeline_run_id, policy=policy)
                logger.warning("reverse conversion skipped: %s", exc)
                return StageOutcome(status=_PIPELINE_SUCCEEDED, run_id=run_id)
            report = build_report(source, policy, anchor, as_of=today)
            report = _with_written_reasons(report, settings.i7_rationale_concurrency)
            run = write_report(session, report, pipeline_run_id=pipeline_run_id)
            session.commit()
            logger.info(
                "reverse conversion: run %d stored %d material-plants measured to %s",
                run.id, len(report.rows), f"{anchor.month:%Y-%m}",
            )
            return StageOutcome(status=_PIPELINE_SUCCEEDED, run_id=run.id, recommendations_written=len(report.rows))
    except Exception as exc:  # noqa: BLE001 -- recorded, never propagated: nothing downstream reads this stage
        logger.exception("reverse conversion failed; the pipeline carries on")
        run_id = _record(
            session_factory,
            status=RUN_FAILED,
            note=f"{type(exc).__name__}: {exc}",
            pipeline_run_id=pipeline_run_id,
            policy=policy,
        )
        return StageOutcome(status=_PIPELINE_SUCCEEDED, run_id=run_id)


# --- Reading ----------------------------------------------------------------------------------


def latest_run(session: Session) -> ReverseRun | None:
    """The newest succeeded run, or None before the first one."""
    return session.scalars(
        select(ReverseRun).where(ReverseRun.status == RUN_SUCCEEDED).order_by(ReverseRun.id.desc()).limit(1)
    ).first()


def latest_attempt(session: Session) -> ReverseRun | None:
    """The newest run of any status: what the stage last did, even if it was skipped or failed."""
    return session.scalars(select(ReverseRun).order_by(ReverseRun.id.desc()).limit(1)).first()


def find_stored(session: Session, material: str, plant: str) -> tuple[ReverseRun, ReverseRecommendation] | None:
    """One material-plant's row in the newest stored run, with that run; None when it is not in it."""
    run = latest_run(session)
    if run is None:
        return None
    stored = session.scalars(
        select(ReverseRecommendation).where(
            ReverseRecommendation.run_id == run.id,
            ReverseRecommendation.sap_material_number == material,
            ReverseRecommendation.sap_plant_code == plant,
        )
    ).first()
    return (run, stored) if stored is not None else None


def latest_request_briefs(session: Session, keys: list[tuple[str, str]]) -> dict[tuple[str, str], tuple[int, str, str | None]]:
    """``(id, status, pending_role)`` of the newest conversion request of each material-plant in ``keys`` that has
    one. Plain SQL over the request table: listing must not load the approval machinery."""
    if not keys:
        return {}
    wanted = set(keys)
    newest: dict[tuple[str, str], tuple[int, str, str | None]] = {}
    for request in session.scalars(
        select(ReverseConversionRequest)
        .where(ReverseConversionRequest.sap_material_number.in_(sorted({material for material, _ in keys})))
        .order_by(ReverseConversionRequest.id)
    ):
        key = (request.sap_material_number, request.sap_plant_code)
        if key in wanted:
            newest[key] = (request.id, request.status, request.pending_role)  # ascending ids: the last one wins
    return newest


def _to_row(stored: ReverseRecommendation, anchor_month) -> RecommendationRow:
    metrics = ConsumptionMetrics(
        anchor=anchor_month,
        issues_latest_window=stored.issues_latest_12m,
        months_with_use_latest_window=stored.months_with_use_12m,
        issues_prior_window=stored.issues_prior_12m,
        months_with_use_prior_window=0,
        first_valid_month=stored.first_valid_month,
        last_valid_month=stored.last_valid_month,
        lifetime_months_with_use=stored.lifetime_months_with_use,
        history_span_months=stored.history_span_months,
        months_since_last_movement=stored.months_since_last_movement,
        material_age_months=None,
    )
    return RecommendationRow(
        material=stored.sap_material_number,
        description=stored.description,
        plant=stored.sap_plant_code,
        mrp_type=stored.mrp_type,
        criticality=stored.criticality,
        category=ReverseCategory(stored.category),
        reason=REASON_BY_CODE[stored.reason],
        detail=stored.detail,
        rule=stored.decision_rule,
        confidence=ReverseConfidence(stored.confidence) if stored.confidence else None,
        points=_load_points(stored.points, stored.detail),
        points_source=stored.points_source or "DETERMINISTIC_FALLBACK",
        metrics=metrics,
        lead_time_days=stored.lead_time_days,
        lead_time_bucket=stored.lead_time_bucket,
        current_stock=stored.current_stock,
        unit_price=stored.unit_price,
        price_currency=stored.price_currency,
        value_currency=stored.value_currency,
        stock_value=stored.stock_value,
        minbe=stored.minbe,
        mabst=stored.mabst,
    )


def _load_points(text: str | None, detail: str) -> tuple[tuple[str, str], ...]:
    """The stored labelled lines; a row written before they existed falls back to its one-paragraph reason."""
    if text:
        try:
            return tuple((str(label), str(line)) for label, line in json.loads(text))
        except (ValueError, TypeError):
            pass
    return (("What we saw", detail),)


def _parse_excluded(text: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for part in (text or "").split(";"):
        plant, _, count = part.partition("=")
        if plant and count.lstrip("-").isdigit():
            out[plant] = int(count)
    return out


def load_latest_report(session: Session) -> ReverseReport | None:
    """The newest stored run as a report, or None when the stage has not stored one yet."""
    run = latest_run(session)
    if run is None or run.anchor_month is None:
        return None
    stored = list(
        session.scalars(
            select(ReverseRecommendation).where(ReverseRecommendation.run_id == run.id).order_by(ReverseRecommendation.sort_order)
        )
    )
    rows = [_to_row(item, run.anchor_month) for item in stored]
    excluded_by_plant = _parse_excluded(run.excluded_by_plant)
    population_by_plant: dict[str, int] = {}
    for row in rows:
        population_by_plant[row.plant] = population_by_plant.get(row.plant, 0) + 1
    for plant, count in excluded_by_plant.items():
        population_by_plant[plant] = population_by_plant.get(plant, 0) + count
    anchor = AnchorResolution(
        month=run.anchor_month,
        source=run.anchor_source or "configured",
        latest_staged_month=run.latest_staged_month,
        data_loaded_at=None,
    )
    return assemble_report(
        rows,
        anchor,
        current_reverse_conversion_policy(),
        source_population=run.source_population,
        excluded=run.excluded,
        duplicate_keys=run.duplicate_keys,
        population_by_plant=population_by_plant,
        generated_at=run.finished_at,
        run_id=run.id,
    )


__all__ = [
    "KEEP_RUNS",
    "CONFIDENCE_CODES",
    "StageOutcome",
    "find_stored",
    "latest_attempt",
    "latest_request_briefs",
    "latest_run",
    "load_latest_report",
    "prune_old_runs",
    "run_reverse_conversion",
    "write_report",
]
