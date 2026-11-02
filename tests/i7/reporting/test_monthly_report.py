"""A month's report: which periods exist, that one is generated the night after the month ends, and that it travels the
same routes as a quarter's.

A monthly report is the existing report over one month, saved in the same table under a label such as ``"Oct 2026"``
(which can never collide with ``"Q4 2026"``). Offline and isolated, like the quarterly tests: in-memory SQLite, the real
generator and repository, injected clocks, a fake lock. Nothing here reaches the shared database or the system clock.
"""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.i7 import reports as reports_api
from app.initiatives.i7 import pipeline_scheduler as scheduler
from app.initiatives.i7.pipeline_scheduler import InvalidScheduleError, SchedulePolicy, monthly_report_step, tick
from app.initiatives.i7.reporting import auto_generate
from app.initiatives.i7.reporting.auto_generate import (
    DUE, FAILED, GENERATED, OUTSIDE_WINDOW, UP_TO_DATE, assess, generate_due_quarterly_report,
)
from app.initiatives.i7.reporting.period import (
    MONTH, QUARTER, latest_closed_month, latest_closed_period, period_kind, resolve_period,
)
from app.initiatives.i7.reporting.repository import save_report
from app.initiatives.i7.reporting.service import generate_quarterly_report as real_generate
from app.main import app
from app.models.i7_reporting import QuarterlyReportRecord
from app.schemas.i7.reports import PeriodStatus, QuarterlyReport
from tests.i7.reporting.quarterly_fixtures import UTC, NoLock, make_factory, seed_q4_2026, stub_stockout

NOV_1 = datetime(2026, 11, 1, 20, 5, tzinfo=UTC)  # the first nightly slot after October ends
JAN_1 = datetime(2027, 1, 1, 20, 5, tzinfo=UTC)  # December and Q4 2026 both end


@pytest.fixture(autouse=True)
def _stockout(monkeypatch):
    stub_stockout(monkeypatch)


@pytest.fixture
def factory():
    f = make_factory()
    with f() as session:
        seed_q4_2026(session)
    return f


def rows(factory) -> list[QuarterlyReportRecord]:
    with factory() as session:
        return list(session.execute(select(QuarterlyReportRecord)).scalars())


def generate_month(factory, now=NOV_1, **kwargs):
    return generate_due_quarterly_report(factory, now, kind="month", lock=NoLock(), **kwargs)


# --- the periods --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label,start,end",
    [
        ("Oct 2026", date(2026, 10, 1), date(2026, 10, 31)),
        ("Dec 2026", date(2026, 12, 1), date(2026, 12, 31)),
        ("Feb 2027", date(2027, 2, 1), date(2027, 2, 28)),
        ("Feb 2028", date(2028, 2, 1), date(2028, 2, 29)),  # a leap year
        ("Q4 2026", date(2026, 10, 1), date(2026, 12, 31)),  # a quarter still resolves as before
    ],
)
def test_a_period_label_resolves_to_its_first_and_last_day(label, start, end):
    assert resolve_period(label) == (start, end)


@pytest.mark.parametrize("bad", ["oct 2026", "October 2026", "Foo 2026", "Oct2026", "Oct 26", "Q5 2026", "2026-10", ""])
def test_anything_that_is_neither_a_quarter_nor_a_month_is_refused(bad):
    with pytest.raises(ValueError):
        resolve_period(bad)
    with pytest.raises(ValueError):
        period_kind(bad)


def test_a_label_says_which_kind_of_period_it_is():
    assert period_kind("Q4 2026") == QUARTER
    assert period_kind("Oct 2026") == MONTH


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 11, 1), "Oct 2026"),  # the day after October ends
        (date(2026, 10, 31), "Sep 2026"),  # October is still running on its last day
        (date(2026, 10, 1), "Sep 2026"),
        (date(2027, 1, 1), "Dec 2026"),  # the year wraps
        (date(2027, 3, 1), "Feb 2027"),
        (date(2028, 3, 1), "Feb 2028"),
    ],
)
def test_the_latest_closed_month_is_always_the_previous_one(today, expected):
    assert latest_closed_month(today) == expected
    assert latest_closed_period(MONTH, today) == expected


def test_the_latest_closed_period_follows_the_kind_asked_for():
    assert latest_closed_period(QUARTER, date(2027, 1, 1)) == "Q4 2026"
    with pytest.raises(ValueError):
        latest_closed_period("week", date(2027, 1, 1))


# --- the automatic step, for a month ------------------------------------------------------------


def test_a_month_is_due_the_night_after_it_ends(factory):
    with factory() as session:
        decision = assess(session, NOV_1, 7, "month")
    assert (decision.quarter, decision.state) == ("Oct 2026", DUE)
    assert decision.reason == "no report exists for this month"


@pytest.mark.parametrize(
    "now,target,state",
    [
        (datetime(2026, 10, 31, 20, 5, tzinfo=UTC), "Sep 2026", OUTSIDE_WINDOW),  # October is still running
        (datetime(2026, 11, 7, 20, 5, tzinfo=UTC), "Oct 2026", DUE),  # the last day of the 7-day window
        (datetime(2026, 11, 8, 20, 5, tzinfo=UTC), "Oct 2026", OUTSIDE_WINDOW),  # the window has passed
    ],
)
def test_a_month_is_only_picked_up_in_the_days_after_it_ends(factory, now, target, state):
    with factory() as session:
        decision = assess(session, now, 7, "month")
    assert (decision.quarter, decision.state) == (target, state)


def test_the_months_report_is_generated_once_and_is_a_report_of_that_month(factory):
    first = generate_month(factory)
    assert (first.quarter, first.action) == ("Oct 2026", GENERATED)

    [row] = rows(factory)
    assert (row.quarter, row.status, row.period_start, row.period_end) == (
        "Oct 2026", "COMPLETED", date(2026, 10, 1), date(2026, 10, 31),
    )
    report = QuarterlyReport.from_report_json(row.report_json)
    assert report.metadata.quarter == "Oct 2026"
    assert report.management.period.status == PeriodStatus.COMPLETE
    assert [m.label for m in report.management.period.months] == ["Oct 2026"]  # one month, not three
    assert report.recommendations.total == 3  # only October's records, not the quarter's seven

    second = generate_month(factory)  # a retry, a restart or a second instance: nothing more is written
    assert second.action == UP_TO_DATE
    assert len(rows(factory)) == 1


def test_a_mid_month_report_made_by_hand_is_replaced_by_the_final_one(factory):
    with factory() as session:
        partial = real_generate(
            session, "Oct 2026", today=date(2026, 10, 20), generated_at=datetime(2026, 10, 20, 9, tzinfo=UTC)
        )
        save_report(session, "Oct 2026", partial, status="COMPLETED")
        session.commit()

    result = generate_month(factory)
    assert result.action == GENERATED
    assert "before the month closed" in result.detail
    [row] = rows(factory)
    assert QuarterlyReport.from_report_json(row.report_json).management.period.status == PeriodStatus.COMPLETE


def test_on_the_first_of_january_the_month_and_the_quarter_are_both_generated_side_by_side(factory):
    month = generate_month(factory, JAN_1)
    quarter = generate_due_quarterly_report(factory, JAN_1, lock=NoLock())
    assert (month.quarter, month.action) == ("Dec 2026", GENERATED)
    assert (quarter.quarter, quarter.action) == ("Q4 2026", GENERATED)
    assert sorted(r.quarter for r in rows(factory)) == ["Dec 2026", "Q4 2026"]


def test_a_month_and_a_quarter_do_not_wait_on_each_others_lock():
    assert auto_generate.LOCK_NAMES[MONTH] != auto_generate.LOCK_NAMES[QUARTER]


def test_a_failed_month_writes_nothing_and_says_so(factory):
    def boom(*args, **kwargs):
        raise RuntimeError("database blip")

    result = generate_month(factory, generate=boom)
    assert result.action == FAILED
    assert rows(factory) == []


# --- the nightly watcher ------------------------------------------------------------------------


def settings(**overrides):
    base = dict(
        i7_pipeline_watch_enabled=True, i7_pipeline_nightly_hour_utc=20, i7_pipeline_retries=2,
        i7_pipeline_check_interval_seconds=300, i7_pipeline_initial_delay_seconds=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


MONTHLY_ON = SchedulePolicy(20, 2, 300, 0, monthly_report_enabled=True)


def runner(reason: str) -> str:
    return "ran"


def locked_month(session_factory, now, *, window_days):
    return generate_due_quarterly_report(session_factory, now, window_days=window_days, kind="month", lock=NoLock())


def test_the_monthly_switch_defaults_to_off_and_a_week():
    from app.core.config import Settings

    s = Settings(_env_file=None)
    assert (s.i7_monthly_report_auto_enabled, s.i7_monthly_report_auto_window_days) == (False, 7)
    policy = SchedulePolicy.from_settings(settings())
    assert (policy.monthly_report_enabled, policy.monthly_report_window_days) == (False, 7)


def test_the_policy_reads_the_monthly_settings_apart_from_the_quarterly_ones():
    on = SchedulePolicy.from_settings(settings(i7_monthly_report_auto_enabled=True, i7_monthly_report_auto_window_days=10))
    assert (on.monthly_report_enabled, on.monthly_report_window_days) == (True, 10)
    assert on.quarterly_report_enabled is False  # one switch does not turn on the other


@pytest.mark.parametrize("window", [0, -1, 32])
def test_a_bad_monthly_window_is_refused_only_when_the_step_is_on(window):
    with pytest.raises(InvalidScheduleError, match="I7_MONTHLY_REPORT_AUTO_WINDOW_DAYS"):
        SchedulePolicy.from_settings(settings(i7_monthly_report_auto_enabled=True, i7_monthly_report_auto_window_days=window))
    assert SchedulePolicy.from_settings(settings(i7_monthly_report_auto_window_days=window)).monthly_report_enabled is False


def test_the_monthly_step_is_only_handed_to_the_thread_when_enabled(factory, monkeypatch):
    import threading

    captured = {}

    class FakeThread:
        def __init__(self, target, args, kwargs, name, daemon):
            captured.update(kwargs)

        def start(self):
            pass

    monkeypatch.setattr("app.core.db.get_sessionmaker", lambda: factory)
    monkeypatch.setattr(scheduler.threading, "Thread", FakeThread)

    scheduler.start(threading.Event(), settings())
    assert captured["monthly"] is None
    scheduler.start(threading.Event(), settings(i7_monthly_report_auto_enabled=True))
    assert callable(captured["monthly"])


def test_the_monthly_step_runs_after_the_snapshots_and_before_the_quarterly_one(factory):
    order = []
    policy = SchedulePolicy(
        20, 2, 300, 0, snapshots_enabled=True, quarterly_report_enabled=True, monthly_report_enabled=True
    )
    out = tick(
        policy, session_factory=factory, runner=runner, now=NOV_1,
        capture=lambda sf, now: order.append("snapshots") or "captured",
        monthly=lambda sf, now: order.append("monthly") or "m",
        report=lambda sf, now: order.append("quarterly") or "q",
    )
    assert order == ["snapshots", "monthly", "quarterly"]  # the month's snapshot is final before its report reads it
    assert out.endswith("snapshots: captured; monthly report: m; quarterly report: q")


def test_the_monthly_step_does_not_run_when_the_policy_is_off_or_outside_a_slot(factory):
    calls = []
    monthly = lambda sf, now: calls.append(1) or "x"  # noqa: E731
    tick(SchedulePolicy(20, 2, 300, 0), session_factory=factory, runner=runner, now=NOV_1, monthly=monthly)
    tick(MONTHLY_ON, session_factory=factory, runner=runner, now=datetime(2026, 11, 1, 12, 0, tzinfo=UTC), monthly=monthly)
    assert calls == []


def test_a_failing_monthly_step_does_not_stop_the_quarterly_one(factory):
    def monthly(sf, now):
        raise RuntimeError("database blip")

    policy = SchedulePolicy(20, 2, 300, 0, quarterly_report_enabled=True, monthly_report_enabled=True)
    out = tick(policy, session_factory=factory, runner=runner, now=NOV_1, monthly=monthly, report=lambda sf, now: "q")
    assert "monthly report failed: RuntimeError" in out
    assert out.endswith("quarterly report: q")


def test_month_end_night_by_night_nothing_until_the_month_closes_then_once(factory):
    step = monthly_report_step(MONTHLY_ON, generate=locked_month)

    last_night_of_october = tick(
        MONTHLY_ON, session_factory=factory, runner=runner, now=datetime(2026, 10, 31, 20, 5, tzinfo=UTC), monthly=step
    )
    assert "Sep 2026: OUTSIDE_WINDOW" in last_night_of_october  # October is still running
    assert rows(factory) == []

    first_night_of_november = tick(MONTHLY_ON, session_factory=factory, runner=runner, now=NOV_1, monthly=step)
    assert "monthly report: Oct 2026: GENERATED" in first_night_of_november
    assert [r.quarter for r in rows(factory)] == ["Oct 2026"]

    next_night = tick(
        MONTHLY_ON, session_factory=factory, runner=runner, now=datetime(2026, 11, 2, 20, 5, tzinfo=UTC), monthly=step
    )
    assert "Oct 2026: UP_TO_DATE" in next_night  # not generated twice
    assert len(rows(factory)) == 1


# --- the API: a month travels the same routes as a quarter --------------------------------------


@pytest.fixture
def client(factory, monkeypatch):
    def session_override():
        with factory() as session:
            yield session

    # "Today" for the route is 1 November 2026: the resolution and the generation both use it.
    today = date(2026, 11, 1)
    monkeypatch.setattr(reports_api, "latest_closed_month", lambda: latest_closed_month(today))
    monkeypatch.setattr(reports_api, "latest_closed_quarter", lambda: "Q3 2026")
    monkeypatch.setattr(
        reports_api, "generate_quarterly_report",
        lambda session, quarter: real_generate(session, quarter, today=today, generated_at=datetime(2026, 11, 1, 20, 5, tzinfo=UTC)),
    )
    app.dependency_overrides[reports_api.get_session] = session_override
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(reports_api.get_session, None)


def test_the_route_generates_the_latest_closed_month_when_asked_for_one(client, factory):
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={"period": "month"})
    assert response.status_code == 200
    assert response.json()["metadata"]["quarter"] == "Oct 2026"
    assert [r.quarter for r in rows(factory)] == ["Oct 2026"]


def test_the_route_still_means_the_latest_closed_quarter_when_asked_nothing(client):
    for body in ({}, {"period": "quarter"}):
        assert client.post("/api/v1/i7/reports/quarterly/generate", json=body).json()["metadata"]["quarter"] == "Q3 2026"


def test_a_named_month_can_be_generated_and_read_back(client, factory):
    assert client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": "Oct 2026"}).status_code == 200
    body = client.get("/api/v1/i7/reports/quarterly/Oct 2026").json()
    assert body["metadata"]["quarter"] == "Oct 2026"
    assert (body["metadata"]["period_start"], body["metadata"]["period_end"]) == ("2026-10-01", "2026-10-31")


def test_the_list_carries_months_and_quarters_together(client):
    for label in ("Oct 2026", "Q4 2026"):
        client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": label})
    items = client.get("/api/v1/i7/reports/quarterly").json()["items"]
    assert sorted(item["quarter"] for item in items) == ["Oct 2026", "Q4 2026"]


def test_an_unknown_period_is_a_bad_request_not_a_server_error(client):
    assert client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": "Foo 2026"}).status_code == 400
    assert client.post("/api/v1/i7/reports/quarterly/generate", json={"period": "week"}).status_code == 422


def test_the_excel_export_is_named_for_the_kind_of_report(client):
    for label in ("Oct 2026", "Q4 2026"):
        client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": label})
    month = client.get("/api/v1/i7/reports/quarterly/Oct 2026/export")
    quarter = client.get("/api/v1/i7/reports/quarterly/Q4 2026/export")
    assert 'filename="i7-monthly-report-Oct_2026.xlsx"' in month.headers["content-disposition"]
    assert 'filename="i7-quarterly-report-Q4_2026.xlsx"' in quarter.headers["content-disposition"]
