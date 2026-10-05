"""FR-6: reconciliation against ZMM065 and the 30-Day GR Report, like with like."""

from datetime import date, timedelta
from decimal import Decimal

from app.core.config import Settings
from app.initiatives.i13.config import build_i13_config
from app.initiatives.i13.models import AgingBand
from app.initiatives.i13.report_validation import (
    GrConfirmation,
    Zmm065MismatchReason,
    validate_gr_30day,
    validate_zmm065,
    zmm065_report_date,
)
from app.integrations.sap.postgres_reports import Gr30DayRow, Zmm065Row

THRESHOLDS = build_i13_config(Settings()).aging  # 365 / 730
REPORT_DATE = date(2026, 8, 3)


def _z(material: str, stock_type: str, last_gi: date | None, plant: str = "1300") -> Zmm065Row:
    days = (REPORT_DATE - last_gi).days if last_gi else 400
    return Zmm065Row(material=material, plant=plant, stock_type=stock_type, last_gi_date=last_gi, days=days)


def _validate(rows, platform_last: dict, history_start=date(2025, 8, 8), moved: set[str] | None = None):
    """``moved``: materials with any movement at all; defaults to every row's."""
    moved = moved if moved is not None else {row.material for row in rows}
    return validate_zmm065(
        rows,
        last_issue_as_of=lambda key, day: platform_last.get(key[0]),
        has_movements=lambda key: key[0] in moved,
        history_start=history_start,
        thresholds=THRESHOLDS,
        tolerance_pct=5.0,
    )


# --- ZMM065 -------------------------------------------------------------


def test_the_report_date_is_last_issue_plus_days() -> None:
    rows = [_z("A", "Fast Moving", date(2026, 7, 30)), _z("B", "Slow Moving", date(2025, 6, 1))]
    assert zmm065_report_date(rows) == REPORT_DATE


def test_bands_are_compared_as_of_the_report_date() -> None:
    # 300 days before the report date is FAST then, even though it is SLOW today.
    last = REPORT_DATE - timedelta(days=300)
    result = _validate([_z("A", "Fast Moving", last)], {"A": last})
    assert (result.compared, result.agreed) == (1, 1)
    fast = result.results[0]
    assert (fast.source_name, fast.computed_count, fast.reference_count, fast.status) == (
        "ZMM065 · Fast moving", 1, 1, "RECONCILED",
    )


def test_non_aging_statuses_are_excluded_and_counted() -> None:
    rows = [_z("A", "OBSOLETE", None), _z("B", "INSURANCE", None), _z("C", "Fast Moving", date(2026, 7, 1))]
    result = _validate(rows, {"C": date(2026, 7, 1)})
    assert result.excluded_non_aging == {"OBSOLETE": 1, "INSURANCE": 1}
    assert result.compared == 1


def test_a_last_issue_older_than_the_platform_history_is_named_as_such() -> None:
    old = date(2024, 12, 1)  # before the platform's movement history starts
    result = _validate([_z("A", "Slow Moving", old)], {})
    (mismatch,) = result.mismatches
    assert mismatch.platform_band is AgingBand.NON_MOVING
    assert mismatch.reason is Zmm065MismatchReason.LAST_ISSUE_BEFORE_PLATFORM_HISTORY


def test_same_last_issue_but_a_different_class_is_the_reports_own_rule() -> None:
    last = REPORT_DATE - timedelta(days=1000)
    result = _validate([_z("A", "Fast Moving", last)], {"A": last}, history_start=date(2023, 1, 1))
    assert result.mismatches[0].reason is Zmm065MismatchReason.REPORT_CLASSIFICATION_DIFFERS
    assert result.mismatch_reasons == {"REPORT_CLASSIFICATION_DIFFERS": 1}


def test_differing_last_issue_dates_are_reported() -> None:
    result = _validate([_z("A", "Slow Moving", date(2025, 5, 1))], {"A": date(2026, 7, 1)})
    assert result.mismatches[0].reason is Zmm065MismatchReason.LAST_ISSUE_DATE_DIFFERS


def test_a_material_with_no_movements_at_all_is_not_in_the_platforms_data() -> None:
    # Received-only would still count as "in the data"; nothing at all does not.
    rows = [_z("A", "Fast Moving", date(2026, 7, 30)), _z("B", "Fast Moving", date(2026, 7, 30))]
    result = _validate(rows, {}, moved={"B"})
    reasons = {m.material: m.reason for m in result.mismatches}
    assert reasons == {
        "A": Zmm065MismatchReason.MATERIAL_NOT_IN_PLATFORM_DATA,
        "B": Zmm065MismatchReason.LAST_ISSUE_DATE_DIFFERS,
    }


def test_no_rows_means_no_reference() -> None:
    result = _validate([], {})
    assert all(r.status == "REFERENCE_UNAVAILABLE" for r in result.results)


# --- 30-Day GR Report ---------------------------------------------------


def _g(po: str, item: str, posted: date) -> Gr30DayRow:
    return Gr30DayRow(post_date=posted, material="MAT", po_number=po, po_item=item, delivered_quantity=Decimal("1"))


def test_each_receipt_is_confirmed_against_the_po_line_and_its_gr_date() -> None:
    day = date(2026, 8, 11)
    rows = [_g("41", "10", day), _g("41", "20", day), _g("99", "10", day)]
    result = validate_gr_30day(
        rows,
        po_line_plant={("41", "10"): "1500", ("41", "20"): "1500"},
        receipt_dates={("41", "10"): {day}, ("41", "20"): {day - timedelta(days=3)}},
        tolerance_pct=5.0,
    )
    statuses = {(c.po_number, c.po_item): c.status for c in result.unconfirmed}
    assert result.confirmed == 1
    assert statuses == {
        ("41", "20"): GrConfirmation.NO_RECEIPT_ON_POST_DATE,
        ("99", "10"): GrConfirmation.PO_LINE_NOT_FOUND,
    }
    assert (result.result.computed_count, result.result.reference_count) == (1, 3)
    assert result.result.status == "OUT_OF_TOLERANCE"


def test_the_window_and_the_platforms_own_receipts_are_context_not_the_comparison() -> None:
    day = date(2026, 8, 11)
    result = validate_gr_30day(
        [_g("41", "10", day)],
        po_line_plant={("41", "10"): "1500", ("42", "10"): "1500", ("43", "10"): "1300"},
        receipt_dates={("41", "10"): {day}, ("42", "10"): {day - timedelta(days=10)}, ("43", "10"): {day}},
        tolerance_pct=5.0,
    )
    assert (result.report_date, result.window_start) == (day, day - timedelta(days=29))
    assert result.plants == {"1500": 1}
    # Two 1500 receipts in the window; the 1300 one is another plant's.
    assert result.platform_receipts_in_window == 2
    assert result.result.status == "RECONCILED"


def test_no_gr_rows_means_no_reference() -> None:
    result = validate_gr_30day([], po_line_plant={}, receipt_dates={}, tolerance_pct=5.0)
    assert result.result.status == "REFERENCE_UNAVAILABLE"
