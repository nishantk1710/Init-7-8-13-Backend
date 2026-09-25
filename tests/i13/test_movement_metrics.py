"""W3.5: statistics-independent movement/aging metrics.

Pure unit tests against small in-memory fixtures -- no CSV, no database, no
generated production data (see the module docstring in ``movement_metrics.py``
for why this is a separate implementation from ``aging.py``'s CSV-backed one).
Postgres integration/query-shape tests live in
``test_movement_metrics_postgres.py``, skipped when no database is configured.
"""

from datetime import date, timedelta
from decimal import Decimal

from app.initiatives.i13.config import AgingThresholds
from app.initiatives.i13.models import AgingBand
from app.initiatives.i13.movement_metrics import compute_all_movement_metrics, compute_movement_metrics

AS_OF = date(2026, 1, 1)
THRESHOLDS = AgingThresholds(fast_max_days=365, slow_max_days=730)


def _movement(bwart: str, menge: str, days_ago: int, *, material: str = "MAT1", plant: str = "1300") -> dict:
    return {
        "Bwart": bwart,
        "Menge": Decimal(menge),
        "Matnr": material,
        "Werks": plant,
        "BudatMkpf": AS_OF - timedelta(days=days_ago),
    }


def _metrics(movements: list[dict], current_stock: Decimal | None = Decimal("10")):
    return compute_movement_metrics(
        "MAT1", "1300", movements, current_stock=current_stock, thresholds=THRESHOLDS, window_months=12, as_of=AS_OF
    )


# --- last movement / last issue -------------------------------------------


def test_last_movement_date_is_the_latest_of_any_type() -> None:
    result = _metrics([_movement("201", "1", 30), _movement("101", "1", 5)])
    assert result.last_movement_date == AS_OF - timedelta(days=5)


def test_days_since_last_movement_is_computed_against_as_of() -> None:
    result = _metrics([_movement("101", "1", 5)])
    assert result.days_since_last_movement == 5


def test_last_issue_differs_from_last_movement_when_latest_event_is_a_receipt() -> None:
    """01-Aug goods issue, 05-Aug goods receipt -> last_issue_date stays
    01-Aug even though last_movement_date is 05-Aug."""
    result = _metrics([_movement("201", "5", days_ago=10), _movement("101", "5", days_ago=6)])
    assert result.last_movement_date == AS_OF - timedelta(days=6)
    assert result.last_issue_date == AS_OF - timedelta(days=10)
    assert result.days_since_last_movement == 6
    assert result.days_since_last_issue == 10


def test_no_issue_history_leaves_last_issue_date_none_and_is_non_moving() -> None:
    """Receipts only, ever -- must not be misread as FAST because *something*
    (a receipt) happened recently."""
    result = _metrics([_movement("101", "10", days_ago=2)])
    assert result.last_issue_date is None
    assert result.days_since_last_issue is None
    assert result.aging_band is AgingBand.NON_MOVING


def test_no_movement_at_all_is_non_moving() -> None:
    result = _metrics([])
    assert result.last_movement_date is None
    assert result.last_issue_date is None
    assert result.aging_band is AgingBand.NON_MOVING


# --- trailing 12-month consumption -----------------------------------------


def test_trailing_12m_count_and_quantity() -> None:
    result = _metrics([_movement("201", "2", 30), _movement("261", "3", 60), _movement("201", "1", 90), _movement("201", "6", 100)])
    assert result.consumption_count_12m == 4
    assert result.consumption_qty_12m == Decimal("12")


def test_movement_outside_lookback_window_is_excluded() -> None:
    result = _metrics([_movement("201", "5", days_ago=400)])
    assert result.consumption_count_12m == 0
    assert result.consumption_qty_12m == Decimal("0")


def test_non_consumption_movement_types_are_excluded_from_consumption() -> None:
    """A goods receipt (101) must never be counted as consumption, and must
    not inflate consumption_qty_12m."""
    result = _metrics([_movement("101", "50", days_ago=10)])
    assert result.consumption_count_12m == 0
    assert result.consumption_qty_12m == Decimal("0")


def test_reversed_issue_is_not_double_counted() -> None:
    """201 issue reversed by 202 nets to zero events and zero quantity --
    reuses movements.py's REVERSAL_OF, the one existing reversal rule."""
    result = _metrics([_movement("201", "5", days_ago=30), _movement("202", "5", days_ago=25)])
    assert result.consumption_count_12m == 0
    assert result.consumption_qty_12m == Decimal("0")


# --- aging band classification (on days_since_last_issue) ------------------


def test_fast_classification() -> None:
    result = _metrics([_movement("201", "1", days_ago=365)])
    assert result.aging_band is AgingBand.FAST


def test_slow_classification() -> None:
    result = _metrics([_movement("201", "1", days_ago=366)])
    assert result.aging_band is AgingBand.SLOW


def test_non_moving_classification() -> None:
    result = _metrics([_movement("201", "1", days_ago=731)])
    assert result.aging_band is AgingBand.NON_MOVING


# --- inventory turns ---------------------------------------------------


def test_inventory_turns_unavailable_when_stock_is_none() -> None:
    result = _metrics([_movement("201", "10", days_ago=30)], current_stock=None)
    assert result.inventory_turns is None
    assert result.inventory_turns_reason == "INSUFFICIENT_HISTORY"


def test_inventory_turns_unavailable_when_stock_is_zero() -> None:
    result = _metrics([_movement("201", "10", days_ago=30)], current_stock=Decimal("0"))
    assert result.inventory_turns is None
    assert result.inventory_turns_reason == "INSUFFICIENT_HISTORY"


def test_inventory_turns_computed_when_stock_available() -> None:
    result = _metrics([_movement("201", "20", days_ago=30)], current_stock=Decimal("10"))
    assert result.inventory_turns == Decimal("2")
    assert result.inventory_turns_reason is None


# --- multi-plant independence, and repository-orchestration -----------------


class _FakeRepository:
    """A minimal stand-in for PostgresMovementRepository -- proves
    ``compute_all_movement_metrics`` groups correctly, without a database.
    The real repository's query shape is proven separately, against Postgres,
    in test_movement_metrics_postgres.py."""

    def __init__(self, movements: list[dict], stock: dict[tuple[str, str], Decimal]) -> None:
        self._movements = movements
        self._stock = stock

    def get_movement_history(self, *, material=None, plant=None):
        rows = self._movements
        if material:
            rows = [r for r in rows if r["Matnr"] == material]
        if plant:
            rows = [r for r in rows if r["Werks"] == plant]
        return rows

    def get_current_stock(self, *, material=None, plant=None):
        return self._stock


def test_same_material_different_plants_are_calculated_independently() -> None:
    movements = [
        _movement("201", "5", days_ago=30, material="MAT1", plant="1300"),
        _movement("201", "5", days_ago=800, material="MAT1", plant="1500"),
    ]
    repository = _FakeRepository(movements, {})
    results = {(m.material, m.plant): m for m in compute_all_movement_metrics(
        repository, thresholds=THRESHOLDS, window_months=12, as_of=AS_OF
    )}

    assert results[("MAT1", "1300")].aging_band is AgingBand.FAST
    assert results[("MAT1", "1500")].aging_band is AgingBand.NON_MOVING
