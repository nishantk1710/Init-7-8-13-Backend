"""The quarterly report API and CLI -- the path an external trigger (Logic App / WebJob) takes -- against an isolated database.

``test_reports_api.py`` and ``test_generate_quarterly_cli.py`` cover the same routes against the real database and are
skipped without one; they also *delete the saved report for the real latest closed quarter* when they finish. These run
the same requests offline: an in-memory SQLite database per test, the application's real routes and the real
generator and repository, and an injected clock -- so the "empty body picks the latest closed quarter" behaviour can be
checked for 27 October 2026 and 1 January 2027 without the system clock moving and without touching shared data.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.i7 import reports as reports_api
from app.initiatives.i7.reporting.period import latest_closed_quarter
from app.initiatives.i7.reporting.repository import save_report as real_save_report
from app.initiatives.i7.reporting.service import generate_quarterly_report as real_generate
from app.main import app
from app.models.i7_reporting import QuarterlyReportRecord
from app.reporting import generate_quarterly as cli
from tests.i7.reporting.quarterly_fixtures import UTC, make_factory, seed_q4_2026, stub_stockout

OCT_27, JAN_1 = date(2026, 10, 27), date(2027, 1, 1)


@pytest.fixture(autouse=True)
def _stockout(monkeypatch):
    stub_stockout(monkeypatch)


@pytest.fixture
def factory():
    f = make_factory()
    with f() as session:
        seed_q4_2026(session)
    return f


@pytest.fixture
def client(factory):
    def session_override():
        with factory() as session:
            yield session

    app.dependency_overrides[reports_api.get_session] = session_override
    try:
        # A 500 must come back as a response here, not be re-raised into the test.
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(reports_api.get_session, None)


class Clock:
    """Moves "today" for the API route and the CLI -- the resolution *and* the generation -- without touching the OS clock."""

    def __init__(self, monkeypatch) -> None:
        self._mp = monkeypatch
        self.set(OCT_27)

    def set(self, today: date) -> None:
        now = datetime(today.year, today.month, today.day, 20, 5, tzinfo=UTC)
        resolve = lambda: latest_closed_quarter(today)  # noqa: E731
        generate = lambda session, quarter: real_generate(session, quarter, today=today, generated_at=now)  # noqa: E731
        for module in (reports_api, cli):
            self._mp.setattr(module, "latest_closed_quarter", resolve)
            self._mp.setattr(module, "generate_quarterly_report", generate)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def saved(factory) -> list[QuarterlyReportRecord]:
    with factory() as session:
        return list(session.execute(select(QuarterlyReportRecord)).scalars())


# --- the endpoint an external trigger calls ---------------------------------------------------


def test_an_empty_body_on_27_october_2026_generates_q3(client, factory, clock):
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={})
    assert response.status_code == 200
    assert response.json()["metadata"]["quarter"] == "Q3 2026"
    assert [r.quarter for r in saved(factory)] == ["Q3 2026"]


def test_an_empty_body_on_1_january_2027_generates_the_completed_q4_and_persists_it(client, factory, clock):
    clock.set(JAN_1)
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["metadata"]["quarter"] == "Q4 2026"
    assert body["management"]["period"]["status"] == "COMPLETE"
    assert body["recommendations"]["total"] == 7  # the synthetic Oct + Nov + Dec rows

    [row] = saved(factory)
    assert (row.quarter, row.status, row.period_end) == ("Q4 2026", "COMPLETED", date(2026, 12, 31))
    assert row.report_json is not None


@pytest.mark.parametrize("payload", [{}, {"quarter": None}])
def test_an_omitted_or_null_quarter_resolve_identically(client, clock, payload):
    clock.set(JAN_1)
    assert client.post("/api/v1/i7/reports/quarterly/generate", json=payload).json()["metadata"]["quarter"] == "Q4 2026"


def test_an_explicit_quarter_still_wins_over_the_clock(client, clock):
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": "Q4 2026"})  # clock says 27 Oct
    assert response.json()["metadata"]["quarter"] == "Q4 2026"
    assert response.json()["management"]["period"]["status"] == "IN_PROGRESS"  # a running quarter reads as one


def test_a_retry_after_a_timeout_overwrites_the_one_row_and_never_adds_a_second(client, factory, clock):
    clock.set(JAN_1)
    for _ in range(3):  # a Logic App retry policy firing three times
        assert client.post("/api/v1/i7/reports/quarterly/generate", json={}).status_code == 200
    rows = saved(factory)
    assert len(rows) == 1 and rows[0].quarter == "Q4 2026"


# --- failure handling -------------------------------------------------------------------------


def test_a_malformed_quarter_is_a_400_and_writes_nothing(client, factory):
    response = client.post("/api/v1/i7/reports/quarterly/generate", json={"quarter": "Q5 2026"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_QUARTER"
    assert saved(factory) == []


def test_a_failure_while_persisting_is_recorded_as_failed_and_surfaces_as_a_500(client, factory, clock, monkeypatch):
    clock.set(JAN_1)

    def save(session, quarter, report, status="COMPLETED", **kwargs):
        if status == "COMPLETED":
            raise RuntimeError("commit failed")
        return real_save_report(session, quarter, report, status=status, **kwargs)

    monkeypatch.setattr(reports_api, "save_report", save)
    assert client.post("/api/v1/i7/reports/quarterly/generate", json={}).status_code == 500

    [row] = saved(factory)
    assert (row.quarter, row.status, row.error) == ("Q4 2026", "FAILED", "commit failed")
    status = client.get("/api/v1/i7/reports/quarterly/Q4 2026/status").json()
    assert (status["status"], status["error"]) == ("FAILED", "commit failed")

    # The same call, once the fault is gone, replaces the FAILED row with the report.
    monkeypatch.setattr(reports_api, "save_report", real_save_report)
    assert client.post("/api/v1/i7/reports/quarterly/generate", json={}).status_code == 200
    [row] = saved(factory)
    assert (row.status, row.error) == ("COMPLETED", None)


# --- what the frontend reads afterwards -------------------------------------------------------


def test_the_saved_report_is_visible_to_the_frontend_through_list_status_detail_and_export(client, clock):
    clock.set(JAN_1)
    assert client.get("/api/v1/i7/reports/quarterly").json() == {"items": [], "total": 0}
    assert client.get("/api/v1/i7/reports/quarterly/Q4 2026").status_code == 404  # "Generate" is what the UI offers

    client.post("/api/v1/i7/reports/quarterly/generate", json={})

    listing = client.get("/api/v1/i7/reports/quarterly").json()
    assert listing["total"] == 1
    [item] = listing["items"]
    # exactly the fields the frontend's ApiQuarterlyReportListItem reads (types/api.ts), and no report body
    assert set(item) == {"report_id", "quarter", "status", "report_version", "generated_at", "period_start", "period_end"}
    assert (item["quarter"], item["status"], item["period_end"]) == ("Q4 2026", "COMPLETED", "2026-12-31")

    status = client.get("/api/v1/i7/reports/quarterly/Q4 2026/status").json()
    assert (status["status"], status["report_id"]) == ("COMPLETED", item["report_id"])

    detail = client.get("/api/v1/i7/reports/quarterly/Q4 2026")
    assert detail.status_code == 200 and detail.json()["metadata"]["quarter"] == "Q4 2026"

    export = client.get("/api/v1/i7/reports/quarterly/Q4 2026/export")
    assert export.status_code == 200 and export.content[:2] == b"PK"  # an .xlsx is a zip


# --- the CLI an external trigger or an operator runs ------------------------------------------


@pytest.fixture
def cli_db(factory, monkeypatch):
    monkeypatch.setattr(cli, "get_sessionmaker", lambda: factory)
    monkeypatch.setattr(cli, "configure_logging", lambda settings: None)
    return factory


def test_the_cli_with_no_quarter_generates_the_latest_closed_one(cli_db, clock, capsys):
    clock.set(JAN_1)
    assert cli.main([]) == 0
    assert "Q4 2026" in capsys.readouterr().out
    assert [r.quarter for r in saved(cli_db)] == ["Q4 2026"]
    assert cli.main([]) == 0  # run again: still one row
    assert len(saved(cli_db)) == 1


def test_the_cli_rejects_a_malformed_quarter_with_exit_2_and_saves_nothing(cli_db, capsys):
    assert cli.main(["--quarter", "2026-Q4"]) == 2
    assert "Invalid --quarter value" in capsys.readouterr().err
    assert saved(cli_db) == []


def test_the_cli_exits_1_when_persisting_fails_and_records_the_failure(cli_db, clock, monkeypatch, capsys):
    clock.set(JAN_1)

    def save(session, quarter, report, status="COMPLETED", **kwargs):
        if status == "COMPLETED":
            raise RuntimeError("commit failed")
        return real_save_report(session, quarter, report, status=status, **kwargs)

    monkeypatch.setattr(cli, "save_report", save)
    assert cli.main([]) == 1  # an external scheduler's retry/alerting acts on this exit code
    assert "Failed to persist report for Q4 2026" in capsys.readouterr().err
    [row] = saved(cli_db)
    assert (row.status, row.error) == ("FAILED", "commit failed")
