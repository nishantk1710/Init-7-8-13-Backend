"""Aging-band classification and calendar-month arithmetic -- the shared
utilities ``movement_metrics.py`` (W3.5), ``watch.py`` and
``reclassification.py`` all reuse from here. Boundary/metric-level behaviour
end-to-end (including consumption windowing, reversal netting and inventory
turns) is covered in ``test_movement_metrics.py``; this file covers just
these two pure functions directly.
"""

from datetime import date

from app.initiatives.i13.aging import classify_aging_band, months_before
from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.models import AgingBand

THRESHOLDS = AgingThresholds(fast_max_days=365, slow_max_days=730)


def test_aging_band_at_365_days_is_fast() -> None:
    assert classify_aging_band(365, THRESHOLDS) is AgingBand.FAST


def test_aging_band_at_366_days_is_slow() -> None:
    assert classify_aging_band(366, THRESHOLDS) is AgingBand.SLOW


def test_aging_band_at_730_days_is_slow() -> None:
    assert classify_aging_band(730, THRESHOLDS) is AgingBand.SLOW


def test_aging_band_at_731_days_is_non_moving() -> None:
    assert classify_aging_band(731, THRESHOLDS) is AgingBand.NON_MOVING


def test_aging_band_with_no_days_is_non_moving() -> None:
    assert classify_aging_band(None, THRESHOLDS) is AgingBand.NON_MOVING


def test_months_before_handles_year_rollover() -> None:
    assert months_before(date(2026, 1, 15), 12) == date(2025, 1, 15)
    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
