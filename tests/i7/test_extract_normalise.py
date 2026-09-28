"""The staging adapter on the normalise views: aggregation, receipts, and the
OData fill for MARC's MRP fields.

No database server. The aggregations are pure functions over row objects; the
OData fill runs against an in-memory SQLite table, which is enough to prove the
column resolution and the key normalisation.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.initiatives.i7.adapters import extract
from app.initiatives.i7.adapters.extract import (
    _Rejections,
    aggregate_consumption,
    odata_key,
    receipts_by_line,
    schedule_dates_by_line,
)
from app.initiatives.i7.adapters.field_map import CREDIT_INDICATOR, DEBIT_INDICATOR
from app.initiatives.i7.adapters.validation import RejectionReason

ISSUES = frozenset({"201", "261"})
REVERSALS = frozenset({"202", "262"})


def _movement(material="5000000394", plant="1300", posted="2026-03-17", quantity="2",
              shkzg=CREDIT_INDICATOR, bwart="201", unit="EA"):
    return SimpleNamespace(material=material, plant=plant, posting_date=posted, quantity=quantity,
                           debit_credit_ind=shkzg, movement_type=bwart, base_unit_of_measure=unit)


# --- Consumption ----------------------------------------------------------------


class TestConsumption:
    def test_issues_add_and_reversals_net_out_within_a_month(self) -> None:
        rows = [
            _movement(quantity="5", posted="2026-03-02"),
            _movement(quantity="2", posted="2026-03-28", shkzg=DEBIT_INDICATOR, bwart="202"),
        ]

        totals = aggregate_consumption(rows, ISSUES, REVERSALS, _Rejections(1))

        entry = totals[("5000000394", "1300", date(2026, 3, 1))]
        assert entry["quantity"] == Decimal("3")
        assert (entry["movement_count"], entry["issue_count"], entry["reversal_count"]) == (2, 1, 1)

    def test_months_and_plants_are_separate_rows(self) -> None:
        rows = [
            _movement(posted="2026-03-31"),
            _movement(posted="2026-04-01"),
            _movement(plant="1500", posted="2026-03-31"),
        ]

        totals = aggregate_consumption(rows, ISSUES, REVERSALS, _Rejections(1))

        assert set(totals) == {
            ("5000000394", "1300", date(2026, 3, 1)),
            ("5000000394", "1300", date(2026, 4, 1)),
            ("5000000394", "1500", date(2026, 3, 1)),
        }

    def test_decimal_quantities_from_the_view_are_exact(self) -> None:
        """The view has already turned the CSV's "0,989" into "0.989"."""
        totals = aggregate_consumption(
            [_movement(quantity="0.989"), _movement(quantity="1.000")], ISSUES, REVERSALS, _Rejections(1)
        )

        assert totals[("5000000394", "1300", date(2026, 3, 1))]["quantity"] == Decimal("1.989")

    @pytest.mark.parametrize(
        ("row", "reason"),
        [
            (_movement(material=""), RejectionReason.MISSING_MATERIAL),
            (_movement(plant=None), RejectionReason.MISSING_PLANT),
            (_movement(posted="17.03.2026"), RejectionReason.INVALID_DATE),
            (_movement(posted=None), RejectionReason.INVALID_DATE),
            (_movement(quantity="abc"), RejectionReason.INVALID_QUANTITY),
        ],
    )
    def test_a_bad_row_is_counted_not_dropped(self, row, reason) -> None:
        rejections = _Rejections(1)

        totals = aggregate_consumption([row, _movement()], ISSUES, REVERSALS, rejections)

        assert rejections.counts == {reason: 1}
        assert len(totals) == 1  # the good row still stages

    def test_the_unit_is_kept(self) -> None:
        totals = aggregate_consumption([_movement(unit="MT")], ISSUES, REVERSALS, _Rejections(1))

        assert totals[("5000000394", "1300", date(2026, 3, 1))]["unit_of_measure"] == "MT"


# --- Purchase orders ----------------------------------------------------------


class TestReceiptsAndSchedules:
    def test_earliest_receipt_date_and_total_quantity_per_line(self) -> None:
        rows = [
            SimpleNamespace(purchasing_document="4100001324", item="10", posting_date="2019-03-07", quantity="1.000"),
            SimpleNamespace(purchasing_document="4100001324", item="10", posting_date="2019-02-01", quantity="2"),
            SimpleNamespace(purchasing_document="4100001324", item="20", posting_date="2019-05-05", quantity="4"),
        ]

        receipts = receipts_by_line(rows, _Rejections(1))

        assert receipts[("4100001324", "10")] == (date(2019, 2, 1), Decimal("3.000"))
        assert receipts[("4100001324", "20")] == (date(2019, 5, 5), Decimal("4"))

    def test_an_unparseable_receipt_is_rejected_and_left_out(self) -> None:
        rejections = _Rejections(1)
        rows = [
            SimpleNamespace(purchasing_document="1", item="10", posting_date="bad", quantity="1"),
            SimpleNamespace(purchasing_document="1", item="10", posting_date="2019-01-01", quantity="x"),
        ]

        assert receipts_by_line(rows, rejections) == {}
        assert rejections.counts == {RejectionReason.INVALID_DATE: 1, RejectionReason.INVALID_QUANTITY: 1}

    def test_earliest_schedule_date_per_line(self) -> None:
        rows = [
            SimpleNamespace(purchasing_document="1", item="10", delivery_date="2026-05-01"),
            SimpleNamespace(purchasing_document="1", item="10", delivery_date="2026-04-01"),
            SimpleNamespace(purchasing_document="1", item="10", delivery_date=None),
        ]

        assert schedule_dates_by_line(rows) == {("1", "10"): date(2026, 4, 1)}


# --- MARC's MRP fields from OData -----------------------------------------------


class TestOdataFill:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("000000000011251001", "11251001"), ("  0005000000394 ", "5000000394"), ("000", None), (None, None)],
    )
    def test_odata_keys_are_spelled_as_the_views_spell_them(self, raw, expected) -> None:
        assert odata_key(raw) == expected

    def test_the_live_csv_marc_takes_every_mrp_field_from_odata(self, monkeypatch) -> None:
        live = ["MANDT", "MATNR", "WERKS", "column_4", "UMLMC", "ZZCRITIC"]
        monkeypatch.setattr(extract, "_columns", lambda session, table: live)

        assert extract._marc_gaps(session=None) == list(extract.MRP_FIELDS_FROM_ODATA)

    def test_a_workbook_marc_takes_nothing_from_odata(self, monkeypatch) -> None:
        workbook = ["material", "plant", "mrp_type", "planned_deliv_time", "reorder_point",
                    "maximum_stock_level", "df_at_plant_level"]
        monkeypatch.setattr(extract, "_columns", lambda session, table: workbook)

        assert extract._marc_gaps(session=None) == []

    def test_fields_are_read_from_the_odata_table_by_normalised_key(self) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE odata_material_plant (Matnr TEXT, Werks TEXT, Dismm TEXT, "
                "Plifz TEXT, Minbe TEXT, Mabst TEXT, Lvorm TEXT)"
            ))
            connection.execute(text(
                "INSERT INTO odata_material_plant VALUES "
                "('000000000011251001', '1300', 'PD', '14', '2.000', '10.000', ''), "
                "('000000000011251002', '1500', '', '0', '0', '0', 'X')"
            ))

        with Session(engine) as session:
            fields = extract._odata_mrp_fields(session, ["mrp_type", "planned_deliv_time"])

        assert fields[("11251001", "1300")] == {"mrp_type": "PD", "planned_deliv_time": "14"}
        assert fields[("11251002", "1500")] == {"mrp_type": "", "planned_deliv_time": "0"}

    def test_no_odata_table_means_gaps_stay_null_not_an_error(self) -> None:
        with Session(create_engine("sqlite://")) as session:
            assert extract._odata_mrp_fields(session, ["mrp_type"]) == {}

    def test_nothing_to_fill_reads_nothing(self) -> None:
        assert extract._odata_mrp_fields(session=None, labels=[]) == {}
