"""W6.3 §7: I13 config threshold validation -- fast/slow aging bounds and the
WATCH window/threshold must reject nonsensical values rather than silently
misclassify or divide by a bad denominator later.
"""

import pytest

from app.initiatives.i13.config import AgingThresholds, WatchConfig


def test_fast_max_days_must_be_less_than_slow_max_days() -> None:
    with pytest.raises(ValueError):
        AgingThresholds(fast_max_days=730, slow_max_days=365)


def test_fast_max_days_equal_to_slow_max_days_is_invalid() -> None:
    with pytest.raises(ValueError):
        AgingThresholds(fast_max_days=365, slow_max_days=365)


def test_aging_thresholds_must_be_positive() -> None:
    with pytest.raises(ValueError):
        AgingThresholds(fast_max_days=0, slow_max_days=730)


def test_valid_aging_thresholds_construct_cleanly() -> None:
    thresholds = AgingThresholds(fast_max_days=365, slow_max_days=730)
    assert thresholds.fast_max_days == 365
    assert thresholds.slow_max_days == 730


def test_consumption_window_months_must_be_positive() -> None:
    with pytest.raises(ValueError):
        WatchConfig(consumption_window_months=0, gr_not_issued_threshold_days=30)


def test_gr_not_issued_threshold_days_must_be_positive() -> None:
    with pytest.raises(ValueError):
        WatchConfig(consumption_window_months=12, gr_not_issued_threshold_days=0)
