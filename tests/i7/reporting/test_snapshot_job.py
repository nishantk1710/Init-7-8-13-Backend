"""The capture command and the nightly step that saves the monthly KPI snapshots.

The nightly step rides on the existing pipeline watcher: no second timer. Offline -- a fake pipeline runner and capture
function, and SQLite for the command.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.initiatives.i7 import pipeline_scheduler as scheduler
from app.initiatives.i7.pipeline_scheduler import SchedulePolicy, capture_snapshots, tick
from app.initiatives.i7.reporting import kpi_definitions
from app.initiatives.i7.stockout_risk.counts import StockoutRiskCounts
from app.models.i7_features import MaterialFeature
from app.models.i7_kpi_snapshot import KpiSnapshot, KpiSnapshotLog
from app.models.i7_pipeline import STATUS_RUNNING, PipelineRun
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun
from app.models.i7_stockout_risk import StockoutRiskInput
from app.models.ingestion import IngestionRun
from app.reporting import capture_snapshot

UTC = timezone.utc
NIGHT = datetime(2026, 10, 13, 20, 5, tzinfo=UTC)  # inside the 20:00 slot
DAYTIME = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)  # no slot open
ON = SchedulePolicy(20, 2, 300, 0, snapshots_enabled=True)
OFF = SchedulePolicy(20, 2, 300, 0)

TABLES = (Recommendation, KpiSnapshot, KpiSnapshotLog, StagedMaterial, StagedMaterialPlant, StagedStock, StagingRun, StockoutRiskInput,
          MaterialFeature, PipelineRun, IngestionRun)


@pytest.fixture
def factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    metadata = MetaData()
    for model in TABLES:
        copy = model.__table__.to_metadata(metadata)
        copy.indexes.clear()
    metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture(autouse=True)
def no_usage(monkeypatch):
    monkeypatch.setattr(
        kpi_definitions, "stockout_risk_counts",
        lambda session, base, **kw: StockoutRiskCounts(at_risk=0, evaluated=0, insufficient_data=0, as_of=None),
    )


def runner(reason: str) -> str:
    return "ran"


# --- the policy ------------------------------------------------------------------------------


def test_snapshots_are_off_unless_asked_for_and_the_setting_is_read_when_present():
    base = dict(i7_pipeline_nightly_hour_utc=20, i7_pipeline_retries=2, i7_pipeline_check_interval_seconds=300, i7_pipeline_initial_delay_seconds=120)
    assert SchedulePolicy.from_settings(SimpleNamespace(**base)).snapshots_enabled is False
    assert SchedulePolicy.from_settings(SimpleNamespace(**base, i7_kpi_snapshot_enabled=True)).snapshots_enabled is True


def test_the_setting_defaults_to_off():
    from app.core.config import Settings

    assert Settings.model_fields["i7_kpi_snapshot_enabled"].default is False


# --- the nightly step ------------------------------------------------------------------------


def test_snapshots_are_saved_after_the_pipeline_check_inside_an_open_slot(factory):
    seen = []

    def capture(session_factory, now):
        seen.append(now)
        return "2026-10 CREATED"

    out = tick(ON, session_factory=factory, runner=runner, now=NIGHT, capture=capture)
    assert len(seen) == 1
    assert out.endswith("snapshots: 2026-10 CREATED")


def test_nothing_is_captured_outside_a_slot_or_when_the_policy_is_off(factory):
    calls = []
    capture = lambda session_factory, now: calls.append(1) or "x"  # noqa: E731
    assert tick(ON, session_factory=factory, runner=runner, now=DAYTIME, capture=capture) == "no slot open"
    tick(OFF, session_factory=factory, runner=runner, now=NIGHT, capture=capture)
    tick(ON, session_factory=factory, runner=runner, now=NIGHT)  # no capture function given
    assert calls == []


def test_a_failed_capture_never_ends_the_tick_or_hides_the_pipeline_outcome(factory):
    def capture(session_factory, now):
        raise RuntimeError("database blip")

    out = tick(ON, session_factory=factory, runner=runner, now=NIGHT, capture=capture)
    assert "snapshots failed: RuntimeError" in out


def test_the_step_waits_while_a_pipeline_run_is_in_progress(factory):
    with factory() as session:
        session.add(PipelineRun(status=STATUS_RUNNING, trigger_reason="manual", started_at=NIGHT, source_fingerprint="f"))
        session.commit()
    assert "in progress" in capture_snapshots(factory, NIGHT)
    with factory() as session:
        assert session.execute(select(KpiSnapshot)).scalars().all() == []  # nothing was read mid-run


def test_the_step_captures_this_month_and_last_and_is_then_idempotent(factory):
    with factory() as session:
        session.add(Recommendation(
            recommendation_id="REC-A", sap_material_number="A", sap_plant_code="1300", feature_run_id=1, forecast_run_id=1,
            inventory_run_id=1, oar_run_id=1, policy_id="p", policy_version=1, formula_version="f", status="READY_FOR_REVIEW",
            impact_status="X", generated_at=datetime(2026, 9, 20, tzinfo=UTC),
        ))
        session.commit()
    first = capture_snapshots(factory, NIGHT)
    assert "2026-09 CREATED" in first and "2026-10 CREATED" in first
    assert capture_snapshots(factory, NIGHT) == "up to date"


# --- the command -----------------------------------------------------------------------------


def test_the_command_captures_a_named_month_and_exits_zero(factory, monkeypatch, capsys):
    monkeypatch.setattr(capture_snapshot, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(capture_snapshot, "configure_logging", lambda settings: None)
    today = datetime.now(UTC).date()
    assert capture_snapshot.main(["--month", f"{today:%Y-%m}"]) == 0
    assert "CREATED" in capsys.readouterr().out
    assert capture_snapshot.main(["--month", f"{today:%Y-%m}"]) == 0
    assert "UNCHANGED" in capsys.readouterr().out  # a second run changes nothing


def test_the_command_exits_one_on_a_failed_capture_and_the_failure_is_logged(factory, monkeypatch, capsys):
    monkeypatch.setattr(capture_snapshot, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(capture_snapshot, "configure_logging", lambda settings: None)
    monkeypatch.setattr(capture_snapshot.snapshots, "compute", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    today = datetime.now(UTC).date()
    assert capture_snapshot.main(["--month", f"{today:%Y-%m}"]) == 1
    assert "run again to retry" in capsys.readouterr().err
    with factory() as session:
        assert [row.action for row in session.execute(select(KpiSnapshotLog)).scalars()] == ["FAILED"]


def test_a_month_in_the_future_is_a_usage_error(factory, monkeypatch, capsys):
    monkeypatch.setattr(capture_snapshot, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(capture_snapshot, "configure_logging", lambda settings: None)
    future = datetime.now(UTC).date() + timedelta(days=62)
    assert capture_snapshot.main(["--month", f"{future:%Y-%m}"]) == 2
    assert "has not started" in capsys.readouterr().err


def test_a_malformed_month_is_refused_by_the_parser():
    with pytest.raises(SystemExit) as caught:
        capture_snapshot.main(["--month", "October"])
    assert caught.value.code == 2
