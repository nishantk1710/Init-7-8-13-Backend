"""Forecasting routes flagged material-plants to TSB directly -- and only then.

Over an in-memory database with fake models (as ``test_forecasting_checkpoint``):
what is under test is which material-plants take which path, what is saved, and
what refuses to run -- not the forecasting maths.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from sqlalchemy import create_engine

from app.initiatives.i7.features.builder import (
    FeatureBuildResult,
    _declining_row,
    _write_declining_evaluations,
)
from app.initiatives.i7.features.declining import DecliningEvaluation, DecliningStatus
from app.initiatives.i7.forecasting import service
from app.initiatives.i7.forecasting.types import (
    AdoptionStatus,
    BacktestStatus,
    ForecastResult,
    ModelName,
    ModelStatus,
)
from app.initiatives.i7.policy import PolicyDocument
from app.models.base import Base
from app.models.i7_features import DecliningDemandEvaluation
from app.models.i7_forecast import Forecast, SegmentModelDecision
from tests.i7.test_forecasting_checkpoint import (
    Killed,
    Models,
    _forecast,
    candidates,
    make_env,
    rows,
    run,
    runs,
)

FLAGGED = {"0000", "0001", "0002"}  # 0000 is SES (smooth); 0001 and 0002 are SBA (intermittent)


def seed(factory, flagged=FLAGGED, *, only=None):
    """Evaluations for feature run 1: ``flagged`` are DECLINING_DEMAND, the rest are not."""
    with factory() as session:
        for candidate in candidates():
            if only is not None and candidate.material not in only:
                continue
            is_flagged = candidate.material in flagged
            session.add(
                DecliningDemandEvaluation(
                    feature_run_id=1,
                    sap_material_number=candidate.material,
                    sap_plant_code=candidate.plant,
                    status="DECLINING_DEMAND" if is_flagged else "RECENT_DEMAND_PRESENT",
                    reason="months with demand fell by 67%",
                    rule_version="declining-v1",
                    window_start=date(2025, 1, 1),
                    window_end=date(2025, 12, 1),
                    months_evaluated=12,
                )
            )
        session.commit()


def fake_models(monkeypatch, *, tsb_status=ModelStatus.SUCCESS):
    """The three forecasters the TSB path calls, replaced by fixed results."""
    monkeypatch.setattr(service.ses, "forecast", lambda s, h: _forecast(ModelName.SES))
    monkeypatch.setattr(service.sba, "forecast", lambda s, h: _forecast(ModelName.SBA))

    def tsb_forecast(series, horizon, obsolescence_trigger_configured):
        assert obsolescence_trigger_configured is True  # the rule is what authorises TSB here
        if tsb_status is ModelStatus.SUCCESS:
            return _forecast(ModelName.TSB)._replace(parameters=(("alpha", "0.10"), ("p_final", "0.2")))
        return ForecastResult(
            model=ModelName.TSB, model_version="tsb-1", status=tsb_status, detail="not enough data"
        )

    monkeypatch.setattr(service.tsb, "forecast", tsb_forecast)


def material_rows(factory, material, run_id=1):
    with factory() as session:
        return session.execute(
            select(Forecast).where(
                Forecast.forecast_run_id == run_id, Forecast.sap_material_number == material
            ).order_by(Forecast.model_name)
        ).scalars().all()


# --- off: nothing changes --------------------------------------------------------


def test_off_keeps_the_placeholder_and_never_reads_the_evaluations(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="off", evaluation_table=False)  # no table at all
    models = Models()
    result = run(models, monkeypatch)
    assert result.status == "succeeded"
    assert set(models.placeholder_flags) == {True}  # TSB placeholder rows exactly as before
    assert len(models.computed) == 12


def test_the_real_models_add_a_tsb_placeholder_only_when_asked():
    candidate = candidates()[0]  # a smooth material, so SES alone with ARIMA off
    kept = service._run_models(
        candidate, None, ModelStatus.SUCCESS, "", False, None, arima_enabled=False
    )
    dropped = service._run_models(
        candidate, None, ModelStatus.SUCCESS, "", False, None,
        arima_enabled=False, include_tsb_placeholder=False,
    )
    assert [r[0].model for r in kept] == [ModelName.SES, ModelName.TSB]
    assert kept[1][0].status is ModelStatus.NOT_EVALUABLE_TRIGGER_UNSET
    assert [r[0].model for r in dropped] == [ModelName.SES]


# --- shadow: evaluated elsewhere, forecasts unchanged --------------------------------


def test_shadow_changes_no_forecast_and_writes_no_tsb_rows(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="shadow", evaluation_table=False)
    models = Models()
    result = run(models, monkeypatch)
    assert result.status == "succeeded"
    assert len(models.computed) == 12  # flagged ones included: routing is unchanged
    assert set(models.placeholder_flags) == {False}  # no placeholder rows for anyone
    assert not [r for r in rows(factory) if r[1] == "TSB"]


# --- active: flagged material-plants take the TSB path ----------------------------------


@pytest.fixture
def active(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="active")
    fake_models(monkeypatch)
    seed(factory)
    return factory


def test_flagged_material_plants_get_tsb_directly_and_skip_the_normal_models(active, monkeypatch):
    models = Models()
    result = run(models, monkeypatch)
    assert result.status == "succeeded" and result.error is None
    assert FLAGGED.isdisjoint(models.computed)  # the normal models never ran for them
    assert len(models.computed) == 12 - len(FLAGGED)
    for material in FLAGGED:
        stored = material_rows(active, material)
        assert {r.model_name for r in stored} == {"TSB", "SES" if material == "0000" else "SBA"}
        tsb = next(r for r in stored if r.model_name == "TSB")
        reference = next(r for r in stored if r.model_name != "TSB")
        assert (tsb.is_champion, reference.is_champion) == (True, False)
        assert (tsb.is_baseline, reference.is_baseline) == (False, True)
        assert tsb.forecast_status == "SUCCESS" and reference.forecast_status == "SUCCESS"
        assert tsb.adoption_status == reference.adoption_status == AdoptionStatus.TSB_DECLINING_DEMAND.value


def test_there_is_no_backtest_and_no_comparison_for_the_flagged_path(active, monkeypatch):
    run(Models(), monkeypatch)
    for row in material_rows(active, "0001"):
        assert row.backtest_status == BacktestStatus.NOT_RUN_DECLINING_DEMAND.value
        assert row.origins_evaluated == 0 and row.pinball_loss is None
    with active() as session:
        decision = session.execute(
            select(SegmentModelDecision).where(SegmentModelDecision.segment_key == "0001/1300")
        ).scalar_one()
    assert decision.adoption_status == "TSB_DECLINING_DEMAND"
    assert (decision.baseline_model, decision.challenger_model) == ("SBA", "TSB")
    assert decision.baseline_pinball is None and decision.improvement is None
    assert "declining demand (declining-v1)" in decision.decision_reason
    assert "baseline stored for reference only" in decision.decision_reason


def test_the_tsb_row_records_the_rule_and_the_months_it_judged(active, monkeypatch):
    run(Models(), monkeypatch)
    tsb = next(r for r in material_rows(active, "0001") if r.model_name == "TSB")
    assert tsb.parameters.startswith("declining_rule=declining-v1;evaluated_months=2025-01..2025-12;")
    assert "alpha=0.10" in tsb.parameters


def test_unflagged_material_plants_route_exactly_as_before_with_no_tsb_row(active, monkeypatch):
    run(Models(), monkeypatch)
    for material in ("0003", "0004", "0011"):
        stored = material_rows(active, material)
        assert "TSB" not in {r.model_name for r in stored}
        assert sum(r.is_champion for r in stored) == 1  # the baseline, as before


def test_only_flagged_material_plants_receive_tsb_rows(active, monkeypatch):
    run(Models(), monkeypatch)
    tsb_materials = {m for m, model, _, _ in rows(active) if model == "TSB"}
    assert tsb_materials == FLAGGED


def test_each_flagged_material_plant_has_exactly_one_champion(active, monkeypatch):
    run(Models(), monkeypatch)
    for material in FLAGGED:
        assert sum(r.is_champion for r in material_rows(active, material)) == 1


def test_a_second_run_writes_its_own_rows_without_duplicating_the_first(active, monkeypatch):
    run(Models(), monkeypatch)
    run(Models(), monkeypatch)
    assert [r.status for r in runs(active)] == ["succeeded", "succeeded"]
    for run_id in (1, 2):
        tsb = [r for r in rows(active, run_id) if r[1] == "TSB"]
        assert len(tsb) == len(FLAGGED)
        assert sum(1 for r in tsb if r[2]) == len(FLAGGED)  # one champion TSB row each, per run


def test_zero_flagged_is_a_normal_answer_and_forecasting_runs_the_normal_way(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="active")
    fake_models(monkeypatch)
    seed(factory, flagged=set())
    models = Models()
    result = run(models, monkeypatch)
    assert result.status == "succeeded"
    assert len(models.computed) == 12
    assert not [r for r in rows(factory) if r[1] == "TSB"]


# --- TSB's own requirements still apply ------------------------------------------------


def test_a_flagged_material_plant_whose_tsb_fails_has_no_champion_and_no_fallback(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="active")
    fake_models(monkeypatch, tsb_status=ModelStatus.INSUFFICIENT_HISTORY)
    seed(factory)
    run(Models(), monkeypatch)
    stored = material_rows(factory, "0001")
    tsb = next(r for r in stored if r.model_name == "TSB")
    assert tsb.forecast_status == "INSUFFICIENT_HISTORY" and tsb.forecast_rate is None
    assert not any(r.is_champion for r in stored)  # the baseline is not promoted in its place


def test_a_flagged_material_plant_without_a_lead_time_is_explicitly_not_evaluable(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="active")
    fake_models(monkeypatch)
    seed(factory)
    original = candidates()
    monkeypatch.setattr(
        service,
        "_load_candidates",
        lambda session: [
            c.__class__(**{**c.__dict__, "lead_time_months": None}) if c.material == "0001" else c
            for c in original
        ],
    )
    run(Models(), monkeypatch)
    stored = material_rows(factory, "0001")
    assert {r.forecast_status for r in stored} == {"MISSING_REQUIRED_FEATURE"}
    assert not any(r.is_champion for r in stored)


# --- active refuses to run without its evaluations ----------------------------------------


def test_active_with_no_evaluations_fails_clearly_and_forecasts_nothing(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="active")
    fake_models(monkeypatch)
    models = Models()
    result = run(models, monkeypatch)
    assert result.status == "failed"
    assert "12 of 12 material-plants have no evaluation" in result.error
    assert "I7_DECLINING_DEMAND_MODE" in result.error and "Nothing was forecast" in result.error
    assert models.computed == [] and rows(factory) == []
    assert [r.status for r in runs(factory)] == ["failed"]


def test_active_with_some_candidates_unevaluated_also_fails(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="active")
    fake_models(monkeypatch)
    seed(factory, only={f"{i:04d}" for i in range(10)})  # two of the twelve are missing
    result = run(Models(), monkeypatch)
    assert result.status == "failed" and "2 of 12 material-plants have no evaluation" in result.error
    assert rows(factory) == []


# --- resume must not mix modes ------------------------------------------------------------------


def test_a_killed_active_run_resumes_under_active(monkeypatch):
    factory, _ = make_env(monkeypatch, declining="active", every=2)
    fake_models(monkeypatch)
    seed(factory)
    with pytest.raises(Killed):
        run(Models(die_at=5), monkeypatch)
    result = run(Models(), monkeypatch)
    assert result.status == "succeeded"
    assert len(runs(factory)) == 1  # resumed, not started again
    assert {m for m, model, _, _ in rows(factory) if model == "TSB"} == FLAGGED


def test_a_run_started_with_the_feature_off_is_not_resumed_with_it_on(monkeypatch):
    factory, settings = make_env(monkeypatch, declining="off", every=2)
    fake_models(monkeypatch)
    with pytest.raises(Killed):
        run(Models(die_at=5), monkeypatch)
    # The killed run has TSB placeholders. Switch the feature on: a new run, not a mix.
    Base.metadata.create_all(factory.kw["bind"], tables=[DecliningDemandEvaluation.__table__], checkfirst=True)
    seed(factory)
    settings.i7_declining_demand_mode = "active"
    # Placeholders are written by the real _run_models, which the fake skips; stand in for one.
    with factory() as session:
        session.add(
            Forecast(
                forecast_run_id=1, sap_material_number="0000", sap_plant_code="1300", demand_class="SMOOTH",
                history_status="SUFFICIENT", model_name="TSB", model_version="tsb-1",
                forecast_status="NOT_EVALUABLE_TRIGGER_UNSET", backtest_status="NOT_EVALUABLE",
            )
        )
        session.commit()
    run(Models(), monkeypatch)
    assert len(runs(factory)) == 2  # a new run was started


def test_a_run_that_used_tsb_directly_is_not_resumed_in_shadow_mode(monkeypatch):
    factory, settings = make_env(monkeypatch, declining="active", every=2)
    fake_models(monkeypatch)
    seed(factory)
    with pytest.raises(Killed):
        run(Models(die_at=5), monkeypatch)
    settings.i7_declining_demand_mode = "shadow"
    run(Models(), monkeypatch)
    assert len(runs(factory)) == 2


# --- the feature build's rows -----------------------------------------------------------------


def evaluation(status=DecliningStatus.DECLINING_DEMAND):
    return DecliningEvaluation(
        status, "months with demand fell by 67%", "declining-v1",
        window_start=date(2025, 1, 1), window_end=date(2025, 12, 1), months_evaluated=12,
        nonzero_months=4, early_nonzero=3, recent_nonzero=1,
        early_rate=Decimal("0.5"), recent_rate=Decimal("0.1666666667"), decline_pct=Decimal("0.6666666667"),
        latest_months_zero=True,
    )


def test_an_evaluation_becomes_a_row_with_rates_rounded_to_four_places():
    row = _declining_row(7, "M1", "1300", evaluation())
    assert row["feature_run_id"] == 7 and row["status"] == "DECLINING_DEMAND"
    assert row["early_rate"] == Decimal("0.5000")
    assert row["recent_rate"] == Decimal("0.1667") and row["decline_pct"] == Decimal("0.6667")
    assert row["latest_months_zero"] is True and row["rule_version"] == "declining-v1"


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    DecliningDemandEvaluation.__table__.create(engine)
    with Session(engine) as session:
        yield session


def build_result(run_id, *evaluations):
    result = FeatureBuildResult(run_id=run_id)
    result.declining_mode = "shadow"
    for material, plant, ev in evaluations:
        result.declining_rows.append(_declining_row(run_id, material, plant, ev))
        result.declining_status_counts[ev.status.value] = result.declining_status_counts.get(ev.status.value, 0) + 1
    return result


def test_the_same_material_in_two_plants_is_evaluated_per_plant(db):
    result = build_result(
        3,
        ("M1", "1300", evaluation()),
        ("M1", "1500", evaluation(DecliningStatus.RECENT_DEMAND_PRESENT)),
    )
    _write_declining_evaluations(db, result, (date(2025, 1, 1), date(2026, 1, 1)), PolicyDocument())
    db.commit()
    stored = {(r.sap_plant_code): r.status for r in db.scalars(select(DecliningDemandEvaluation))}
    assert stored == {"1300": "DECLINING_DEMAND", "1500": "RECENT_DEMAND_PRESENT"}


def test_a_later_feature_run_supersedes_an_old_flag_and_never_edits_it(db):
    old = build_result(3, ("M1", "1300", evaluation()))
    _write_declining_evaluations(db, old, (date(2025, 1, 1), date(2026, 1, 1)), PolicyDocument())
    new = build_result(4, ("M1", "1300", evaluation(DecliningStatus.RECENT_DEMAND_PRESENT)))
    _write_declining_evaluations(db, new, (date(2025, 1, 1), date(2026, 3, 1)), PolicyDocument())
    db.commit()
    by_run = {r.feature_run_id: r.status for r in db.scalars(select(DecliningDemandEvaluation))}
    assert by_run == {3: "DECLINING_DEMAND", 4: "RECENT_DEMAND_PRESENT"}  # forecasting reads run 4


def test_rewriting_the_same_run_does_not_duplicate_rows(db):
    result = build_result(5, ("M1", "1300", evaluation()))
    for _ in range(2):
        _write_declining_evaluations(db, result, (date(2025, 1, 1), date(2026, 1, 1)), PolicyDocument())
        db.commit()
    assert len(db.scalars(select(DecliningDemandEvaluation)).all()) == 1


def test_a_short_extract_is_warned_about(db, caplog):
    import logging

    result = build_result(6, ("M1", "1300", evaluation(DecliningStatus.INSUFFICIENT_HISTORY)))
    with caplog.at_level(logging.WARNING, logger="app.initiatives.i7.features.builder"):
        _write_declining_evaluations(db, result, (date(2025, 6, 1), date(2026, 6, 1)), PolicyDocument())  # 13 months: enough
        assert not [r for r in caplog.records if "declining-demand needs" in r.getMessage()]
        _write_declining_evaluations(db, result, (date(2025, 7, 1), date(2026, 6, 1)), PolicyDocument())  # 12: not enough
    assert any("covers 12 months; declining-demand needs 13" in r.getMessage() for r in caplog.records)


def test_the_setting_is_off_by_default_and_rejects_anything_else_than_the_three_modes():
    from app.core.config import Settings

    assert Settings().i7_declining_demand_mode == "off"
    with pytest.raises(ValueError):
        Settings(i7_declining_demand_mode="sometimes")
