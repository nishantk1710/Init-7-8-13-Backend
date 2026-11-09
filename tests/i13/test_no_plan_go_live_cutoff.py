"""Non-AI is raised only for reservations made since the assistant's go-live.

Before 6-Nov-2026 every OAR reservation without a plan could raise one --
~31,900 on the live extract, almost all reserved years before the assistant
existed. That is why NO_PLAN was left out of ``I13_DETECTION_TYPES``, and why
the capture-time run (which ignored that setting) raised them material by
material anyway. Now:

* only a reservation made on or after ``I13_ASSISTANT_GO_LIVE_DATE`` can raise
  Non-AI (RESB-CRDAT; its requirement date where SAP sends no creation date);
* while that date is unset, none is raised at all; and
* a capture and a UAT run check the same types as the nightly run.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.initiatives.i13 import act_runner
from app.initiatives.i13.act.domain import ExceptionStatus, ExceptionType
from app.initiatives.i13.act_runner import no_plan_cutoff, no_plan_eligible
from app.integrations.sap.postgres_reservation import reservation_keys_since
from tests.i13.test_act_service import _detect, _ledger_entry

UTC = timezone.utc
OLD = ("1000000000", "0001")
NEW = ("1000000000", "0002")


def _entries():
    return [_ledger_entry(), _ledger_entry(ledger_id="LEDGER-2", reservation_item="0002")]


# --- the rule ------------------------------------------------------------------


def test_only_a_reservation_made_since_go_live_raises_non_ai():
    repository, _, result = _detect(datetime(2026, 11, 20, tzinfo=UTC), ledger_entries=_entries(), no_plan_eligible={NEW})

    (raised,) = repository.list(exception_type=ExceptionType.NO_PLAN)
    assert (raised.reservation_number, raised.reservation_item) == NEW
    assert result.created == 1


def test_no_cutoff_raises_no_non_ai_at_all():
    repository, _, _ = _detect(datetime(2026, 11, 20, tzinfo=UTC), ledger_entries=_entries(), no_plan_eligible=frozenset())
    assert repository.list(exception_type=ExceptionType.NO_PLAN) == []


def test_an_open_non_ai_on_a_reservation_from_before_go_live_is_resolved():
    """What non-prod held on 6-Nov-2026: 2,109 raised with no cutoff."""
    repository, _, _ = _detect(datetime(2026, 11, 6, tzinfo=UTC), ledger_entries=_entries())
    assert len(repository.list(exception_type=ExceptionType.NO_PLAN)) == 2

    _, _, result = _detect(
        datetime(2026, 11, 20, tzinfo=UTC), ledger_entries=_entries(), repository=repository, no_plan_eligible={NEW}
    )

    assert result.resolved == 1
    assert repository.get("ACT-NO_PLAN-1000000000-0001").status is ExceptionStatus.RESOLVED
    assert repository.get("ACT-NO_PLAN-1000000000-0002").status is not ExceptionStatus.RESOLVED


# --- the cutoff --------------------------------------------------------------------


def test_the_cutoff_is_the_go_live_setting_and_empty_is_none():
    assert no_plan_cutoff("2026-11-15") == date(2026, 11, 15)
    assert no_plan_cutoff("  ") is None
    assert no_plan_cutoff("") is None
    with pytest.raises(ValueError):
        no_plan_cutoff("15-11-2026")


def _settings(monkeypatch, go_live: str):
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: SimpleNamespace(i13_assistant_go_live_date=go_live, i13_uat_simulation_enabled=False),
    )


def test_nothing_is_read_when_the_run_does_not_check_non_ai(monkeypatch):
    _settings(monkeypatch, "2026-11-15")
    monkeypatch.setattr(act_runner, "reservation_keys_since", lambda *a, **k: pytest.fail("read for nothing"))
    assert no_plan_eligible(None, {ExceptionType.PLAN_BREACH}, material=None, plant=None) is None


def test_no_reservation_is_eligible_while_the_go_live_date_is_unset(monkeypatch):
    _settings(monkeypatch, "")
    monkeypatch.setattr(act_runner, "reservation_keys_since", lambda *a, **k: pytest.fail("read for nothing"))
    assert no_plan_eligible(None, None, material=None, plant=None) == frozenset()


def test_the_eligible_reservations_are_those_made_since_the_cutoff(monkeypatch):
    _settings(monkeypatch, "2026-11-15")
    seen = {}

    def keys_since(db, since, *, material, plant):
        seen.update(since=since, material=material, plant=plant)
        return frozenset({NEW})

    monkeypatch.setattr(act_runner, "reservation_keys_since", keys_since)
    assert no_plan_eligible(None, {ExceptionType.NO_PLAN}, material="MAT1", plant="1300") == {NEW}
    assert seen == {"since": date(2026, 11, 15), "material": "MAT1", "plant": "1300"}


# --- which reservations count as made since -------------------------------------


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(i13_uat_simulation_enabled=False)
    )
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE n_resb (reservation TEXT, item_no_stock_transfer_reserv TEXT, material TEXT, "
            "plant TEXT, created_on TEXT, requirement_date TEXT)"
        ))
        connection.execute(text(
            "INSERT INTO n_resb VALUES "
            "('1', '1', 'MAT1', '1300', '2026-11-16', '2026-11-01'),"  # made after: counts
            "('2', '1', 'MAT1', '1300', '2026-10-01', '2026-12-01'),"  # made before, required after: not
            "('3', '1', 'MAT1', '1300', NULL, '2026-11-20'),"          # no creation date, required after: counts
            "('4', '1', 'MAT1', '1300', NULL, '2026-10-20'),"          # no creation date, required before: not
            "('5', '1', 'MAT2', '1500', '2026-11-15', NULL),"          # made on the day: counts
            "('6', '1', 'MAT1', '9999', '2026-11-16', NULL)"           # out-of-scope plant: never
        ))
    with Session(engine) as session:
        yield session


def test_made_means_its_creation_date_else_its_requirement_date(db):
    assert reservation_keys_since(db, date(2026, 11, 15)) == {("1", "1"), ("3", "1"), ("5", "1")}


def test_the_scope_narrows_to_one_material_and_plant(db):
    assert reservation_keys_since(db, date(2026, 11, 15), material="MAT2", plant="1500") == {("5", "1")}


# --- a capture checks the deployment's types --------------------------------------


def test_a_capture_runs_only_the_types_the_deployment_checks(monkeypatch):
    from app.assistant import downstream

    calls = []
    monkeypatch.setattr(
        downstream,
        "get_settings",
        lambda: SimpleNamespace(i13_data_dir=".", i13_detection_types="PLAN_BREACH,QUANTITY_OVERRIDE,NOT_ISSUED_30_DAYS"),
    )
    monkeypatch.setattr("app.initiatives.i13.act_runner.run_detection", lambda *a, **k: calls.append(k))
    monkeypatch.setattr("app.initiatives.i13.snapshot.refresh_key", lambda *a: False)
    monkeypatch.setattr("app.initiatives.i13.snapshot.serving_snapshot", lambda db: None)
    monkeypatch.setattr("app.initiatives.i13.config.get_i13_config", lambda: None)

    class _Db:
        def commit(self):
            pass

        def rollback(self):
            pass

    downstream.apply_capture(_Db(), material="MAT1", plant="1300")

    (kwargs,) = calls
    assert ExceptionType.NO_PLAN not in kwargs["types"]
    assert ExceptionType.PLAN_BREACH in kwargs["types"]

