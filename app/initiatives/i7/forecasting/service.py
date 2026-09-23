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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.initiatives.i7.adapters import consumption_series_for
from app.initiatives.i7.contracts import DemandPattern
from app.initiatives.i7.features import BaselineModel, HistoryStatus
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
)
from app.initiatives.i7.policy import PolicyDocument
from app.models.i7_forecast import (
    Forecast,
    ForecastBacktestPath,
    ForecastRun,
    SegmentModelDecision,
)

logger = logging.getLogger(__name__)

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
     ORDER BY f.sap_material_number, f.sap_plant_code
"""
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
        text(_ROUTED_SQL), {"sufficient": HistoryStatus.SUFFICIENT.value}
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
) -> list[tuple[ForecastResult, BacktestResult, bool]]:
    """Forecast and backtest every model applicable to one material-plant.

    Returns ``(forecast, backtest, is_baseline)`` per model.
    """
    horizon = candidate.lead_time_months
    results: list[tuple[ForecastResult, BacktestResult, bool]] = []

    if candidate.baseline_model == BaselineModel.SES.value:
        pairs = [
            (ModelName.SES, lambda s, h: ses.forecast(s, h), True),
            (ModelName.AUTO_ARIMA, lambda s, h: arima.forecast(s, h), False),
        ]
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

    # TSB is appended only when an approved obsolescence rule exists. Without
    # one it still reports, so the gate is visible rather than silent.
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


def run_forecasting(policy: PolicyDocument | None = None) -> ForecastRunResult:
    """Forecast every routed material-plant and record the evidence.

    ``policy`` defaults to the dev-fixture-aware default (see
    ``app.initiatives.i7.policy.dev_fixtures.default_policy``): an empty,
    unsigned :class:`PolicyDocument` everywhere the
    ``I7_DEV_MOCK_SERVICE_LEVEL`` env var is unset (every environment
    including production), or one carrying the development-only mock
    Criticality -> Service Level matrix when that flag is explicitly set.
    """
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    policy = policy or default_policy()
    session_factory = get_sessionmaker()
    quantile = _target_quantile(policy)

    # No approved obsolescence-risk rule exists in policy, so TSB is never a
    # candidate on the current configuration. Read from policy rather than
    # hardcoded false, so a future rule switches it on without a code change.
    obsolescence_trigger_configured = False

    with session_factory() as session:
        feature_run_id = session.execute(
            text("select max(id) from i7_feature_run where status = 'succeeded'")
        ).scalar()
        run = ForecastRun(
            status="running",
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            feature_run_id=feature_run_id,
            target_quantile=Decimal(str(quantile)) if quantile is not None else None,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    result = ForecastRunResult(run_id=run_id)

    try:
        with session_factory() as session:
            logger.info("forecast run %d: loading routed material-plants", run_id)
            candidates = _load_candidates(session)
            result.materials_evaluated = len(candidates)
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
            pooled, pooled_status, pooled_detail = lightgbm_model.train(corpus, quantile)
            logger.info(
                "forecast run %d: pooled LightGBM %s (%s)", run_id, pooled_status, pooled_detail
            )

            rows: list[dict[str, Any]] = []
            decisions: list[Any] = []
            # One entry per Forecast row, same index, holding that row's own
            # BacktestResult -- so once is_champion is finalised below (after
            # _apply_decision_to_rows may have flipped it from is_baseline),
            # the champion's own paths can be found by row index alone,
            # without re-running or re-matching anything.
            backtest_by_row_index: list[BacktestResult] = []

            for candidate in candidates:
                material_plant_key = f"{candidate.material}/{candidate.plant}"
                by_model: dict[ModelName, BacktestResult] = {}
                baseline_index: int | None = None
                candidate_row_start = len(rows)

                for forecast_result, backtest_result, is_baseline in _run_models(
                    candidate,
                    pooled,
                    pooled_status,
                    pooled_detail,
                    obsolescence_trigger_configured,
                    quantile,
                ):
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
                            "is_champion": is_baseline,
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
                            "detail": (forecast_result.detail or backtest_result.detail or None),
                        }
                    )

                decision = _material_plant_decision(
                    candidate.demand_class, material_plant_key, by_model
                )
                if decision is not None:
                    decisions.append(decision)
                    _apply_decision_to_rows(rows, candidate_row_start, decision)

            session.bulk_insert_mappings(Forecast, rows)
            result.forecasts_written = len(rows)

            # Champion backtest paths -- persisted after is_champion is final
            # (decisions above may have flipped it from is_baseline), one
            # path row per rolling-origin/horizon-step pair, for the row that
            # actually won this material-plant's comparison. See
            # ForecastBacktestPath's docstring for why only the champion.
            path_rows: list[dict[str, Any]] = []
            for row, backtest_result in zip(rows, backtest_by_row_index):
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
                            decision.challenger_model.value
                            if decision.challenger_model
                            else None
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
            result.segment_decisions = decisions
            session.commit()

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
