"""Local reconciliation against reference counts, tolerance-based."""

from decimal import Decimal

from app.initiatives.i13.reconciliation import reconcile


def test_reference_unavailable_when_no_reference_given() -> None:
    result = reconcile("ZMM065", 100, None, tolerance_pct=5.0)
    assert result.status == "REFERENCE_UNAVAILABLE"
    assert result.within_tolerance is None


def test_within_tolerance() -> None:
    result = reconcile("ZMM065", 100, 103, tolerance_pct=5.0)
    assert result.absolute_difference == 3
    assert result.within_tolerance is True
    assert result.status == "RECONCILED"


def test_out_of_tolerance() -> None:
    result = reconcile("ZMM065", 100, 50, tolerance_pct=5.0)
    assert result.within_tolerance is False
    assert result.status == "OUT_OF_TOLERANCE"


def test_exact_match_is_zero_difference() -> None:
    result = reconcile("30-Day GR Report", 42, 42, tolerance_pct=5.0)
    assert result.absolute_difference == 0
    assert result.percentage_difference == Decimal("0")
    assert result.within_tolerance is True


def test_zero_reference_with_nonzero_computed_is_out_of_tolerance() -> None:
    result = reconcile("ZMM065", 5, 0, tolerance_pct=5.0)
    assert result.within_tolerance is False
