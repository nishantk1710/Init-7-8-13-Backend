"""The pipeline stage that rebuilds the High Stockout Risk usage table.

The card reads ``i7_stockout_risk_input``, which used to be rebuilt only by hand, so it went stale while the
other cards moved with every pipeline run. The stage runs that rebuild at the end of the pipeline. These tests
pin what it reports, with the database and the rebuild itself replaced by stand-ins.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.initiatives.i7.stockout_risk import stage as stage_module
from app.models.i7_pipeline import STATUS_FAILED, STATUS_SUCCEEDED


class _Session:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


@pytest.fixture
def session(monkeypatch):
    held = _Session()
    monkeypatch.setattr(stage_module, "get_sessionmaker", lambda: (lambda: held))
    return held


def test_a_rebuild_is_committed_and_reports_the_date_and_the_rows(monkeypatch, session):
    monkeypatch.setattr(stage_module, "build_stockout_risk_input", lambda s: (date(2026, 11, 22), 12345))

    result = stage_module.rebuild_stockout_risk_input()

    assert result.status == STATUS_SUCCEEDED and result.error is None
    assert (result.as_of, result.rows_written) == (date(2026, 11, 22), 12345)
    assert session.committed and not session.rolled_back


def test_no_issue_movements_is_a_success_with_an_empty_table_not_a_failure(monkeypatch, session):
    monkeypatch.setattr(stage_module, "build_stockout_risk_input", lambda s: (None, 0))

    result = stage_module.rebuild_stockout_risk_input()

    assert result.status == STATUS_SUCCEEDED
    assert result.as_of is None and result.rows_written == 0


def test_a_failed_rebuild_is_rolled_back_and_returned_never_raised(monkeypatch, session):
    def boom(s):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(stage_module, "build_stockout_risk_input", boom)

    result = stage_module.rebuild_stockout_risk_input()

    assert result.status == STATUS_FAILED
    assert result.error == "RuntimeError: the database went away"
    assert session.rolled_back and not session.committed


def test_the_stage_rebuilds_with_the_configured_anchor_date(monkeypatch, session):
    """It passes no date, so the rebuild uses I7_STOCKOUT_RISK_AS_OF: a fixed date stays fixed, and blank follows
    the newest issue date in the data on every run."""
    seen = {}

    def spy(s, **kwargs):
        seen.update(kwargs)
        return date(2026, 11, 22), 1

    monkeypatch.setattr(stage_module, "build_stockout_risk_input", spy)

    stage_module.rebuild_stockout_risk_input()

    assert "as_of" not in seen
