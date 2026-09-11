"""Statistics-independent aging: bands, trailing-12-month window, reversals."""

from datetime import date, timedelta
from decimal import Decimal

from app.initiatives.i13.aging import compute_aging, months_before
from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.models import AgingBand

AS_OF = date(2026, 1, 1)
THRESHOLDS = AgingThresholds(fast_max_days=365, slow_max_days=730)


def _movement(bwart: str, menge: str, days_ago: int) -> dict:
    return {
        "Bwart": bwart,
        "Menge": Decimal(menge),
        "Matnr": "MAT1",
        "Werks": "1000",
        "BudatMkpf": AS_OF - timedelta(days=days_ago),
    }


def _aging(movements: list[dict], current_stock: Decimal | None = Decimal("10")):
    return compute_aging(
        "MAT1",
        "1000",
        movements,
        current_stock=current_stock,
        thresholds=THRESHOLDS,
        window_months=12,
        as_of=AS_OF,
    )


def test_aging_band_at_365_days_is_fast() -> None:
    result = _aging([_movement("261", "1", 365)])
    assert result.days_since_last_movement == 365
    assert result.aging_band is AgingBand.FAST


def test_aging_band_at_366_days_is_slow() -> None:
    result = _aging([_movement("261", "1", 366)])
    assert result.aging_band is AgingBand.SLOW


def test_aging_band_at_730_days_is_slow() -> None:
    result = _aging([_movement("261", "1", 730)])
    assert result.aging_band is AgingBand.SLOW


def test_aging_band_at_731_days_is_non_moving() -> None:
    result = _aging([_movement("261", "1", 731)])
    assert result.aging_band is AgingBand.NON_MOVING


def test_aging_band_with_no_movement_is_non_moving() -> None:
    result = _aging([])
    assert result.aging_band is AgingBand.NON_MOVING
    assert result.last_movement_date is None
    assert result.days_since_last_movement is None


def test_consumption_within_trailing_window_is_counted() -> None:
    result = _aging([_movement("261", "5", 30), _movement("261", "5", 300)])
    assert result.consumption_count_12m == 2
    assert result.consumed_qty_12m == Decimal("10")


def test_consumption_outside_trailing_window_is_excluded() -> None:
    result = _aging([_movement("261", "5", 400)])
    assert result.consumption_count_12m == 0
    assert result.consumed_qty_12m == Decimal("0")


def test_consumption_reversal_within_window_is_netted() -> None:
    result = _aging([_movement("261", "5", 30), _movement("262", "5", 20)])
    assert result.consumption_count_12m == 0
    assert result.consumed_qty_12m == Decimal("0")


def test_inventory_turns_insufficient_history_when_no_stock() -> None:
    result = _aging([_movement("261", "5", 30)], current_stock=None)
    assert result.inventory_turns is None
    assert result.inventory_turns_reason == "INSUFFICIENT_HISTORY"


def test_inventory_turns_computed_when_stock_available() -> None:
    result = _aging([_movement("261", "10", 30)], current_stock=Decimal("5"))
    assert result.inventory_turns == Decimal("2")
    assert result.inventory_turns_reason is None


def test_months_before_handles_year_rollover() -> None:
    assert months_before(date(2026, 1, 15), 12) == date(2025, 1, 15)
    assert months_before(date(2026, 3, 31), 1) == date(2026, 2, 28)
