"""Forecasting saves as it goes, resumes an interrupted run, and can drop Auto-ARIMA.

The full forecast used to be held in memory and written once at the end, so a
run lost on 13 Oct 2026 took 3 h 40 min of work with it. These tests pin the
fix over an in-memory database. The models are replaced by fakes -- what is
under test is the run bookkeeping (batches, resume, locks, ARIMA switch), not
the maths, which ``test_forecasting_models.py`` covers.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.forecasting import service
from app.initiatives.i7.forecasting.series import DemandPoint, PreparedSeries
from app.initiatives.i7.forecasting.types import (
    BacktestMetrics,
    BacktestResult,
    BacktestStatus,
    ForecastResult,
    MetricStatus,
    ModelName,
    ModelStatus,
    OriginForecast,
)
from app.initiatives.i7.locking import NoLock
from app.initiatives.i7.policy import PolicyDocument
from app.models.base import Base
from app.models.i7_features import FeatureBuildRun
from app.models.i7_forecast import Forecast, ForecastBacktestPath, ForecastRun, SegmentModelDecision

POLICY = PolicyDocument()
TOTAL = 12  # material-plants; every 4th is SES (smooth), the rest SBA
ROWS = 21   # SBA + LightGBM = 2 rows each (9), SES alone (ARIMA off) = 1 row each (3)


class Killed(BaseException):
    """A process dying: not an Exception, so run_forecasting cannot catch it."""


class Models:
    """Fake models. Records what was computed; can die at the Nth material-plant."""

    def __init__(self, die_at: int | None = None, exc: type[BaseException] = Killed) -> None:
        self.computed: list[str] = []
        self.die_at, self.exc = die_at, exc

    def __call__(self, candidate, pooled, status, detail, trigger, quantile, arima_enabled=True):
        if self.die_at is not None and len(self.computed) == self.die_at:
            raise self.exc("the process died")
        self.computed.append(candidate.material)
        if candidate.baseline_model == "SES":
            plan = [(ModelName.SES, True)] + ([(ModelName.AUTO_ARIMA, False)] if arima_enabled else [])
        else:
            plan = [(ModelName.SBA, True), (ModelName.LIGHTGBM, False)]
        return [(_forecast(m), _backtest(m), baseline) for m, baseline in plan]


def _forecast(model: ModelName) -> ForecastResult:
    return ForecastResult(
        model=model, model_version=f"{model.value}-1", status=ModelStatus.SUCCESS, rate=Decimal("5"),
        unit="EA", training_start=date(2025, 1, 1), training_end=date(2026, 1, 1), horizon_months=2,
        parameters=(("alpha", "0.1"),),
    )


def _backtest(model: ModelName) -> BacktestResult:
    metrics = BacktestMetrics(
        pinball_loss=Decimal("1"), pinball_status=MetricStatus.AVAILABLE, mean_error=Decimal("0"),
        bias_percentage=Decimal("0"), fill_rate=None, fill_rate_status=MetricStatus.NOT_EVALUABLE,
        holding_cost=None, holding_cost_status=MetricStatus.NOT_EVALUABLE, mean_absolute_error=Decimal("1"),
    )
    path = OriginForecast(date(2025, 6, 1), 1, date(2025, 7, 1), Decimal("5"), Decimal("4"))
    return BacktestResult(
        model=model, model_version=f"{model.value}-1", status=BacktestStatus.PARTIAL_DEVELOPMENT_DATA,
        required_origins=12, available_origins=10, origins_evaluated=10, metrics=metrics, paths=(path,),
    )


def candidates() -> list[service._Candidate]:
    series = PreparedSeries(points=(DemandPoint(date(2025, 1, 1), Decimal("3")),), unit="EA")
    return [
        service._Candidate(
            material=f"{i:04d}", plant="1300", demand_class="SMOOTH" if i % 4 == 0 else "INTERMITTENT",
            history_status="SUFFICIENT", baseline_model="SES" if i % 4 == 0 else "SBA", lead_time_months=2,
            lead_time_source="PLANNED_DELIVERY_TIME", series=series,
        )
        for i in range(TOTAL)
    ]


def make_env(monkeypatch, *, arima: bool = False, every: int = 3):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(
        engine,
        tables=[
            FeatureBuildRun.__table__, ForecastRun.__table__, Forecast.__table__,
            ForecastBacktestPath.__table__, SegmentModelDecision.__table__,
        ],
    )
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(FeatureBuildRun(status="succeeded", features_built=TOTAL, policy_id="p", policy_version=1))
        session.commit()
    settings = SimpleNamespace(i7_forecast_arima_enabled=arima)
    monkeypatch.setattr(service, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(service, "get_settings", lambda: settings)
    monkeypatch.setattr(service, "_load_candidates", lambda session: candidates())
    monkeypatch.setattr(service.lightgbm_model, "train", lambda corpus, q: (None, ModelStatus.SUCCESS, ""))
    monkeypatch.setattr(service, "CHECKPOINT_EVERY", every)
    monkeypatch.setattr(service, "CHECKPOINT_SECONDS", 10_000)
    return factory, settings


@pytest.fixture
def env(monkeypatch):
    return make_env(monkeypatch)


def run(models: Models, monkeypatch, **kwargs):
    monkeypatch.setattr(service, "_run_models", models)
    return service.run_forecasting(POLICY, lock=NoLock(), **kwargs)


def rows(factory, run_id=None):
    with factory() as session:
        query = select(Forecast.sap_material_number, Forecast.model_name, Forecast.is_champion, Forecast.adoption_status)
        if run_id is not None:
            query = query.where(Forecast.forecast_run_id == run_id)
        return sorted(session.execute(query).all())


def runs(factory):
    with factory() as session:
        return list(session.scalars(select(ForecastRun).order_by(ForecastRun.id)))


def saved_materials(factory, run_id=1):
    with factory() as session:
        return sorted({m for m, in session.execute(select(Forecast.sap_material_number).where(Forecast.forecast_run_id == run_id))})


# --- a clean run ----------------------------------------------------------


def test_a_clean_run_saves_everything_and_succeeds(env, monkeypatch) -> None:
    factory, _ = env
    result = run(Models(), monkeypatch)
    assert result.status == "succeeded" and result.error is None
    assert result.materials_evaluated == TOTAL and result.forecasts_written == ROWS
    [stored] = runs(factory)
    assert (stored.status, stored.forecasts_written, stored.materials_evaluated) == ("succeeded", ROWS, TOTAL)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(SegmentModelDecision)) == TOTAL
        assert session.scalar(select(func.count()).select_from(ForecastBacktestPath)) == TOTAL  # one champion path each
    assert len(result.segment_decisions) == TOTAL


def test_the_work_is_saved_in_batches_as_it_goes(env, monkeypatch) -> None:
    factory, _ = env
    with pytest.raises(Killed):
        run(Models(die_at=8), monkeypatch)  # dies while forecasting the 9th
    # 8 done; batches of 3 -> two batches saved (6), the 2 in flight were not.
    assert saved_materials(factory) == [f"{i:04d}" for i in range(6)]
    [stored] = runs(factory)
    assert stored.status == "running"  # a killed process cannot write its own ending


def test_a_material_plant_is_saved_whole_or_not_at_all(env, monkeypatch) -> None:
    factory, _ = env
    with pytest.raises(Killed):
        run(Models(die_at=7), monkeypatch)
    with factory() as session:
        per_material = session.execute(
            select(Forecast.sap_material_number, func.count()).group_by(Forecast.sap_material_number)
        ).all()
        decisions = session.scalar(select(func.count()).select_from(SegmentModelDecision))
    # every model's rows for a material-plant, never just some of them
    assert all(n == (1 if int(m) % 4 == 0 else 2) for m, n in per_material)
    assert decisions == len(per_material)


def test_time_alone_triggers_a_save_when_the_models_are_slow(monkeypatch) -> None:
    factory, _ = make_env(monkeypatch, every=1000)
    monkeypatch.setattr(service, "CHECKPOINT_SECONDS", 0)
    with pytest.raises(Killed):
        run(Models(die_at=5), monkeypatch)
    assert len(saved_materials(factory)) == 5  # saved after each one, not after 1000


# --- resume ---------------------------------------------------------------


def test_a_rerun_resumes_and_computes_only_what_is_missing(env, monkeypatch) -> None:
    factory, _ = env
    with pytest.raises(Killed):
        run(Models(die_at=8), monkeypatch)
    second = Models()
    result = run(second, monkeypatch)
    assert second.computed == [f"{i:04d}" for i in range(6, TOTAL)]  # nothing already saved is redone
    assert result.run_id == 1 and result.status == "succeeded"
    assert [r.status for r in runs(factory)] == ["succeeded"]


def test_the_resumed_result_is_the_same_as_an_uninterrupted_one(monkeypatch) -> None:
    factory, _ = make_env(monkeypatch)
    with pytest.raises(Killed):
        run(Models(die_at=8), monkeypatch)
    resumed = run(Models(), monkeypatch)
    after_resume = rows(factory)

    clean_factory, _ = make_env(monkeypatch)
    clean = run(Models(), monkeypatch)
    assert after_resume == rows(clean_factory)
    assert resumed.forecasts_written == clean.forecasts_written == ROWS
    assert len(resumed.segment_decisions) == len(clean.segment_decisions) == TOTAL
    assert resumed.model_status_counts == clean.model_status_counts
    assert resumed.backtest_status_counts == clean.backtest_status_counts
    assert sum(resumed.model_status_counts.values()) == ROWS


def test_nothing_is_saved_twice(env, monkeypatch) -> None:
    factory, _ = env
    with pytest.raises(Killed):
        run(Models(die_at=5), monkeypatch)
    run(Models(), monkeypatch)
    with factory() as session:
        duplicates = session.execute(
            select(Forecast.sap_material_number, Forecast.model_name, func.count())
            .group_by(Forecast.sap_material_number, Forecast.model_name)
            .having(func.count() > 1)
        ).all()
    assert duplicates == []


def test_a_run_that_failed_with_an_error_is_resumed_too(env, monkeypatch) -> None:
    factory, _ = env
    result = run(Models(die_at=8, exc=RuntimeError), monkeypatch)  # an ordinary failure, recorded
    assert result.status == "failed" and "the process died" in result.error
    assert runs(factory)[0].status == "failed"
    resumed = Models()
    assert run(resumed, monkeypatch).status == "succeeded"
    assert resumed.computed == [f"{i:04d}" for i in range(6, TOTAL)]


def test_a_run_with_everything_saved_finishes_without_recomputing(env, monkeypatch) -> None:
    factory, _ = env
    run(Models(), monkeypatch)
    with factory() as session:  # the process died after the last batch, before writing its ending
        stored = session.get(ForecastRun, 1)
        stored.status, stored.finished_at = "running", None
        session.commit()
    again = Models()
    result = run(again, monkeypatch)
    assert again.computed == [] and result.status == "succeeded" and result.run_id == 1
    assert result.forecasts_written == ROWS


def test_resume_false_starts_a_fresh_run(env, monkeypatch) -> None:
    factory, _ = env
    with pytest.raises(Killed):
        run(Models(die_at=5), monkeypatch)
    fresh = Models()
    assert run(fresh, monkeypatch, resume=False).run_id == 2
    assert len(fresh.computed) == TOTAL


def test_a_new_feature_build_starts_a_fresh_run(env, monkeypatch) -> None:
    """Rows built from earlier features must not be mixed with the new ones."""
    factory, _ = env
    with pytest.raises(Killed):
        run(Models(die_at=5), monkeypatch)
    with factory() as session:
        session.add(FeatureBuildRun(status="succeeded", features_built=TOTAL, policy_id="p", policy_version=1))
        session.commit()
    models = Models()
    assert run(models, monkeypatch).run_id == 2
    assert len(models.computed) == TOTAL
    assert [r.status for r in runs(factory)] == ["failed", "succeeded"]


def test_dead_running_runs_are_marked_failed(env, monkeypatch) -> None:
    factory, _ = env
    with factory() as session:
        for _ in range(3):
            session.add(ForecastRun(status="running", policy_id="i07-default", policy_version=1))
        session.commit()
    run(Models(), monkeypatch)
    statuses = [r.status for r in runs(factory)]
    assert statuses == ["failed", "failed", "failed", "succeeded"]
    assert all("abandoned" in r.error for r in runs(factory)[:3])


def test_a_held_lock_refuses_without_touching_anything(env, monkeypatch) -> None:
    factory, _ = env

    class Held:
        def acquire(self) -> bool:
            return False

        def release(self) -> None:
            raise AssertionError("never acquired, so never released")

    monkeypatch.setattr(service, "_run_models", Models())
    result = service.run_forecasting(POLICY, lock=Held())
    assert result.status == "failed" and "forecasting lock" in result.error
    assert runs(factory) == []


def test_the_lock_is_released_even_when_the_process_is_killed(env, monkeypatch) -> None:
    class Spy(NoLock):
        released = False

        def release(self) -> None:
            Spy.released = True

    monkeypatch.setattr(service, "_run_models", Models(die_at=2))
    with pytest.raises(Killed):
        service.run_forecasting(POLICY, lock=Spy())
    assert Spy.released


# --- the ARIMA switch -----------------------------------------------------


def test_arima_is_off_by_default_and_smooth_demand_runs_ses_alone(env, monkeypatch) -> None:
    from app.core.config import Settings

    assert Settings(_env_file=None).i7_forecast_arima_enabled is False
    factory, _ = env
    run(Models(), monkeypatch)
    models = {m for _, m, _, _ in rows(factory)}
    assert ModelName.AUTO_ARIMA.value not in models
    with factory() as session:
        smooth = session.execute(
            select(SegmentModelDecision.adoption_status, SegmentModelDecision.decision_reason)
            .where(SegmentModelDecision.segment_key.like("0000/%"))
        ).one()
    assert smooth == ("NOT_EVALUABLE", "challenger not run")


def test_intermittent_and_lumpy_demand_are_unaffected_by_the_switch(monkeypatch) -> None:
    results = {}
    for flag in (False, True):
        factory, _ = make_env(monkeypatch, arima=flag)
        run(Models(), monkeypatch)
        results[flag] = [r for r in rows(factory) if int(r[0]) % 4 != 0]
    assert results[False] == results[True]


def test_turning_arima_on_adds_it_as_the_smooth_challenger(monkeypatch) -> None:
    factory, _ = make_env(monkeypatch, arima=True)
    run(Models(), monkeypatch)
    assert ModelName.AUTO_ARIMA.value in {m for _, m, _, _ in rows(factory)}


def test_a_run_is_not_resumed_across_a_change_of_the_arima_setting(monkeypatch) -> None:
    for first, second in ((True, False), (False, True)):
        factory, settings = make_env(monkeypatch, arima=first)
        with pytest.raises(Killed):
            run(Models(die_at=6), monkeypatch)
        settings.i7_forecast_arima_enabled = second
        models = Models()
        assert run(models, monkeypatch).run_id == 2, (first, second)
        assert len(models.computed) == TOTAL
