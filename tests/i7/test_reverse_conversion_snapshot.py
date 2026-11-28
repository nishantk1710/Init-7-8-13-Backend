"""The stored Min-Max -> OAR recommendation: the pipeline stage that writes it and the reader the page uses.

An in-memory SQLite database holds just the four ``i7_reverse_*`` tables; the data source is the fake one.
"""

from types import SimpleNamespace

import pytest
from reverse_conversion_support import FakeSource, population
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.initiatives.i7.reverse_conversion import snapshot
from app.initiatives.i7.reverse_conversion.snapshot import (
    find_stored,
    latest_request_briefs,
    latest_run,
    load_latest_report,
    run_reverse_conversion,
)
from app.initiatives.i7.reverse_conversion.types import ReverseCategory as Cat
from app.models.base import Base
from app.models.i7_reverse import (
    RUN_FAILED,
    RUN_SKIPPED,
    RUN_SUCCEEDED,
    ReverseApprovalLedger,
    ReverseConversionRequest,
    ReverseRecommendation,
    ReverseRun,
)

TABLES = [ReverseRun.__table__, ReverseRecommendation.__table__, ReverseConversionRequest.__table__, ReverseApprovalLedger.__table__]


@pytest.fixture
def factory():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=TABLES)
    return sessionmaker(bind=engine)


@pytest.fixture
def pinned(monkeypatch):
    """The calculation month pinned to 2026-10, as the App Service setting does."""

    def pin(month="2026-10"):
        monkeypatch.setattr(
            snapshot, "get_settings", lambda: SimpleNamespace(i7_reverse_anchor_month=month, i7_reverse_anchor_source="configured", i7_rationale_concurrency=2)
        )

    pin()
    return pin


def source(session):
    return FakeSource(*population())


def stage(factory, **kw):
    return run_reverse_conversion(session_factory=factory, source_factory=source, **kw)


# --- The stage stores a run -------------------------------------------------------------------------


def test_the_stage_stores_a_succeeded_run_with_every_ranked_row(factory, pinned):
    outcome = stage(factory)
    assert outcome.status == "succeeded" and outcome.recommendations_written == 18
    with factory() as session:
        run = latest_run(session)
        assert (run.status, run.rows_written, run.source_population, run.excluded) == (RUN_SUCCEEDED, 18, 19, 1)
        assert f"{run.anchor_month:%Y-%m}" == "2026-10" and run.policy_version == "reverse-conversion-v2"
        assert run.finished_at is not None
        rows = list(session.scalars(select(ReverseRecommendation).order_by(ReverseRecommendation.sort_order)))
        assert len(rows) == 18 and [r.sort_order for r in rows] == list(range(18))


def test_a_stored_run_reads_back_as_the_same_report(factory, pinned):
    stage(factory)
    with factory() as session:
        stored = load_latest_report(session)
    from app.initiatives.i7.reverse_conversion.metrics import resolve_anchor
    from app.initiatives.i7.reverse_conversion.service import build_report
    from app.initiatives.i7.policy.reverse_conversion import current_reverse_conversion_policy
    from datetime import date

    built = build_report(
        FakeSource(*population()),
        current_reverse_conversion_policy(),
        resolve_anchor(latest_staged_month=date(2026, 11, 1), data_loaded_at=None, configured_anchor=date(2026, 10, 1)),
    )
    assert [(r.material, r.plant, r.category, r.reason, r.confidence, r.detail, r.points, r.stock_value) for r in stored.rows] == [
        (r.material, r.plant, r.category, r.reason, r.confidence, r.detail, r.points, r.stock_value) for r in built.rows
    ]
    assert {c: t.count for c, t in stored.totals_by_category.items()} == {c: t.count for c, t in built.totals_by_category.items()}
    assert stored.count_by_confidence == built.count_by_confidence
    assert stored.count_by_category_reason == built.count_by_category_reason
    assert stored.reconciliation == built.reconciliation  # population, excluded, per plant: all rebuilt from the run
    assert stored.reconciliation.reconciled and stored.reconciliation.excluded == 1
    assert stored.run_id is not None and stored.generated_at is not None


def test_the_metrics_a_row_shows_survive_the_round_trip(factory, pinned):
    stage(factory)
    with factory() as session:
        row = {r.material: r for r in load_latest_report(session).rows}["REC1"]
    m = row.metrics
    assert (m.issues_latest_window, m.months_with_use_latest_window) == (2, 2)
    assert m.last_valid_month is not None and m.months_since_last_movement is not None


# --- It never holds back the pipeline ---------------------------------------------------------------


def test_with_no_calculation_month_the_stage_is_skipped_and_still_reports_success(factory, pinned):
    pinned(month=None)
    outcome = stage(factory)
    assert outcome.status == "succeeded" and outcome.recommendations_written == 0  # the pipeline carries on
    with factory() as session:
        attempt = snapshot.latest_attempt(session)
        assert attempt.status == RUN_SKIPPED and "Configure the anchor" in attempt.note
        assert latest_run(session) is None and load_latest_report(session) is None
        assert session.scalars(select(ReverseRecommendation)).first() is None


def test_a_month_that_is_not_loaded_yet_is_skipped_not_guessed(factory, pinned):
    pinned(month="2027-03")
    outcome = stage(factory)
    assert outcome.status == "succeeded"
    with factory() as session:
        assert snapshot.latest_attempt(session).status == RUN_SKIPPED


def test_an_error_inside_the_stage_is_recorded_and_the_pipeline_is_not_failed(factory, pinned):
    def broken(session):
        raise RuntimeError("the database went away")

    outcome = run_reverse_conversion(session_factory=factory, source_factory=broken)
    assert outcome.status == "succeeded"  # never fails the pipeline
    with factory() as session:
        attempt = snapshot.latest_attempt(session)
        assert attempt.status == RUN_FAILED and "RuntimeError" in attempt.note and "the database went away" in attempt.note


def test_a_skipped_run_leaves_the_last_good_snapshot_in_use(factory, pinned):
    stage(factory)
    pinned(month=None)
    stage(factory)
    with factory() as session:
        assert snapshot.latest_attempt(session).status == RUN_SKIPPED
        report = load_latest_report(session)
    assert report is not None and len(report.rows) == 18


# --- Keeping it small -------------------------------------------------------------------------------


def test_only_the_newest_two_runs_keep_their_rows(factory, pinned):
    for _ in range(3):
        stage(factory)
    with factory() as session:
        runs = list(session.scalars(select(ReverseRun).order_by(ReverseRun.id)))
        with_rows = {r.run_id for r in session.scalars(select(ReverseRecommendation))}
    assert [r.status for r in runs] == [RUN_SUCCEEDED] * 3  # the history of runs stays
    assert with_rows == {runs[1].id, runs[2].id}  # the oldest run's rows are gone


# --- Looking one up ---------------------------------------------------------------------------------


def test_one_stored_row_can_be_found_with_its_run(factory, pinned):
    stage(factory)
    with factory() as session:
        run, stored = find_stored(session, "REC1", "1300")
        assert stored.category == Cat.RECOMMENDED_FOR_OAR.value and stored.run_id == run.id
        assert find_stored(session, "NOPE", "1300") is None


def test_the_newest_request_of_each_material_plant_is_found_in_plain_sql(factory, pinned):
    with factory() as session:
        for status, role in (("REJECTED", None), ("PENDING_APPROVAL", "Inventory Controller")):
            session.add(
                ReverseConversionRequest(
                    sap_material_number="M1", sap_plant_code="1300", status=status, pending_role=role, reason="x", detail="y", submitted_by="a"
                )
            )
        session.commit()
        briefs = latest_request_briefs(session, [("M1", "1300"), ("M2", "1300")])
    assert briefs == {("M1", "1300"): (2, "PENDING_APPROVAL", "Inventory Controller")}


# --- Who wrote each reason is stored with it ---------------------------------------------------------


def test_a_reason_written_by_the_model_is_stored_and_read_back_as_ai_and_the_rest_as_standard_wording(factory, pinned, monkeypatch):
    from app.core.ai import Completion
    from app.initiatives.i7.reverse_conversion import rationale

    monkeypatch.setattr(
        rationale,
        "complete_with_prompt",
        lambda prompt_id, **values: Completion(
            text="It was used rarely, so ordering only when needed suits it.",
            model="gpt-fake",
            provider="foundry",
        ),
    )
    stage(factory)
    with factory() as session:
        report = load_latest_report(session)
    recommended = [r for r in report.rows if r.category is Cat.RECOMMENDED_FOR_OAR]
    others = [r for r in report.rows if r.category is not Cat.RECOMMENDED_FOR_OAR]
    assert recommended and others
    assert {r.points_source for r in recommended} == {"AI_GENERATED"}
    assert all(r.detail == "It was used rarely, so ordering only when needed suits it." and r.points == () for r in recommended)
    assert {r.points_source for r in others} == {"DETERMINISTIC_FALLBACK"}


def test_with_no_model_every_reason_is_stored_as_the_standard_wording(factory, pinned):
    stage(factory)
    with factory() as session:
        report = load_latest_report(session)
    assert {r.points_source for r in report.rows} == {"DETERMINISTIC_FALLBACK"}
