"""The trend history: the KPIs recorded per generation, and the endpoint that reads them.

Three promises are tested:

* The numbers are the Overview's. Snapshot figures come from the same functions as the summary
  endpoint, and a parity test holds them to the endpoint's own output.
* Recording is safe. All three scopes or none, once per generation however many times it is
  called or raced, and a failure never reaches the generation that triggered it.
* Nothing is invented. A reconstructed point has a null stockout count, an empty history is empty,
  and a generation that was never calculated is not turned into "0 excess".

SQLite with only the tables these read.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import MetaData, create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i7 import recommendations as api
from app.api.i7.deps import get_session
from app.initiatives.i7.recommendations import kpi_metrics, service, trend_backfill, trend_snapshots
from app.initiatives.i7.recommendations.kpi_metrics import base_for_scope, compute_kpi_metrics
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedStock
from app.models.i7_trend import RecommendationTrendSnapshot

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in (Recommendation, StagedStock, StagedMaterial, RecommendationTrendSnapshot):
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    return engine


@pytest.fixture
def db(engine):
    with Session(engine) as session:
        yield session


_n = {"i": 0}


def rec(
    material, plant="1300", *, rop=None, cur_max=None, rec_max=None, price=None, cur_ss=None, rec_ss=None,
    status="READY_FOR_REVIEW", run=1, adjusted=0, minutes=None,
):
    _n["i"] += 1
    return Recommendation(
        recommendation_id=f"REC-{material}-{plant}", sap_material_number=material, sap_plant_code=plant,
        feature_run_id=run, forecast_run_id=run, inventory_run_id=run, oar_run_id=None, policy_id="p",
        policy_version=1, formula_version="f1", status=status, impact_status="X", recommended_rop=rop,
        current_max_stock=cur_max, recommended_max_stock=rec_max, unit_price=price,
        current_safety_stock=cur_ss, recommended_safety_stock=rec_ss, adjustment_count=adjusted,
        generated_at=BASE + timedelta(minutes=_n["i"] if minutes is None else minutes),
    )


def stock(material, plant, quantity, location="0001"):
    return StagedStock(
        sap_material_number=material, sap_plant_code=plant, storage_location=location,
        unrestricted_use_stock=Decimal(quantity), staging_run_id=1, source_table="n_mard",
    )


def priced(material, currency="USD", price="1"):
    return StagedMaterial(
        sap_material_number=material, currency=currency, unit_price=Decimal(price), staging_run_id=1,
        source_table="n_mara",
    )


def seed_portfolio(db):
    """Three plants, so 'ALL' is visibly not the sum of the two named ones."""
    db.add_all([
        # 1300
        rec("A", "1300", rop=10, cur_max=100, rec_max=60, price=2, cur_ss=20, rec_ss=15),   # at risk, excess 40*2=80, ss 5*2=10
        rec("B", "1300", rop=10, cur_max=50, rec_max=50, price=3),                           # on-hand == rop: not at risk; no excess
        rec("C", "1300", rop=None, cur_max=30, rec_max=10),                                  # no ROP; excess 20 but no price
        rec("D", "1300", rop=5, cur_max=None, rec_max=None),                                 # at risk (0 < 5); no max stock
        # 1500
        rec("A", "1500", rop=8, cur_max=40, rec_max=30, price=5),                            # at risk (3 < 8); excess 10*5=50
        # a third plant, outside the two named scopes
        rec("E", "3000", rop=4, cur_max=9, rec_max=1, price=1),                              # at risk; excess 8
        stock("A", "1300", "4"), stock("A", "1300", "3", "0002"),     # summed across storage locations: 7 < 10
        stock("B", "1300", "10"), stock("C", "1300", "0"), stock("D", "1300", "0"),
        stock("A", "1500", "3"), stock("E", "3000", "2"),
        priced("A"),
    ])
    db.commit()


def summary(db):
    return api.get_recommendation_summary(
        session=db, status=None, plant=None, material=None, demand_class=None, is_oar=None,
        confidence=None, criticality=None,
    )


# --- The KPIs are the Overview's ----------------------------------------------------------------


def test_the_shared_calculation_matches_the_overview_summary_exactly(db):
    seed_portfolio(db)
    metrics = compute_kpi_metrics(db, base_for_scope(None))
    overview = summary(db)
    assert metrics.total == overview.total
    assert metrics.stockout_risk_count == overview.critical_stockout_risk_count
    assert metrics.excess_count == overview.excess_inventory_candidates_count
    assert metrics.excess_opportunity == overview.excess_inventory_opportunity
    assert metrics.net_safety_stock_value_impact == overview.net_safety_stock_value_impact


def test_the_formulas_on_known_numbers(db):
    seed_portfolio(db)
    whole = compute_kpi_metrics(db, base_for_scope(None))
    # at risk: A/1300 (7<10), D/1300 (0<5), A/1500 (3<8), E/3000 (2<4). B is equal to its ROP, C has none.
    assert whole.stockout_risk_count == 4
    # excess: A/1300, C/1300, A/1500, E/3000. Opportunity counts priced rows only: 80 + 50 + 8.
    assert whole.excess_count == 4
    assert whole.excess_opportunity == Decimal("138")
    assert whole.net_safety_stock_value_impact == Decimal("10")  # A/1300: 2 * (20 - 15)


def test_stock_in_several_storage_locations_is_summed_before_comparing(db):
    db.add_all([rec("A", rop=10), stock("A", "1300", "6", "0001"), stock("A", "1300", "5", "0002")])
    db.commit()
    assert compute_kpi_metrics(db, base_for_scope("1300")).stockout_risk_count == 0  # 11 is not below 10


def test_no_staged_stock_is_neither_at_risk_nor_safe(db):
    db.add(rec("A", rop=10))
    db.commit()
    assert compute_kpi_metrics(db, base_for_scope(None)).stockout_risk_count == 0


def test_no_priced_excess_gives_no_opportunity_not_zero(db):
    db.add(rec("C", cur_max=30, rec_max=10))
    db.commit()
    metrics = compute_kpi_metrics(db, base_for_scope(None))
    assert metrics.excess_count == 1 and metrics.excess_opportunity is None


def test_only_the_newest_row_of_each_material_plant_counts(db):
    db.add_all([rec("A", cur_max=100, rec_max=10, price=1, run=1), rec("A", cur_max=10, rec_max=10, price=1, run=2)])
    db.commit()
    metrics = compute_kpi_metrics(db, base_for_scope(None))
    assert metrics.total == 1 and metrics.excess_count == 0


def test_all_is_computed_over_everything_not_summed_from_the_plants(db):
    seed_portfolio(db)
    all_scope = compute_kpi_metrics(db, base_for_scope(None))
    plants = [compute_kpi_metrics(db, base_for_scope(p)) for p in ("1300", "1500")]
    assert all_scope.total == 6 and sum(p.total for p in plants) == 5  # plant 3000 is in ALL only
    assert all_scope.excess_count == sum(p.excess_count for p in plants) + 1


# --- Recording ------------------------------------------------------------------------------------


KEY = trend_snapshots.generation_key(
    inventory_run_id=7, oar_run_id=3, forecast_run_id=5, formula_version="f1", policy_id="p", policy_version=1
)


def record(db, key=KEY, **kw):
    return trend_snapshots.record_generation_snapshots(
        db, key=key, inventory_run_id=7, oar_run_id=3, forecast_run_id=5, formula_version="f1", **kw
    )


def snapshots(db):
    return db.execute(select(RecommendationTrendSnapshot).order_by(RecommendationTrendSnapshot.id)).scalars().all()


def test_a_generation_gets_one_snapshot_per_scope_with_the_overview_numbers(db):
    seed_portfolio(db)
    outcome = record(db)
    assert sorted(outcome.created) == ["1300", "1500", "ALL"]
    by_scope = {row.plant_scope: row for row in snapshots(db)}
    assert by_scope["ALL"].total_count == 6 and by_scope["ALL"].stockout_risk_count == 4
    assert by_scope["1300"].stockout_risk_count == 2 and by_scope["1300"].excess_count == 2
    assert by_scope["1500"].stockout_risk_count == 1 and by_scope["1500"].excess_opportunity == Decimal("50")
    assert {row.source for row in by_scope.values()} == {"captured"}
    assert by_scope["ALL"].currency == "USD"
    assert by_scope["ALL"].inventory_run_id == 7 and by_scope["ALL"].oar_run_id == 3


def test_recording_the_same_generation_again_adds_nothing(db):
    seed_portfolio(db)
    record(db)
    again = record(db)
    assert again.created == [] and sorted(again.already_recorded) == ["1300", "1500", "ALL"]
    assert len(snapshots(db)) == 3


def test_only_a_missing_scope_is_filled_in(db):
    seed_portfolio(db)
    record(db)
    db.execute(RecommendationTrendSnapshot.__table__.delete().where(RecommendationTrendSnapshot.plant_scope == "1500"))
    db.commit()
    assert record(db).created == ["1500"]
    assert len(snapshots(db)) == 3


def test_an_earlier_snapshot_is_untouched_when_a_later_generation_is_recorded(db):
    seed_portfolio(db)
    record(db)
    before = {row.plant_scope: (row.excess_count, row.stockout_risk_count, row.captured_at) for row in snapshots(db)}
    db.add_all([rec("Z", "1300", rop=100, cur_max=500, rec_max=1, price=1, run=2), stock("Z", "1300", "0")])
    db.commit()
    later = trend_snapshots.generation_key(
        inventory_run_id=8, oar_run_id=3, forecast_run_id=5, formula_version="f1", policy_id="p", policy_version=1
    )
    record(db, key=later)
    rows = snapshots(db)
    assert len(rows) == 6
    for row in rows[:3]:
        assert (row.excess_count, row.stockout_risk_count, row.captured_at) == before[row.plant_scope]
    assert rows[3].plant_scope == "ALL" and rows[3].excess_count == before["ALL"][0] + 1


def test_a_failure_part_way_leaves_no_snapshot_at_all(db, monkeypatch):
    seed_portfolio(db)
    real = trend_snapshots.compute_kpi_metrics

    def fail_on_1500(session, base):
        if "1500" in str(base.compile(compile_kwargs={"literal_binds": True})):
            raise RuntimeError("database went away")
        return real(session, base)

    monkeypatch.setattr(trend_snapshots, "compute_kpi_metrics", fail_on_1500)
    with pytest.raises(RuntimeError):
        record(db)
    assert snapshots(db) == []


def test_a_writer_that_loses_a_race_is_not_an_error_and_adds_no_duplicate(db, engine, monkeypatch):
    seed_portfolio(db)
    real = trend_snapshots.compute_kpi_metrics

    def other_worker_wins(session, base):
        metrics = real(session, base)
        with Session(engine) as other:  # records the same generation while this one is computing
            other.add_all([
                RecommendationTrendSnapshot(
                    generation_key=KEY, plant_scope=scope, source="captured", captured_at=BASE, total_count=1,
                    excess_count=0,
                )
                for scope in ("ALL", "1300", "1500")
            ])
            other.commit()
        monkeypatch.setattr(trend_snapshots, "compute_kpi_metrics", real)
        return metrics

    monkeypatch.setattr(trend_snapshots, "compute_kpi_metrics", other_worker_wins)
    outcome = record(db)
    assert outcome.created == [] and len(snapshots(db)) == 3


def test_the_generation_key_names_the_runs_and_never_writes_none_as_zero():
    assert KEY == "inv=7|oar=3|fc=5|formula=f1|policy=p:1"
    key = trend_snapshots.generation_key(
        inventory_run_id=None, oar_run_id=None, forecast_run_id=None, formula_version="f1", policy_id="p", policy_version=1
    )
    assert "inv=none" in key and "=0" not in key


# --- The hook in the generator --------------------------------------------------------------------


def run_hook(engine, rows, result=None):
    factory = sessionmaker(bind=engine)
    result = result or service.RecommendationRunResult()
    service._record_trend_snapshots(
        factory, result, rows=rows, inventory_run_id=7, oar_run_id=3, forecast_run_id=5
    )
    return result


ROW = {"formula_version": "f1", "policy_id": "p", "policy_version": 1}


def test_the_hook_records_after_a_successful_generation_and_repeats_are_harmless(engine, db):
    seed_portfolio(db)
    assert run_hook(engine, [ROW]).trend_snapshots == "recorded"
    assert run_hook(engine, [ROW]).trend_snapshots == "already_recorded"
    assert len(snapshots(db)) == 3


def test_the_hook_does_nothing_when_no_recommendation_was_generated(engine, db):
    result = run_hook(engine, [])
    assert result.trend_snapshots == "skipped" and snapshots(db) == []


def test_a_snapshot_failure_is_reported_and_does_not_fail_the_generation(engine, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("snapshot table missing")

    monkeypatch.setattr(service, "record_generation_snapshots", boom)
    with caplog.at_level("ERROR"):
        result = run_hook(engine, [ROW])
    assert result.status == service.STATUS_SUCCEEDED  # the recommendations are already saved
    assert result.trend_snapshots == "failed" and "snapshot table missing" in result.trend_snapshot_error
    assert "trend snapshot recording failed" in caplog.text


def test_the_recorder_is_called_only_after_the_generation_commits():
    """In generate_recommendations the call sits after the session block that commits the
    recommendations, and the failure path returns before reaching it."""
    import inspect

    source = inspect.getsource(service.generate_recommendations)
    assert source.index("session.commit()") < source.index("_record_trend_snapshots(")
    assert source.index("_record_trend_snapshots(") < source.index("except Exception")


# --- The endpoint ---------------------------------------------------------------------------------


@pytest.fixture
def client(engine):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/i7")

    def session():
        with Session(engine) as s:
            yield s

    app.dependency_overrides[get_session] = session
    return TestClient(app)


def add_point(db, scope="ALL", day=1, *, source="captured", stockout=3, excess=4, opportunity="10", key=None):
    db.add(RecommendationTrendSnapshot(
        generation_key=key or f"k{scope}{day}{source}", plant_scope=scope, source=source,
        captured_at=BASE + timedelta(days=day), total_count=100, stockout_risk_count=stockout,
        excess_count=excess, excess_opportunity=Decimal(opportunity) if opportunity is not None else None,
        currency="USD" if source == "captured" else None,
    ))
    db.commit()


def test_an_empty_history_has_a_consistent_shape(client):
    body = client.get("/api/v1/i7/recommendations/trends").json()
    assert body["points"] == [] and body["plant"] == "ALL"
    assert body["has_stockout_history"] is False and body["has_excess_history"] is False
    assert body["captured_count"] == 0 and body["reconstructed_count"] == 0
    assert "not yet been confirmed" in body["definition_note"]


def test_the_trends_route_is_not_swallowed_by_the_single_recommendation_route(client):
    assert client.get("/api/v1/i7/recommendations/trends").status_code == 200


def test_points_come_back_oldest_first_for_the_requested_scope_only(client, db):
    for day in (3, 1, 2):
        add_point(db, "ALL", day)
    add_point(db, "1300", 1)
    body = client.get("/api/v1/i7/recommendations/trends", params={"plant": "ALL"}).json()
    times = [p["captured_at"] for p in body["points"]]
    assert times == sorted(times) and len(times) == 3
    assert len(client.get("/api/v1/i7/recommendations/trends", params={"plant": "1300"}).json()["points"]) == 1
    assert client.get("/api/v1/i7/recommendations/trends", params={"plant": "1500"}).json()["points"] == []


def test_an_unknown_plant_is_rejected(client):
    assert client.get("/api/v1/i7/recommendations/trends", params={"plant": "9999"}).status_code == 422


def test_a_backwards_date_range_is_rejected(client):
    response = client.get("/api/v1/i7/recommendations/trends", params={"since": "2026-10-09", "until": "2026-10-01"})
    assert response.status_code == 400


def test_the_date_range_is_inclusive_of_both_days(client, db):
    for day in (0, 1, 2, 3):
        add_point(db, "ALL", day)
    body = client.get("/api/v1/i7/recommendations/trends", params={"since": "2026-10-02", "until": "2026-10-03"}).json()
    assert [p["captured_at"][:10] for p in body["points"]] == ["2026-10-02", "2026-10-03"]


def test_limit_keeps_the_most_recent_points_still_in_order(client, db):
    for day in range(1, 6):
        add_point(db, "ALL", day)
    body = client.get("/api/v1/i7/recommendations/trends", params={"limit": 2}).json()
    assert [p["captured_at"][:10] for p in body["points"]] == ["2026-10-05", "2026-10-06"]


def test_reconstructed_points_never_carry_a_stockout_count(client, db):
    add_point(db, "ALL", 1, source="reconstructed", stockout=None)
    body = client.get("/api/v1/i7/recommendations/trends").json()
    assert body["points"][0]["stockout_risk_count"] is None and body["points"][0]["source"] == "reconstructed"
    assert body["has_stockout_history"] is False and body["has_excess_history"] is True
    assert body["reconstructed_count"] == 1


def test_stockout_history_starts_at_the_first_captured_point(client, db):
    add_point(db, "ALL", 1, source="reconstructed", stockout=None)
    add_point(db, "ALL", 2, source="captured", stockout=7)
    body = client.get("/api/v1/i7/recommendations/trends").json()
    assert [p["stockout_risk_count"] for p in body["points"]] == [None, 7]
    assert body["has_stockout_history"] is True and body["captured_count"] == 1


def test_a_missing_opportunity_stays_null_and_is_not_shown_as_zero(client, db):
    add_point(db, "ALL", 1, opportunity=None)
    assert client.get("/api/v1/i7/recommendations/trends").json()["points"][0]["excess_opportunity"] is None


def test_reading_the_trends_writes_nothing(client, db):
    add_point(db, "ALL", 1)
    before = db.execute(select(func.count()).select_from(RecommendationTrendSnapshot)).scalar_one()
    client.get("/api/v1/i7/recommendations/trends")
    assert db.execute(select(func.count()).select_from(RecommendationTrendSnapshot)).scalar_one() == before


# --- Backfill -------------------------------------------------------------------------------------


def seed_two_generations(db):
    """Generation 1 (feature run 1): A has an excess of 40. Generation 2 (run 2): A is fixed, B is new."""
    db.add_all([
        rec("A", "1300", cur_max=100, rec_max=60, price=2, run=1, minutes=1),
        rec("A", "1500", cur_max=10, rec_max=10, price=2, run=1, minutes=2),
        rec("A", "1300", cur_max=60, rec_max=60, price=2, run=2, minutes=100),
        rec("B", "1300", cur_max=30, rec_max=10, price=1, run=2, minutes=101),
    ])
    db.commit()


def test_each_generation_is_rebuilt_as_it_stood_then(db):
    seed_two_generations(db)
    plans = trend_backfill.build_plan(db)
    assert [p.reconstructable for p in plans] == [True, True]
    first, second = ({r.plant_scope: r for r in p.rows} for p in plans)
    assert (first["ALL"].total_count, first["ALL"].excess_count, first["ALL"].excess_opportunity) == (2, 1, Decimal("80"))
    # After generation 2: A/1300 is no longer excess, B is -- and A/1500 (from run 1) still stands.
    assert (second["ALL"].total_count, second["ALL"].excess_count, second["ALL"].excess_opportunity) == (3, 1, Decimal("20"))
    assert second["1500"].total_count == 1 and second["1300"].excess_count == 1


def test_rebuilt_rows_never_claim_a_stockout_count_or_a_currency(db):
    seed_two_generations(db)
    for plan in trend_backfill.build_plan(db):
        for row in plan.rows:
            assert row.stockout_risk_count is None and row.currency is None and row.source == "reconstructed"


def test_a_generation_that_was_never_calculated_is_not_turned_into_zero_excess(db):
    db.add_all([rec("A", cur_max=None, rec_max=None, run=1), rec("B", cur_max=None, rec_max=5, run=1)])
    db.commit()
    (plan,) = trend_backfill.build_plan(db)
    assert plan.reconstructable is False and "not calculated" in plan.reason and plan.rows == ()


def test_a_dry_run_writes_nothing_and_apply_writes_each_scope_once(db, engine, monkeypatch, capsys):
    seed_two_generations(db)
    monkeypatch.setattr(trend_backfill, "get_sessionmaker", lambda: sessionmaker(bind=engine))
    assert trend_backfill.main([]) == 0
    assert "Dry run: nothing written" in capsys.readouterr().out
    assert snapshots(db) == []

    trend_backfill.main(["--apply"])
    assert len(snapshots(db)) == 6  # two generations x three scopes
    trend_backfill.main(["--apply"])
    assert len(snapshots(db)) == 6  # already recorded: not duplicated


def test_the_dry_run_reports_prices_and_adjustments_it_could_not_be_sure_of(db):
    db.add_all([
        rec("A", cur_max=100, rec_max=60, price=None, adjusted=1, run=1),
        rec("B", cur_max=100, rec_max=60, price=2, run=1),
    ])
    db.commit()
    (plan,) = trend_backfill.build_plan(db)
    assert plan.excess_without_price == 1 and plan.adjusted == 1
    text = trend_backfill.render([plan])
    assert "excess_without_price=1" in text and "adjusted=1" in text


def test_backfilled_points_appear_in_the_api_as_reconstructed(client, db):
    seed_two_generations(db)
    trend_backfill.apply_plan(db, trend_backfill.build_plan(db))
    body = client.get("/api/v1/i7/recommendations/trends").json()
    assert body["reconstructed_count"] == 2 and body["has_stockout_history"] is False


# --- Snapshot-only command ------------------------------------------------------------------------

from app.initiatives.i7.recommendations import trend_snapshot_now  # noqa: E402


@pytest.fixture
def latest_runs(monkeypatch):
    """The upstream runs the command believes are the latest (the real ones come from SQL tables
    this SQLite fixture does not have)."""
    runs = {"inventory": 1, "oar": None, "forecast": 1}
    monkeypatch.setattr(trend_snapshot_now.repository, "latest_inventory_run", lambda s: runs["inventory"])
    monkeypatch.setattr(trend_snapshot_now.repository, "latest_oar_run", lambda s: runs["oar"])
    monkeypatch.setattr(trend_snapshot_now.repository, "forecast_run_for_inventory_run", lambda s, i: runs["forecast"])
    return runs


@pytest.fixture
def now_cli(engine, monkeypatch):
    monkeypatch.setattr(trend_snapshot_now, "get_sessionmaker", lambda: sessionmaker(bind=engine))


def recommendation_fingerprint(db):
    return db.execute(
        select(func.count(), func.sum(Recommendation.id), func.max(Recommendation.updated_at)).select_from(Recommendation)
    ).one()


def test_the_dry_run_prints_the_figures_and_writes_nothing(db, now_cli, latest_runs, capsys):
    seed_portfolio(db)
    assert trend_snapshot_now.main([]) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing written" in out and "would be recorded" in out and "inv=1|oar=none|fc=1" in out
    assert snapshots(db) == []


def test_apply_records_the_three_scopes_and_never_touches_a_recommendation(db, now_cli, latest_runs, capsys):
    seed_portfolio(db)
    before = recommendation_fingerprint(db)
    assert trend_snapshot_now.main(["--apply"]) == 0
    assert sorted(r.plant_scope for r in snapshots(db)) == ["1300", "1500", "ALL"]
    assert recommendation_fingerprint(db) == before
    assert {r.source for r in snapshots(db)} == {"captured"}


def test_the_figures_are_the_overviews(db, now_cli, latest_runs):
    seed_portfolio(db)
    trend_snapshot_now.main(["--apply"])
    all_row = next(r for r in snapshots(db) if r.plant_scope == "ALL")
    overview = summary(db)
    assert all_row.stockout_risk_count == overview.critical_stockout_risk_count
    assert all_row.excess_count == overview.excess_inventory_candidates_count
    assert all_row.excess_opportunity == overview.excess_inventory_opportunity


def test_running_it_twice_adds_nothing(db, now_cli, latest_runs, capsys):
    seed_portfolio(db)
    trend_snapshot_now.main(["--apply"])
    capsys.readouterr()
    assert trend_snapshot_now.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "Recorded: nothing new" in out and "already recorded" in out
    assert len(snapshots(db)) == 3


def test_the_next_real_generation_on_the_same_inputs_finds_it_already_recorded(db, engine, now_cli, latest_runs):
    """The key it records is the key the generator builds, so there is no double entry."""
    seed_portfolio(db)
    trend_snapshot_now.main(["--apply"])
    generator_view = service.RecommendationRunResult()
    service._record_trend_snapshots(
        sessionmaker(bind=engine), generator_view, rows=[ROW], inventory_run_id=1, oar_run_id=None, forecast_run_id=1
    )
    assert generator_view.trend_snapshots == "already_recorded"
    assert len(snapshots(db)) == 3


def test_it_refuses_when_a_newer_run_exists_that_was_never_turned_into_recommendations(db, now_cli, latest_runs, capsys):
    seed_portfolio(db)
    latest_runs["inventory"] = 99  # a newer inventory run; the recommendations still come from run 1
    assert trend_snapshot_now.main(["--apply"]) == 1
    assert "were not built from the latest runs" in capsys.readouterr().out
    assert snapshots(db) == []


def test_it_refuses_when_there_are_no_recommendations(db, now_cli, latest_runs, capsys):
    assert trend_snapshot_now.main(["--apply"]) == 1
    assert "no recommendations yet" in capsys.readouterr().out and snapshots(db) == []
