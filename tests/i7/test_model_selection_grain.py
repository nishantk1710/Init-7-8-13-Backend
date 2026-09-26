"""Per-material-plant model selection (FRS Section 3.1, FR-3).

The FRS requires "select per material by backtest" -- twice, in Section 3.1
and FR-3 -- which supersedes the earlier Solution Design draft that pooled
adoption at demand-class grain. These tests pin the resulting behaviour at
the ``service._material_plant_decision`` boundary: each material-plant's own
baseline/challenger ``BacktestResult`` decides its own model, independent of
what any other material-plant in the same demand class does.

Numbers are deliberately not hardcoded into production code -- ``selection.
decide_intermittent`` computes the verdict; these tests only supply distinct
per-material inputs and check the per-material outputs differ accordingly.
"""

from decimal import Decimal

from app.initiatives.i7.contracts import DemandPattern
from app.initiatives.i7.forecasting import service
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    AdoptionStatus,
    BacktestMetrics,
    BacktestResult,
    BacktestStatus,
    MetricStatus,
    ModelName,
)


def result_with(model: ModelName, pinball, bias, origins: int, required: int = 12) -> BacktestResult:
    return BacktestResult(
        model=model,
        model_version=MODEL_VERSIONS[model],
        status=BacktestStatus.COMPLETE
        if origins >= required
        else BacktestStatus.PARTIAL_DEVELOPMENT_DATA,
        required_origins=required,
        available_origins=origins,
        origins_evaluated=origins,
        metrics=BacktestMetrics(
            pinball_loss=Decimal(str(pinball)) if pinball is not None else None,
            pinball_status=MetricStatus.AVAILABLE
            if pinball is not None
            else MetricStatus.NOT_EVALUABLE,
            mean_error=None,
            bias_percentage=Decimal(str(bias)) if bias is not None else None,
            fill_rate=None,
            fill_rate_status=MetricStatus.NOT_EVALUABLE,
            holding_cost=None,
            holding_cost_status=MetricStatus.NOT_EVALUABLE,
            mean_absolute_error=None,
        ),
    )


# --- Two materials in the same demand class, different backtests, different
# --- winners: the central property the FRS requires and the Solution Design
# --- draft's "decision is per segment" stance forbade. --------------------


def test_two_materials_same_class_select_different_models_by_their_own_backtest():
    """Material A: SBA pinball=10, LightGBM pinball=12 -> SBA wins.
    Material B: SBA pinball=15, LightGBM pinball=10 -> LightGBM wins.
    Same demand class (LUMPY) for both -- only each material's own evidence
    differs, and that alone must decide its own model.
    """
    material_a = {
        ModelName.SBA: result_with(ModelName.SBA, 10, 0.10, 12),
        ModelName.LIGHTGBM: result_with(ModelName.LIGHTGBM, 12, 0.10, 12),
    }
    material_b = {
        ModelName.SBA: result_with(ModelName.SBA, 15, 0.10, 12),
        ModelName.LIGHTGBM: result_with(ModelName.LIGHTGBM, 10, 0.10, 12),
    }

    decision_a = service._material_plant_decision(
        DemandPattern.LUMPY.value, "MAT-A/1300", material_a
    )
    decision_b = service._material_plant_decision(
        DemandPattern.LUMPY.value, "MAT-B/1300", material_b
    )

    assert decision_a.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert decision_a.segment_key == "MAT-A/1300"

    assert decision_b.adoption_status is AdoptionStatus.CHALLENGER_ELIGIBLE
    assert decision_b.segment_key == "MAT-B/1300"


def test_material_plant_isolation_no_leakage_between_candidates():
    """A third material with yet another backtest outcome must not be swayed
    by materials A or B's results -- each call is independent, no shared
    pooling state."""
    material_c = {
        ModelName.SBA: result_with(ModelName.SBA, 5, 0.02, 12),
        ModelName.LIGHTGBM: result_with(ModelName.LIGHTGBM, 4.5, 0.02, 12),
    }
    decision_c = service._material_plant_decision(
        DemandPattern.INTERMITTENT.value, "MAT-C/1300", material_c
    )
    # 10% improvement, clears the >5% bar with bias unchanged.
    assert decision_c.adoption_status is AdoptionStatus.CHALLENGER_ELIGIBLE
    assert decision_c.segment_key == "MAT-C/1300"


def test_insufficient_history_no_baseline_result_yields_no_decision():
    """A material-plant whose baseline could not even run backtests has
    nothing to compare -- no decision is fabricated."""
    decision = service._material_plant_decision(
        DemandPattern.LUMPY.value, "MAT-D/1300", {ModelName.LIGHTGBM: result_with(
            ModelName.LIGHTGBM, 5, 0.10, 12
        )}
    )
    assert decision is None


def test_no_valid_candidate_unroutable_demand_class_yields_no_decision():
    """A demand class outside the routed set (e.g. OBSOLETE) has no baseline/
    challenger pair defined at all."""
    decision = service._material_plant_decision("OBSOLETE", "MAT-E/1300", {})
    assert decision is None


def test_one_valid_candidate_baseline_only_is_not_evaluable():
    """Baseline ran, challenger did not (e.g. LightGBM blocked on service
    level) -- reported NOT_EVALUABLE, not silently retained as if compared."""
    decision = service._material_plant_decision(
        DemandPattern.LUMPY.value,
        "MAT-F/1300",
        {ModelName.SBA: result_with(ModelName.SBA, 10, 0.10, 12)},
    )
    assert decision is not None
    assert decision.adoption_status is AdoptionStatus.NOT_EVALUABLE


def test_bias_threshold_rejection_is_evaluated_per_material():
    """20% pinball improvement but bias deteriorates from 10% to 20% -- over
    the >5% deterioration bar, so rejected for THIS material regardless of
    what another material with the same demand class saw."""
    material = {
        ModelName.SBA: result_with(ModelName.SBA, 1.0, 0.10, 12),
        ModelName.LIGHTGBM: result_with(ModelName.LIGHTGBM, 0.80, 0.20, 12),
    }
    decision = service._material_plant_decision(
        DemandPattern.LUMPY.value, "MAT-G/1300", material
    )
    assert decision.adoption_status is AdoptionStatus.BASELINE_RETAINED
    assert "bias" in decision.decision_reason


def test_minimum_origin_requirement_is_evaluated_per_material():
    """Better metrics but only 8 of 12 required origins -- ineligible for
    THIS material even though its metrics alone would otherwise qualify."""
    material = {
        ModelName.SBA: result_with(ModelName.SBA, 1.0, 0.10, 8),
        ModelName.LIGHTGBM: result_with(ModelName.LIGHTGBM, 0.50, 0.10, 8),
    }
    decision = service._material_plant_decision(
        DemandPattern.LUMPY.value, "MAT-H/1300", material
    )
    assert decision.adoption_status is AdoptionStatus.NOT_ELIGIBLE_INSUFFICIENT_ORIGINS


def test_smooth_erratic_classes_route_to_ses_auto_arima_per_material():
    material = {
        ModelName.SES: result_with(ModelName.SES, 1.0, 0.10, 12),
        ModelName.AUTO_ARIMA: result_with(ModelName.AUTO_ARIMA, 0.90, 0.10, 12),
    }
    decision = service._material_plant_decision(
        DemandPattern.SMOOTH.value, "MAT-I/1300", material
    )
    assert decision is not None
    assert decision.baseline_model is ModelName.SES
    assert decision.challenger_model is ModelName.AUTO_ARIMA
    assert decision.segment_key == "MAT-I/1300"


# --- The selected model must actually be what downstream forecasting uses ---
# --- (is_champion), never the fixed is_baseline flag. ----------------------


def _rows_for(baseline_model: str, challenger_model: str) -> list[dict]:
    return [
        {"model_name": baseline_model, "is_baseline": True, "is_champion": True,
         "adoption_status": None, "decision_reason": None},
        {"model_name": challenger_model, "is_baseline": False, "is_champion": False,
         "adoption_status": None, "decision_reason": None},
        {"model_name": "TSB", "is_baseline": False, "is_champion": False,
         "adoption_status": None, "decision_reason": None},
    ]


def test_champion_flag_moves_to_the_challenger_when_it_wins():
    """Material B's own decision (CHALLENGER_ELIGIBLE) must flip is_champion
    onto LightGBM's row and off SBA's row -- this is what inventory/
    recommendations actually read, not is_baseline."""
    material_b = {
        ModelName.SBA: result_with(ModelName.SBA, 15, 0.10, 12),
        ModelName.LIGHTGBM: result_with(ModelName.LIGHTGBM, 10, 0.10, 12),
    }
    decision = service._material_plant_decision(
        DemandPattern.LUMPY.value, "MAT-B/1300", material_b
    )
    rows = _rows_for("SBA", "LIGHTGBM")
    service._apply_decision_to_rows(rows, 0, decision)

    sba_row = next(r for r in rows if r["model_name"] == "SBA")
    lgbm_row = next(r for r in rows if r["model_name"] == "LIGHTGBM")
    tsb_row = next(r for r in rows if r["model_name"] == "TSB")

    assert sba_row["is_champion"] is False
    assert lgbm_row["is_champion"] is True
    assert tsb_row["is_champion"] is False  # untouched -- not part of this decision
    assert lgbm_row["adoption_status"] == AdoptionStatus.CHALLENGER_ELIGIBLE.value
    assert sba_row["adoption_status"] == AdoptionStatus.CHALLENGER_ELIGIBLE.value


def test_champion_flag_stays_on_baseline_when_challenger_does_not_win():
    """Material A's own decision (BASELINE_RETAINED) leaves is_champion on
    SBA's row -- the default from row construction is preserved, not flipped
    just because a decision exists."""
    material_a = {
        ModelName.SBA: result_with(ModelName.SBA, 10, 0.10, 12),
        ModelName.LIGHTGBM: result_with(ModelName.LIGHTGBM, 12, 0.10, 12),
    }
    decision = service._material_plant_decision(
        DemandPattern.LUMPY.value, "MAT-A/1300", material_a
    )
    rows = _rows_for("SBA", "LIGHTGBM")
    service._apply_decision_to_rows(rows, 0, decision)

    sba_row = next(r for r in rows if r["model_name"] == "SBA")
    lgbm_row = next(r for r in rows if r["model_name"] == "LIGHTGBM")

    assert sba_row["is_champion"] is True
    assert lgbm_row["is_champion"] is False
    assert sba_row["adoption_status"] == AdoptionStatus.BASELINE_RETAINED.value
