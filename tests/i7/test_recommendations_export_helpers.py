"""The CSV export's cell formatting (the endpoint itself needs the database, which is not reachable locally)."""

from decimal import Decimal

from app.api.i7.recommendations import EXPORT_COLUMNS, _csv_number, _csv_text, router


def test_quantities_are_plain_digits_and_a_missing_one_is_blank_not_zero():
    assert _csv_number(Decimal("12.500")) == "12.5"
    assert _csv_number(Decimal("1E+3")) == "1000"
    assert _csv_number(Decimal("0")) == "0"
    assert _csv_number(None) == ""


def test_text_that_excel_would_run_as_a_formula_is_quoted():
    assert _csv_text("=SUM(A1)") == "'=SUM(A1)"
    assert _csv_text("+1") == "'+1"
    assert _csv_text("Bearing, 6205") == "Bearing, 6205"
    assert _csv_text(None) == ""


def test_export_route_is_registered_before_the_id_route():
    paths = [route.path for route in router.routes]
    assert paths.index("/recommendations/export") < paths.index("/recommendations/{recommendation_id}")


def test_export_carries_the_requested_columns():
    for column in ("Material Description", "Current ROP (SAP)", "Recommended Max Stock", "Criticality", "Reason"):
        assert column in EXPORT_COLUMNS
