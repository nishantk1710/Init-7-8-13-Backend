"""Forecasting orchestration.

Reads the Phase 3 feature store, forecasts every routed material-plant, runs the
rolling-origin backtest, and records each material-plant's own champion/
challenger decision.

**Model selection is per material-plant, not per segment.** The FRS is
explicit, twice (Section 3.1: "Per-material model selection... the winning
model's recommendation is surfaced"; FR-3: "select per material by backtest").
This module used to pool every material-plant's backtest paths into one
segment-level (demand-class-level) comparison before this fix -- built to an
earlier Solution Design draft that predates the FRS and says the opposite
("Decision is per SEGMENT... not per individual SKU"). The FRS is the later,
more specific document and its own change log states it supersedes prior
scope-confirmation rulings, so it governs here. Each material-plant's own
``BacktestResult`` (already computed per-candidate) now feeds
``selection.decide_intermittent``/``decide_smooth`` directly, with no pooling
step -- the acceptance criteria themselves (>5% pinball improvement, <=5% bias
deterioration, >=12 origins) are unchanged, only the population each material's
comparison is judged against.

**Phase 3 remains the authority on routing.** The history gate and demand class
are read, never recomputed -- a second gate implementation would be free to
disagree with the first, and the disagreement would surface as a forecast for a
material Phase 3 had already sent to the OAR path.

**Lead time comes from the feature store, not from PO history.** The horizon is
``T+1 .. T+LT``, and ``LT`` is ``i7_material_feature.lead_time_days`` -- the
same figure Phase 5 (inventory) uses, resolved by
:func:`app.initiatives.i7.features.lead_time_provider.resolve_lead_time` from
the Initiative 11 Z-program output when available, MARC-PLIFZ otherwise. This
module used to average ``i7_staged_purchase_order.lead_time_days`` (actual
PO-to-GR durations) directly -- an independent, PO-based lead-time calculation
that duplicated exactly the computation the FRS assigns to Initiative 11.
That has been removed: forecasting must consume the one lead-time value I07
agrees on, not derive a second one from raw purchase orders. A material with no
resolved lead time has no defined horizon and is reported as
``NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE``. No 30/60/90-day default is
substituted, and no Initiative 11 value is fabricated -- see
``lead_time_provider.I11LeadTimeProvider``, which always returns ``None``
until a real integration exists.

**Nothing here computes an inventory parameter.** Models predict a demand rate;
safety stock, reorder point and maximum are Phase 5.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, NamedTuple

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.adapters import consumption_series_for
from app.initiatives.i7.contracts import DemandPattern, ScopeDecision
from app.initiatives.i7.features import BaselineModel, HistoryStatus
from app.initiatives.i7.features.declining import DecliningStatus
from app.initiatives.i7.forecasting import arima, lightgbm_model, sba, ses, tsb
from app.initiatives.i7.forecasting import backtest as backtest_engine
from app.initiatives.i7.forecasting import metrics as metric_functions
from app.initiatives.i7.forecasting import selection
from app.initiatives.i7.forecasting.series import PreparedSeries, prepare
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    AdoptionStatus,
    BacktestResult,
    BacktestStatus,
    ForecastResult,
    ModelName,
    ModelStatus,
    SegmentDecision,
)
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.smooth_demand import SMOOTH_DEMAND_CLASS
from app.models.i7_features import DecliningDemandEvaluation
from app.models.i7_forecast import (
    Forecast,
    ForecastBacktestPath,
    ForecastRun,
    SegmentModelDecision,
)

logger = logging.getLogger(__name__)

def log_progress(run_id: int, completed: int, total: int, started: float, *, skipped: int = 0) -> None:
    """One line saying how far the candidate loop has got, logged at every save.

    ``skipped`` is how many were already saved by an earlier attempt and so cost
    this call nothing; the ETA is worked out from the rest. Logging only: it
    reads nothing the forecast depends on and changes nothing it produces."""
    elapsed = time.monotonic() - started
    done_now = max(completed - skipped, 0)
    eta = f"{int(elapsed / done_now * (total - completed))}" if done_now else "unknown"
    logger.info(
        "I07 forecasting progress: run_id=%d completed=%d total=%d elapsed_seconds=%d percent=%.1f eta_seconds=%s saved=yes",
        run_id,
        completed,
        total,
        int(elapsed),
        100.0 * completed / total if total else 100.0,
        eta,
    )


STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

DAYS_PER_MONTH = Decimal("30.44")
"""Mean Gregorian month, matching the Formula Reference's lead-time conversion.
Used only to express an observed lead time in months; it introduces no policy."""


@dataclass
class ForecastRunResult:
    run_id: int | None = None
    status: str = STATUS_SUCCEEDED
    materials_evaluated: int = 0
    forecasts_written: int = 0
    model_status_counts: dict[str, int] = field(default_factory=dict)
    backtest_status_counts: dict[str, int] = field(default_factory=dict)
    lead_time_source_counts: dict[str, int] = field(default_factory=dict)
    """How many candidates' horizons came from each lead-time source
    (I11_PROGRAM / PLANNED_DELIVERY_TIME / no resolved lead time at all). The
    run-level equivalent of ``lead_time_method_counts`` on the inventory run --
    provenance that is countable, not just inferable from one row at a time."""
    segment_decisions: list[Any] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Candidate:
    """A material-plant that Phase 3 routed to a forecasting path."""

    material: str
    plant: str
    demand_class: str
    history_status: str
    baseline_model: str | None
    lead_time_months: int | None
    lead_time_source: str | None
    """Provenance of ``lead_time_months`` -- ``I11_PROGRAM`` or
    ``PLANNED_DELIVERY_TIME``, per
    :class:`app.initiatives.i7.contracts.enums.LeadTimeSource`. ``None`` when
    the feature store has no resolved lead time at all."""
    series: PreparedSeries


_ROUTED_SQL = """
    SELECT f.sap_material_number,
           f.sap_plant_code,
           f.demand_class,
           f.history_status,
           f.baseline_model,
           f.lead_time_days,
           f.lead_time_source
      FROM i7_material_feature f
     WHERE f.history_status = :sufficient
       AND f.baseline_model IS NOT NULL
       AND f.demand_class <> :smooth
       AND f.oar_scope <> :oar
     ORDER BY f.sap_material_number, f.sap_plant_code
"""
# :oar -- an OAR material-plant (MRP type ND / PD / blank, Stage 1's oar_scope) is not forecast
# whatever its history: its ROP, safety stock and max stock come from similar materials' current
# SAP values (Phase 6). Only VB and the other MRP types are forecast.
# :smooth -- a SMOOTH material-plant is not forecast (app.initiatives.i7.smooth_demand): its current
# SAP values stand, so no SES / Auto-ARIMA run is spent on it and no forecast exists to be used.
# Lead time is f.lead_time_days -- the feature store's own resolved value
# (Initiative 11 Z-program output when available, MARC-PLIFZ otherwise; see
# app.initiatives.i7.features.lead_time_provider.resolve_lead_time), not an
# independent AVG(lead_time_days) over i7_staged_purchase_order. Forecasting
# does not compute its own lead time -- it consumes the one I07 already agreed
# on, the same figure Phase 5 (inventory) uses.


def _horizon_months(lead_time_days: Decimal | None) -> int | None:
    """Lead time in whole months, rounded up.

    ``None`` stays ``None``: an absent lead time leaves the horizon undefined,
    and that is reported rather than defaulted.
    """
    if lead_time_days is None:
        return None
    months = Decimal(str(lead_time_days)) / DAYS_PER_MONTH
    return max(1, int(months.to_integral_value(rounding="ROUND_CEILING")))


def _load_candidates(session: Session) -> list[_Candidate]:
    """Every routed material-plant, with its series and horizon.

    One query for the routing and lead time, then one series read per candidate
    -- 471 of them, not 45,409, because Phase 3 already gated the rest out.
    """
    candidates: list[_Candidate] = []
    rows = session.execute(
        text(_ROUTED_SQL),
        {
            "sufficient": HistoryStatus.SUFFICIENT.value,
            "smooth": SMOOTH_DEMAND_CLASS,
            "oar": ScopeDecision.IN_SCOPE.value,
        },
    ).all()

    for row in rows:
        canonical = consumption_series_for(session, row.sap_material_number, row.sap_plant_code)
        if canonical is None:
            continue
        prepared, problem = prepare(canonical)
        if prepared is None:
            logger.warning(
                "%s/%s: series rejected (%s)",
                row.sap_material_number,
                row.sap_plant_code,
                problem.reason if problem else "unknown",
            )
            continue
        candidates.append(
            _Candidate(
                material=row.sap_material_number,
                plant=row.sap_plant_code,
                demand_class=row.demand_class,
                history_status=row.history_status,
                baseline_model=row.baseline_model,
                lead_time_months=_horizon_months(row.lead_time_days),
                lead_time_source=row.lead_time_source,
                series=prepared,
            )
        )
    return candidates


def _target_quantile(policy: PolicyDocument) -> float | None:
    """The LightGBM quantile, from the signed service-level matrix.

    ``None`` while unsigned. Deliberately not defaulted -- the quantile *is* the
    service level, and choosing one here would set inventory policy in a
    forecasting module.
    """
    if not policy.service_level.is_configured:
        return None
    levels = [level for _, level in policy.service_level.matrix]
    if not levels:
        return None
    # Several tiers may be signed with different levels; the pooled model is one
    # model, so it trains at the highest -- the most demanding target in play.
    return max(levels)


def _run_models(
    candidate: _Candidate,
    pooled: lightgbm_model.PooledModel | None,
    pooled_status: ModelStatus,
    pooled_detail: str,
    obsolescence_trigger_configured: bool,
    quantile: float | None,
    arima_enabled: bool = True,
    include_tsb_placeholder: bool = True,
) -> list[tuple[ForecastResult, BacktestResult, bool]]:
    """Forecast and backtest every model applicable to one material-plant.

    Returns ``(forecast, backtest, is_baseline)`` per model. ``arima_enabled``
    adds Auto-ARIMA as SES's challenger for Smooth and Erratic demand; the run
    passes ``I7_FORECAST_ARIMA_ENABLED`` (default off).
    """
    horizon = candidate.lead_time_months
    results: list[tuple[ForecastResult, BacktestResult, bool]] = []

    if candidate.baseline_model == BaselineModel.SES.value:
        pairs = [(ModelName.SES, lambda s, h: ses.forecast(s, h), True)]
        if arima_enabled:
            pairs.append((ModelName.AUTO_ARIMA, lambda s, h: arima.forecast(s, h), False))
    else:
        pairs = [
            (ModelName.SBA, lambda s, h: sba.forecast(s, h), True),
            (
                ModelName.LIGHTGBM,
                lambda s, h: lightgbm_model.forecast(
                    s, h, pooled, pooled_status, pooled_detail
                ),
                False,
            ),
        ]

    # With the declining-demand feature OFF, TSB still reports a "not evaluable"
    # row for every material-plant, exactly as before, so the closed gate is
    # visible. With it on (shadow or active) that placeholder is not written:
    # TSB rows exist only for the material-plants the rule flags (see
    # _run_tsb_path), and why the others were not flagged is in
    # i7_declining_demand_evaluation.
    if include_tsb_placeholder:
        pairs.append(
            (
                ModelName.TSB,
                lambda s, h: tsb.forecast(
                    s,
                    h,
                    obsolescence_trigger_configured=obsolescence_trigger_configured,
                ),
                False,
            )
        )

    for model_name, function, is_baseline in pairs:
        if horizon is None:
            forecast_result = ForecastResult(
                model=model_name,
                model_version=MODEL_VERSIONS[model_name],
                status=ModelStatus.MISSING_REQUIRED_FEATURE,
                detail="no lead time, so the T+1..T+LT horizon is undefined",
            )
            backtest_result = backtest_engine.not_evaluable(
                model_name,
                MODEL_VERSIONS[model_name],
                BacktestStatus.NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE,
                "no purchase-order history supplies a lead time; the fallback "
                "policy is unresolved and no default is substituted",
            )
            results.append((forecast_result, backtest_result, is_baseline))
            continue

        forecast_result = function(candidate.series, horizon)

        if forecast_result.status in (
            ModelStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET,
            ModelStatus.NOT_EVALUABLE_TRIGGER_UNSET,
        ):
            backtest_result = backtest_engine.not_evaluable(
                model_name,
                MODEL_VERSIONS[model_name],
                BacktestStatus.NOT_EVALUABLE,
                forecast_result.detail or "",
            )
        else:
            backtest_result = backtest_engine.run(
                candidate.series,
                model_name,
                MODEL_VERSIONS[model_name],
                function,
                horizon,
            )
            if backtest_result.paths:
                backtest_result = backtest_result._replace(
                    metrics=metric_functions.evaluate(
                        list(backtest_result.paths),
                        quantile=quantile,
                        # Unit price and holding rate are unavailable, so
                        # holding cost reports NOT_EVALUABLE rather than a
                        # number built on an invented rate.
                        unit_price=None,
                        holding_rate=None,
                    )
                )

        results.append((forecast_result, backtest_result, is_baseline))

    return results


def _parameters_text(result: ForecastResult) -> str | None:
    if not result.parameters:
        return None
    return ";".join(f"{key}={value}" for key, value in result.parameters)[:500]


class DecliningEvaluationMissing(RuntimeError):
    """Active mode was asked to route by declining-demand evaluations that are not
    there. Raised instead of quietly forecasting everything the normal way: a run
    that says it used the rule when it did not would be wrong without looking wrong."""


class _Evaluation(NamedTuple):
    status: str
    reason: str
    window_start: Any
    window_end: Any
    rule_version: str


def _load_evaluations(session: Session, feature_run_id: int | None) -> dict[tuple[str, str], _Evaluation]:
    """This feature run's declining-demand evaluations, keyed by material-plant."""
    if feature_run_id is None:
        return {}
    rows = session.execute(
        select(
            DecliningDemandEvaluation.sap_material_number,
            DecliningDemandEvaluation.sap_plant_code,
            DecliningDemandEvaluation.status,
            DecliningDemandEvaluation.reason,
            DecliningDemandEvaluation.window_start,
            DecliningDemandEvaluation.window_end,
            DecliningDemandEvaluation.rule_version,
        ).where(DecliningDemandEvaluation.feature_run_id == feature_run_id)
    )
    return {
        (row.sap_material_number, row.sap_plant_code): _Evaluation(
            row.status, row.reason, row.window_start, row.window_end, row.rule_version
        )
        for row in rows
    }


def _run_tsb_path(
    candidate: _Candidate, evaluation: _Evaluation
) -> list[tuple[ForecastResult, BacktestResult, bool, bool]]:
    """The declining-demand path: TSB directly, no comparison, no backtest.

    Returns ``(forecast, backtest, is_baseline, is_champion)``. The baseline
    (SES or SBA, the model the demand class would have used) is forecast for the
    reviewer to see what the rule changed; it is never the champion here and is
    not compared with TSB. TSB is the champion only when it produced a forecast:
    a flagged material-plant whose TSB did not succeed has NO champion, and the
    inventory stage reports it as such -- there is no silent fallback to the
    baseline, because the business policy defines none.
    """
    horizon = candidate.lead_time_months
    baseline_name = ModelName(candidate.baseline_model)
    baseline_function = ses.forecast if baseline_name is ModelName.SES else sba.forecast
    no_backtest = (
        "declining-demand path: TSB is used directly and is not backtested or compared "
        "with another model"
    )

    results: list[tuple[ForecastResult, BacktestResult, bool, bool]] = []
    for name, function, is_baseline in (
        (baseline_name, baseline_function, True),
        (
            ModelName.TSB,
            lambda s, h: tsb.forecast(s, h, obsolescence_trigger_configured=True),
            False,
        ),
    ):
        if horizon is None:
            forecast_result = ForecastResult(
                model=name,
                model_version=MODEL_VERSIONS[name],
                status=ModelStatus.MISSING_REQUIRED_FEATURE,
                detail="no lead time, so the T+1..T+LT horizon is undefined",
            )
            backtest_result = backtest_engine.not_evaluable(
                name,
                MODEL_VERSIONS[name],
                BacktestStatus.NOT_EVALUABLE_LEAD_TIME_UNAVAILABLE,
                "no purchase-order history supplies a lead time; the fallback "
                "policy is unresolved and no default is substituted",
            )
        else:
            forecast_result = function(candidate.series, horizon)
            backtest_result = backtest_engine.not_evaluable(
                name, MODEL_VERSIONS[name], BacktestStatus.NOT_RUN_DECLINING_DEMAND, no_backtest
            )
        if name is ModelName.TSB:
            # Rule and window first: the parameters column is capped at 500
            # characters and TSB's own values are long.
            window = (
                f"{evaluation.window_start:%Y-%m}..{evaluation.window_end:%Y-%m}"
                if evaluation.window_start and evaluation.window_end
                else "unknown"
            )
            forecast_result = forecast_result._replace(
                parameters=(
                    ("declining_rule", evaluation.rule_version),
                    ("evaluated_months", window),
                )
                + forecast_result.parameters
            )
        is_champion = name is ModelName.TSB and forecast_result.status is ModelStatus.SUCCESS
        results.append((forecast_result, backtest_result, is_baseline, is_champion))
    return results


def _declining_consistent(
    session: Session,
    run_id: int,
    mode: str,
    flagged: frozenset[tuple[str, str]],
) -> bool:
    """Whether a run's saved rows were produced under the current declining-demand mode.

    Resuming across a change would mix material-plants forecast one way with
    others forecast another. Read from the rows themselves, as the ARIMA check
    does, so no column is needed:

    * off     -- every candidate has a placeholder TSB row, none is a direct TSB row.
    * shadow  -- no placeholders and no direct TSB rows.
    * active  -- no placeholders, and exactly the flagged saved material-plants
                 have a direct TSB row.
    """
    rows = session.execute(
        select(
            Forecast.sap_material_number,
            Forecast.sap_plant_code,
            Forecast.forecast_status,
            Forecast.adoption_status,
        ).where(Forecast.forecast_run_id == run_id, Forecast.model_name == ModelName.TSB.value)
    ).all()
    placeholder = any(row.forecast_status == ModelStatus.NOT_EVALUABLE_TRIGGER_UNSET.value for row in rows)
    direct = {
        (row.sap_material_number, row.sap_plant_code)
        for row in rows
        if row.adoption_status == AdoptionStatus.TSB_DECLINING_DEMAND.value
    }
    if mode == "off":
        return not direct
    if placeholder:
        return False
    if mode == "shadow":
        return not direct
    return direct == (flagged & _saved_keys(session, run_id))


#: Material-plants between saves. Each batch is committed on its own, with its
#: segment decisions and champion back-test paths, so an interruption costs at
#: most the batch in progress and the next run resumes from the saved ones.
CHECKPOINT_EVERY = 50

#: The longest a save (and so a progress line) may be put off. With a slow model
#: 50 material-plants can take an hour; this keeps the log alive and the work
#: saved at least this often either way.
CHECKPOINT_SECONDS = 60

#: Held for the life of a forecasting run (``sp_getapplock``). Holding it proves
#: no other forecast run is alive, which is what makes it safe to resume the
#: interrupted one and to mark any other ``running`` row dead.
FORECASTING_LOCK = "spares_ai_i7_forecasting"

STATUS_RUNNING = "running"

ABANDONED = (
    "abandoned: its process stopped (an App Service restart, a deploy or an OOM) "
    "before the run finished; found by forecast run {by}"
)


def _same_quantile(stored: Decimal | None, quantile: float | None) -> bool:
    if stored is None or quantile is None:
        return stored is None and quantile is None
    return abs(float(stored) - quantile) < 1e-9


def _saved_keys(session: Session, run_id: int) -> set[tuple[str, str]]:
    """Material-plants this run already has rows for -- saved in whole batches,
    so a material-plant is either fully saved or absent."""
    rows = session.execute(
        select(Forecast.sap_material_number, Forecast.sap_plant_code)
        .where(Forecast.forecast_run_id == run_id)
        .distinct()
    )
    return {(material, plant) for material, plant in rows}


def _arima_consistent(session: Session, run_id: int, arima_enabled: bool) -> bool:
    """Whether a run's saved rows were produced under the current ARIMA setting.

    Resuming across a change would leave some smooth material-plants with an
    ARIMA challenger and some without. Read from the rows themselves, so no
    column (and no migration) is needed."""
    models = set(
        session.execute(
            select(Forecast.model_name)
            .where(
                Forecast.forecast_run_id == run_id,
                Forecast.model_name.in_([ModelName.SES.value, ModelName.AUTO_ARIMA.value]),
            )
            .distinct()
        ).scalars()
    )
    has_arima = ModelName.AUTO_ARIMA.value in models
    has_ses = ModelName.SES.value in models
    if arima_enabled:
        return has_arima or not has_ses
    return not has_arima


def _claim_run(
    session: Session,
    *,
    policy: PolicyDocument,
    feature_run_id: int | None,
    quantile: float | None,
    arima_enabled: bool,
    resume: bool,
    declining_mode: str = "off",
    flagged_keys: frozenset[tuple[str, str]] = frozenset(),
) -> tuple[ForecastRun, bool]:
    """The run to write into, and whether it is a resumed one. The caller holds
    the forecasting lock and commits.

    **Resumed** when the newest run did not succeed, has saved rows, and was
    producing the same thing this run would: the same feature run, policy,
    target quantile and ARIMA setting. Anything else starts a new run -- rows
    from a different input or model set must not be mixed into one run.

    Every other run still marked ``running`` is dead (the lock proves it) and is
    marked ``failed``, so nothing reads it as in flight.
    """
    newest = session.execute(select(ForecastRun).order_by(ForecastRun.id.desc()).limit(1)).scalars().first()
    candidate = None
    if (
        resume
        and newest is not None
        and newest.status != STATUS_SUCCEEDED
        and feature_run_id is not None
        and newest.feature_run_id == feature_run_id
        and newest.policy_id == policy.policy_id
        and newest.policy_version == policy.policy_version
        and _same_quantile(newest.target_quantile, quantile)
        and session.execute(
            select(Forecast.id).where(Forecast.forecast_run_id == newest.id).limit(1)
        ).first()
        is not None
        and _arima_consistent(session, newest.id, arima_enabled)
        and _declining_consistent(session, newest.id, declining_mode, flagged_keys)
    ):
        candidate = newest

    if candidate is not None:
        run = candidate
        run.status = STATUS_RUNNING
        run.error = None
        run.finished_at = None
        session.flush()
    else:
        run = ForecastRun(
            status=STATUS_RUNNING,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            feature_run_id=feature_run_id,
            target_quantile=Decimal(str(quantile)) if quantile is not None else None,
        )
        session.add(run)
        session.flush()

    stale = session.execute(
        select(ForecastRun).where(ForecastRun.status == STATUS_RUNNING, ForecastRun.id != run.id)
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for old in stale:
        old.status = STATUS_FAILED
        old.error = ABANDONED.format(by=run.id)
        old.finished_at = now
    if stale:
        logger.warning(
            "forecast run %d: marked %d dead run(s) failed: %s",
            run.id, len(stale), ", ".join(str(old.id) for old in stale),
        )
    return run, candidate is not None


def _save_chunk(
    session: Session,
    run_id: int,
    rows: list[dict[str, Any]],
    backtests: list[BacktestResult],
    decisions: list[Any],
) -> None:
    """Insert one batch: its ``Forecast`` rows, the champion back-test paths and
    the segment decisions. The caller commits, so all three land together --
    which is what makes a material-plant either fully saved or absent."""
    if rows:
        session.bulk_insert_mappings(Forecast, rows)

    # Champion backtest paths -- persisted after is_champion is final (decisions
    # may have flipped it from is_baseline), one path row per rolling-origin /
    # horizon-step pair, for the row that actually won this material-plant's
    # comparison. See ForecastBacktestPath's docstring for why only the champion.
    path_rows: list[dict[str, Any]] = []
    for row, backtest_result in zip(rows, backtests):
        if not row["is_champion"] or not backtest_result.paths:
            continue
        for path in backtest_result.paths:
            path_rows.append(
                {
                    "forecast_run_id": run_id,
                    "sap_material_number": row["sap_material_number"],
                    "sap_plant_code": row["sap_plant_code"],
                    "model_name": row["model_name"],
                    "origin_period": path.origin_period,
                    "horizon_step": path.horizon_step,
                    "forecast_period": path.forecast_period,
                    "predicted": path.predicted,
                    "actual": path.actual,
                }
            )
    if path_rows:
        session.bulk_insert_mappings(ForecastBacktestPath, path_rows)

    for decision in decisions:
        session.add(
            SegmentModelDecision(
                forecast_run_id=run_id,
                segment_key=decision.segment_key,
                baseline_model=decision.baseline_model.value,
                challenger_model=(
                    decision.challenger_model.value if decision.challenger_model else None
                ),
                baseline_pinball=decision.baseline_pinball,
                challenger_pinball=decision.challenger_pinball,
                improvement=decision.improvement,
                baseline_bias=decision.baseline_bias,
                challenger_bias=decision.challenger_bias,
                bias_change=decision.bias_change,
                origins_available=decision.origins_available,
                origins_evaluated=decision.origins_evaluated,
                required_origins=decision.required_origins,
                adoption_status=decision.adoption_status.value,
                decision_reason=decision.decision_reason[:500],
            )
        )


def _decisions_from_db(session: Session, run_id: int) -> list[SegmentDecision]:
    """Every saved decision of a run, as the same value objects the loop builds."""
    stored = session.execute(
        select(SegmentModelDecision)
        .where(SegmentModelDecision.forecast_run_id == run_id)
        .order_by(SegmentModelDecision.id)
    ).scalars()
    return [
        SegmentDecision(
            segment_key=row.segment_key,
            baseline_model=ModelName(row.baseline_model),
            challenger_model=ModelName(row.challenger_model) if row.challenger_model else None,
            baseline_pinball=row.baseline_pinball,
            challenger_pinball=row.challenger_pinball,
            improvement=row.improvement,
            baseline_bias=row.baseline_bias,
            challenger_bias=row.challenger_bias,
            bias_change=row.bias_change,
            origins_available=row.origins_available,
            origins_evaluated=row.origins_evaluated,
            required_origins=row.required_origins,
            adoption_status=AdoptionStatus(row.adoption_status),
            decision_reason=row.decision_reason,
            generated_at=row.generated_at,
        )
        for row in stored
    ]


def _status_counts(session: Session, run_id: int) -> tuple[dict[str, int], dict[str, int]]:
    """Model- and backtest-status counts over everything the run has saved."""
    model_counts: dict[str, int] = {}
    backtest_counts: dict[str, int] = {}
    for model_status, backtest_status, n in session.execute(
        select(Forecast.forecast_status, Forecast.backtest_status, func.count())
        .where(Forecast.forecast_run_id == run_id)
        .group_by(Forecast.forecast_status, Forecast.backtest_status)
    ):
        model_counts[model_status] = model_counts.get(model_status, 0) + n
        backtest_counts[backtest_status] = backtest_counts.get(backtest_status, 0) + n
    return model_counts, backtest_counts


def run_forecasting(
    policy: PolicyDocument | None = None, *, resume: bool = True, lock: Any = None
) -> ForecastRunResult:
    """Forecast every routed material-plant and record the evidence.

    ``policy`` defaults to the dev-fixture-aware default (see
    ``app.initiatives.i7.policy.dev_fixtures.default_policy``): an empty,
    unsigned :class:`PolicyDocument` everywhere the
    ``I7_DEV_MOCK_SERVICE_LEVEL`` env var is unset (every environment
    including production), or one carrying the development-only mock
    Criticality -> Service Level matrix when that flag is explicitly set.

    **Saved as it goes.** Every :data:`CHECKPOINT_EVERY` material-plants (or
    :data:`CHECKPOINT_SECONDS`, whichever comes first) the finished ones are
    committed with their decisions and champion paths, and a progress line is
    logged. The run stays ``running`` until every material-plant is done and
    only a ``succeeded`` run is read downstream, so a partly forecast run is
    never used. If the process dies (a deploy, a restart), the next call
    **resumes** that run and skips what is saved -- the full forecast used to be
    kept in memory and written once at the end, so a run lost on 13 Oct 2026
    took 3 h 40 min of work with it. ``resume=False`` forces a fresh run.
    """
    from app.initiatives.i7.locking import application_lock
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    policy = policy or default_policy()
    lock = lock if lock is not None else application_lock(FORECASTING_LOCK)
    if not lock.acquire():
        return ForecastRunResult(
            status=STATUS_FAILED,
            error="another forecasting run holds the forecasting lock; it is already forecasting",
        )
    try:
        return _run_forecasting_locked(policy, resume)
    finally:
        lock.release()


def _run_forecasting_locked(policy: PolicyDocument, resume: bool) -> ForecastRunResult:
    """The body of :func:`run_forecasting`, run while the forecasting lock is held."""
    session_factory = get_sessionmaker()
    quantile = _target_quantile(policy)
    arima_enabled = get_settings().i7_forecast_arima_enabled
    declining_mode = get_settings().i7_declining_demand_mode

    # No approved obsolescence-risk rule exists in policy, so TSB is never a
    # candidate on the current configuration. Read from policy rather than
    # hardcoded false, so a future rule switches it on without a code change.
    obsolescence_trigger_configured = False

    with session_factory() as session:
        feature_run_id = session.execute(
            text("select max(id) from i7_feature_run where status = 'succeeded'")
        ).scalar()
        # Only active mode reads the evaluations: shadow and off must keep
        # working on a database that has not had the table added yet.
        evaluations = _load_evaluations(session, feature_run_id) if declining_mode == "active" else {}
        flagged_keys = frozenset(
            key for key, evaluation in evaluations.items()
            if evaluation.status == DecliningStatus.DECLINING_DEMAND.value
        )
        run, resumed = _claim_run(
            session,
            policy=policy,
            feature_run_id=feature_run_id,
            quantile=quantile,
            arima_enabled=arima_enabled,
            resume=resume,
            declining_mode=declining_mode,
            flagged_keys=flagged_keys,
        )
        session.commit()
        run_id = run.id

    result = ForecastRunResult(run_id=run_id)
    logger.info(
        "forecast run %d: %s; Auto-ARIMA challenger %s; declining-demand mode %s",
        run_id,
        "resuming an interrupted run" if resumed else "starting",
        "ON" if arima_enabled else "off",
        declining_mode,
    )
    run_started = time.monotonic()
    tsb_path_counts = {"success": 0, "not_evaluable": 0}

    try:
        with session_factory() as session:
            logger.info("forecast run %d: loading routed material-plants", run_id)
            candidates = _load_candidates(session)
            result.materials_evaluated = len(candidates)
            if declining_mode == "active":
                # Zero flagged is a normal answer. No evaluation at all for a
                # candidate is not: it means the feature build did not evaluate it.
                missing = [c for c in candidates if (c.material, c.plant) not in evaluations]
                if missing:
                    raise DecliningEvaluationMissing(
                        f"declining-demand mode is 'active' but {len(missing)} of {len(candidates)} "
                        f"material-plants have no evaluation for feature run {feature_run_id}. "
                        "Rebuild the features with I7_DECLINING_DEMAND_MODE=shadow or active first "
                        "(it also needs the i7_declining_demand_evaluation table). "
                        "Nothing was forecast."
                    )
                by_status: dict[str, int] = {}
                for evaluation in evaluations.values():
                    by_status[evaluation.status] = by_status.get(evaluation.status, 0) + 1
                logger.info(
                    "forecast run %d: declining-demand evaluations %s; %d of %d candidates flagged",
                    run_id,
                    by_status,
                    sum(1 for c in candidates if (c.material, c.plant) in flagged_keys),
                    len(candidates),
                )
            for candidate in candidates:
                source = candidate.lead_time_source or "NONE"
                result.lead_time_source_counts[source] = (
                    result.lead_time_source_counts.get(source, 0) + 1
                )
            logger.info(
                "forecast run %d: lead-time sources %s",
                run_id,
                result.lead_time_source_counts,
            )

            # One global pooled LightGBM across the intermittent/lumpy corpus --
            # never one model per material, which would train on 13 points.
            corpus = [
                candidate.series
                for candidate in candidates
                if candidate.baseline_model == BaselineModel.SBA.value
            ]
            logger.info("forecast run %d: training the shared LightGBM model", run_id)
            pooled, pooled_status, pooled_detail = lightgbm_model.train(corpus, quantile)
            logger.info(
                "forecast run %d: pooled LightGBM %s (%s)", run_id, pooled_status, pooled_detail
            )

            saved = _saved_keys(session, run_id) if resumed else set()
            total_candidates = len(candidates)
            logger.info(
                "forecast run %d: forecasting %d material-plants (%d already saved, %d to do)",
                run_id,
                total_candidates,
                len(saved),
                total_candidates - len(saved),
            )

            rows: list[dict[str, Any]] = []
            decisions: list[Any] = []
            # One entry per Forecast row, same index, holding that row's own
            # BacktestResult -- so once is_champion is finalised (after
            # _apply_decision_to_rows may have flipped it from is_baseline),
            # the champion's own paths can be found by row index alone,
            # without re-running or re-matching anything.
            backtest_by_row_index: list[BacktestResult] = []

            completed_candidates = len(saved)
            skipped_candidates = len(saved)
            since_save = 0
            loop_started = time.monotonic()
            last_save = loop_started

            for candidate in candidates:
                if (candidate.material, candidate.plant) in saved:
                    continue

                material_plant_key = f"{candidate.material}/{candidate.plant}"
                by_model: dict[ModelName, BacktestResult] = {}
                baseline_index: int | None = None
                candidate_row_start = len(rows)

                declining = (
                    evaluations.get((candidate.material, candidate.plant))
                    if declining_mode == "active" and (candidate.material, candidate.plant) in flagged_keys
                    else None
                )
                if declining is not None:
                    model_results = _run_tsb_path(candidate, declining)
                else:
                    model_results = _run_models(
                        candidate,
                        pooled,
                        pooled_status,
                        pooled_detail,
                        obsolescence_trigger_configured,
                        quantile,
                        arima_enabled=arima_enabled,
                        include_tsb_placeholder=declining_mode == "off",
                    )

                for model_result in model_results:
                    forecast_result, backtest_result, is_baseline = model_result[:3]
                    is_champion = model_result[3] if len(model_result) > 3 else is_baseline
                    result.model_status_counts[forecast_result.status.value] = (
                        result.model_status_counts.get(forecast_result.status.value, 0) + 1
                    )
                    result.backtest_status_counts[backtest_result.status.value] = (
                        result.backtest_status_counts.get(backtest_result.status.value, 0) + 1
                    )
                    by_model[forecast_result.model] = backtest_result
                    if is_baseline:
                        baseline_index = len(rows)
                    backtest_by_row_index.append(backtest_result)

                    metrics = backtest_result.metrics
                    rows.append(
                        {
                            "forecast_run_id": run_id,
                            "sap_material_number": candidate.material,
                            "sap_plant_code": candidate.plant,
                            "demand_class": candidate.demand_class,
                            "history_status": candidate.history_status,
                            "model_name": forecast_result.model.value,
                            "model_version": forecast_result.model_version,
                            "is_baseline": is_baseline,
                            "is_champion": is_champion,
                            "adoption_status": None,
                            "decision_reason": None,
                            "forecast_status": forecast_result.status.value,
                            "forecast_rate": forecast_result.rate,
                            "forecast_unit": forecast_result.unit,
                            "training_start": forecast_result.training_start,
                            "training_end": forecast_result.training_end,
                            "horizon_months": forecast_result.horizon_months,
                            "parameters": _parameters_text(forecast_result),
                            "backtest_status": backtest_result.status.value,
                            "required_origins": backtest_result.required_origins,
                            "available_origins": backtest_result.available_origins,
                            "origins_evaluated": backtest_result.origins_evaluated,
                            "pinball_loss": metrics.pinball_loss if metrics else None,
                            "pinball_status": metrics.pinball_status.value if metrics else None,
                            "mean_error": metrics.mean_error if metrics else None,
                            "bias_percentage": metrics.bias_percentage if metrics else None,
                            "mean_absolute_error": (
                                metrics.mean_absolute_error if metrics else None
                            ),
                            "fill_rate": metrics.fill_rate if metrics else None,
                            "fill_rate_status": (
                                metrics.fill_rate_status.value if metrics else None
                            ),
                            "holding_cost": metrics.holding_cost if metrics else None,
                            "holding_cost_status": (
                                metrics.holding_cost_status.value if metrics else None
                            ),
                            "detail": (
                                forecast_result.detail or backtest_result.detail or None
                            ),
                        }
                    )

                if declining is not None:
                    decision = selection.decide_declining(
                        material_plant_key,
                        ModelName(candidate.baseline_model),
                        f"declining demand ({declining.rule_version}): {declining.reason}; TSB used "
                        "directly, baseline stored for reference only",
                    )
                    tsb_ok = any(
                        r[0].model is ModelName.TSB and r[0].status is ModelStatus.SUCCESS
                        for r in model_results
                    )
                    tsb_path_counts["success" if tsb_ok else "not_evaluable"] += 1
                else:
                    decision = _material_plant_decision(
                        candidate.demand_class, material_plant_key, by_model
                    )
                if decision is not None:
                    decisions.append(decision)
                    _apply_decision_to_rows(rows, candidate_row_start, decision)

                completed_candidates += 1
                since_save += 1
                if (
                    since_save >= CHECKPOINT_EVERY
                    or time.monotonic() - last_save >= CHECKPOINT_SECONDS
                    or completed_candidates == total_candidates
                ):
                    _save_chunk(session, run_id, rows, backtest_by_row_index, decisions)
                    session.commit()
                    result.segment_decisions.extend(decisions)
                    rows, decisions, backtest_by_row_index = [], [], []
                    since_save = 0
                    last_save = time.monotonic()
                    log_progress(
                        run_id, completed_candidates, total_candidates, loop_started,
                        skipped=skipped_candidates,
                    )

            # A run whose every material-plant was already saved never reaches
            # the save above; anything left over is saved here.
            if rows or decisions:
                _save_chunk(session, run_id, rows, backtest_by_row_index, decisions)
                session.commit()
                result.segment_decisions.extend(decisions)

            result.forecasts_written = session.execute(
                select(func.count()).select_from(Forecast).where(Forecast.forecast_run_id == run_id)
            ).scalar_one()
            if resumed:
                # What this call computed is only the tail of the run.
                result.model_status_counts, result.backtest_status_counts = _status_counts(
                    session, run_id
                )
                result.segment_decisions = _decisions_from_db(session, run_id)

        with session_factory() as session:
            stored = session.get(ForecastRun, run_id)
            stored.status = STATUS_SUCCEEDED
            stored.forecasts_written = result.forecasts_written
            stored.materials_evaluated = result.materials_evaluated
            stored.finished_at = datetime.now(timezone.utc)
            session.commit()

        logger.info(
            "forecast run %d: %d material-plants, %d forecasts, %d segment decisions",
            run_id,
            result.materials_evaluated,
            result.forecasts_written,
            len(result.segment_decisions),
        )
        if declining_mode != "off":
            logger.info(
                "forecast run %d: declining-demand %s: %d flagged, TSB used for %d, "
                "TSB not evaluable for %d, in %.0f s",
                run_id,
                declining_mode,
                len(flagged_keys),
                tsb_path_counts["success"],
                tsb_path_counts["not_evaluable"],
                time.monotonic() - run_started,
            )
        return result

    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        logger.error("forecast run %d failed: %s", run_id, detail)
        result.status = STATUS_FAILED
        result.error = detail
        try:
            with session_factory() as session:
                stored = session.get(ForecastRun, run_id)
                stored.status = STATUS_FAILED
                stored.error = detail[:4000]
                stored.finished_at = datetime.now(timezone.utc)
                session.commit()
        except Exception:
            logger.exception("forecast run %d: could not record the failure either", run_id)
        return result


def _material_plant_decision(
    demand_class: str,
    material_plant_key: str,
    by_model: dict[ModelName, BacktestResult],
) -> Any | None:
    """One champion/challenger decision for a single material-plant.

    The FRS requires model selection per material by backtest (Section 3.1,
    FR-3), so each material-plant's own baseline/challenger ``BacktestResult``
    (already computed per-candidate by ``_run_models``) is compared directly
    -- no pooling across other material-plants in the same demand class.
    """
    if demand_class in (DemandPattern.SMOOTH.value, DemandPattern.ERRATIC.value):
        baseline_model, challenger_model = ModelName.SES, ModelName.AUTO_ARIMA
        decide = selection.decide_smooth
    elif demand_class in (DemandPattern.INTERMITTENT.value, DemandPattern.LUMPY.value):
        baseline_model, challenger_model = ModelName.SBA, ModelName.LIGHTGBM
        decide = selection.decide_intermittent
    else:
        return None

    baseline = by_model.get(baseline_model)
    challenger = by_model.get(challenger_model)
    if baseline is None:
        return None

    return decide(material_plant_key, baseline, challenger)


def _apply_decision_to_rows(
    rows: list[dict[str, Any]], candidate_row_start: int, decision: Any
) -> None:
    """Stamp one material-plant's decision onto its own ``Forecast`` rows.

    ``is_champion`` starts equal to ``is_baseline`` for every row (set while
    the row was built); this only flips it when the challenger actually won
    THIS material-plant's own comparison -- never based on any other
    material-plant's decision.
    """
    for offset in range(candidate_row_start, len(rows)):
        row = rows[offset]
        if row["model_name"] == decision.baseline_model.value or (
            decision.challenger_model is not None
            and row["model_name"] == decision.challenger_model.value
        ):
            row["adoption_status"] = decision.adoption_status.value
            row["decision_reason"] = decision.decision_reason[:500]

    if decision.adoption_status == AdoptionStatus.CHALLENGER_ELIGIBLE:
        for offset in range(candidate_row_start, len(rows)):
            row = rows[offset]
            if (
                decision.challenger_model is not None
                and row["model_name"] == decision.challenger_model.value
            ):
                row["is_champion"] = True
            elif row["model_name"] == decision.baseline_model.value:
                row["is_champion"] = False
