"""Movement normalisation: reversal netting for receipts and issues."""

from datetime import date
from decimal import Decimal

from app.initiatives.i13.movements import (
    ISSUE_TYPES,
    RECEIPT_TYPES,
    filter_by_window,
    is_reversal,
    last_unreversed_date,
    latest_movement_date,
    net_event_count,
    net_quantity,
)


def _row(bwart: str, menge: str, budat: date | None = None) -> dict:
    return {"Bwart": bwart, "Menge": Decimal(menge), "BudatMkpf": budat}


def test_is_reversal() -> None:
    assert is_reversal("102") is True
    assert is_reversal("202") is True
    assert is_reversal("262") is True
    assert is_reversal("101") is False


def test_net_quantity_receipt_reversed_in_full_nets_to_zero() -> None:
    rows = [_row("101", "10"), _row("102", "10")]
    assert net_quantity(rows, RECEIPT_TYPES) == Decimal("0")


def test_net_quantity_receipt_partially_reversed() -> None:
    rows = [_row("101", "10"), _row("102", "4")]
    assert net_quantity(rows, RECEIPT_TYPES) == Decimal("6")


def test_net_quantity_issue_reversal_201_202() -> None:
    rows = [_row("201", "5"), _row("202", "5")]
    assert net_quantity(rows, ISSUE_TYPES) == Decimal("0")


def test_net_quantity_issue_reversal_261_262() -> None:
    rows = [_row("261", "8"), _row("262", "3")]
    assert net_quantity(rows, ISSUE_TYPES) == Decimal("5")


def test_net_quantity_ignores_unrelated_movement_types() -> None:
    rows = [_row("541", "100"), _row("101", "10")]
    assert net_quantity(rows, RECEIPT_TYPES) == Decimal("10")


def test_net_event_count_fully_reversed_is_zero() -> None:
    rows = [_row("101", "10"), _row("102", "10")]
    assert net_event_count(rows, RECEIPT_TYPES) == 0


def test_net_event_count_floors_at_zero() -> None:
    rows = [_row("102", "10")]
    assert net_event_count(rows, RECEIPT_TYPES) == 0


def test_filter_by_window_excludes_rows_without_a_date() -> None:
    rows = [_row("101", "1", date(2026, 1, 1)), _row("101", "1", None)]
    filtered = filter_by_window(rows, start=date(2025, 1, 1), end=date(2026, 12, 31))
    assert len(filtered) == 1


def test_latest_movement_date() -> None:
    rows = [_row("101", "1", date(2026, 1, 1)), _row("101", "1", date(2026, 6, 1))]
    assert latest_movement_date(rows) == date(2026, 6, 1)


def test_latest_movement_date_no_movements() -> None:
    assert latest_movement_date([]) is None


def test_last_unreversed_date_skips_an_issue_its_reversal_cancelled() -> None:
    rows = [_row("261", "1", date(2026, 1, 10)), _row("261", "1", date(2026, 3, 1)), _row("262", "1", date(2026, 3, 2))]
    assert last_unreversed_date(rows, ISSUE_TYPES) == date(2026, 1, 10)


def test_last_unreversed_date_is_none_when_every_issue_was_reversed() -> None:
    rows = [_row("201", "2", date(2026, 2, 1)), _row("202", "2", date(2026, 2, 3))]
    assert last_unreversed_date(rows, ISSUE_TYPES) is None


def test_a_reversal_only_cancels_its_own_movement_type() -> None:
    rows = [_row("201", "1", date(2026, 2, 1)), _row("262", "1", date(2026, 2, 5))]
    assert last_unreversed_date(rows, ISSUE_TYPES) == date(2026, 2, 1)


def test_last_unreversed_date_as_of_ignores_later_postings() -> None:
    rows = [_row("261", "1", date(2026, 3, 1)), _row("262", "1", date(2026, 4, 1)), _row("261", "1", date(2026, 5, 1))]
    # On 15-Mar the March issue was still standing; its reversal came later.
    assert last_unreversed_date(rows, ISSUE_TYPES, as_of=date(2026, 3, 15)) == date(2026, 3, 1)
    assert last_unreversed_date(rows, ISSUE_TYPES) == date(2026, 5, 1)
