"""Forecasting says how far it has got, without changing what it computes."""

from __future__ import annotations

import logging
import time

from app.initiatives.i7.forecasting import service


def _line(caplog) -> str:
    [record] = [r.getMessage() for r in caplog.records]
    return record


def test_a_progress_line_carries_counts_elapsed_percent_and_eta(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=service.logger.name):
        service.log_progress(123, 1500, 6000, time.monotonic() - 420)
    line = _line(caplog)
    assert line.startswith("I07 forecasting progress: run_id=123 completed=1500 total=6000 elapsed_seconds=")
    assert "percent=25.0" in line and line.endswith("saved=yes")
    # 1,500 done in 420 s, 4,500 to go -> 1,260 s.
    eta = int(line.split("eta_seconds=")[1].split()[0])
    assert 1255 <= eta <= 1265


def test_the_eta_ignores_material_plants_an_earlier_attempt_already_saved(caplog) -> None:
    """3,000 were saved before this call; it did 1,000 in 100 s, 2,000 to go -> 200 s.
    Counting the 3,000 as this call's work would promise 25 s."""
    with caplog.at_level(logging.INFO, logger=service.logger.name):
        service.log_progress(5, 4000, 6000, time.monotonic() - 100, skipped=3000)
    eta = int(_line(caplog).split("eta_seconds=")[1].split()[0])
    assert 195 <= eta <= 205


def test_the_eta_is_unknown_until_this_call_has_done_some_work(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=service.logger.name):
        service.log_progress(5, 3000, 6000, time.monotonic() - 5, skipped=3000)
    assert "eta_seconds=unknown" in _line(caplog)


def test_an_empty_run_does_not_divide_by_zero(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=service.logger.name):
        service.log_progress(5, 0, 0, time.monotonic())
    assert "percent=100.0" in _line(caplog)


def test_saves_and_progress_lines_are_frequent_enough_to_tell_working_from_stuck() -> None:
    assert service.CHECKPOINT_EVERY == 50
    assert service.CHECKPOINT_SECONDS == 60  # a slow model still logs at least this often
