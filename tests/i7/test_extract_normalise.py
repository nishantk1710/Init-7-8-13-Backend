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
    odata_flag_to_canonical,
    odata_key,
    receipts_by_line,
    schedule_dates_by_line,
)
from app.initiatives.i7.adapters.field_map import CREDIT_INDICATOR, DEBIT_INDICATOR
from app.initiatives.i7.adapters.validation import RejectionReason, parse_flag

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


# --- LVORM / deletion_flag: two sources, two encodings -------------------------
#
# Measured against the live odata_material_plant table (27 Sep): Lvorm = "1"
# for a deleted material-plant, never "X" -- the workbook/n_marc encoding
# parse_flag() was written for. 479 material-plants were staged with
# deletion_flag = False as a result, because "1" != DELETION_FLAG_TRUE.


class TestLvormEncoding:
    """Tests 1-3 (regression spec): the conversion function in isolation."""

    def test_workbook_x_produces_deleted(self) -> None:
        assert parse_flag(odata_flag_to_canonical("X")) is True

    def test_odata_one_produces_deleted(self) -> None:
        """The bug this fix addresses: OData's "1", not "X"."""
        assert odata_flag_to_canonical("1") == "X"
        assert parse_flag(odata_flag_to_canonical("1")) is True

    def test_blank_from_either_source_produces_not_deleted(self) -> None:
        assert parse_flag(odata_flag_to_canonical("")) is False
        assert parse_flag(odata_flag_to_canonical(None)) is False

    def test_a_workbook_value_reaching_this_path_is_unaffected(self) -> None:
        """Idempotent: "X" is not itself "1", so it must pass through unchanged
        rather than being (incorrectly) read as the OData encoding."""
        assert odata_flag_to_canonical("X") == "X"

    def test_an_unrecognised_value_passes_through_rather_than_being_guessed_at(self) -> None:
        """Neither "X" nor "1": not laundered into either boolean state here --
        parse_flag() will report it via its own (strict) rule instead of this
        function silently picking one."""
        assert odata_flag_to_canonical("L") == "L"


class TestMaterialPlantStagingAppliesTheOdataConversion:
    """Test 2 (regression spec) at the merge site itself (_stage_material_plants),
    not only at the helper function -- this is what actually failed for the 479
    live records: the conversion has to run *before* values.update() overlays the
    raw OData row onto the n_marc-sourced one, or parse_flag() never sees it."""

    def _staged_rows(self, monkeypatch, marc_rows, odata_present, odata_rows):
        """Runs _stage_material_plants's row-building logic and returns what it
        would have upserted. n_marc is a real SQLite table (so rows have the
        ._mapping the function needs); only _marc_gaps/_odata_mrp_fields/_upsert
        are stubbed, since those are the ones that would otherwise need a real
        odata_material_plant table and a real dialect to write through."""
        from app.initiatives.i7.adapters.ingestion_policy import ExtractIngestionPolicy

        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE n_marc (material TEXT, plant TEXT, mrp_type TEXT, "
                "planned_deliv_time TEXT, reorder_point TEXT, maximum_stock_level TEXT, "
                "df_at_plant_level TEXT)"
            ))
            for r in marc_rows:
                connection.execute(
                    text(
                        "INSERT INTO n_marc VALUES (:material, :plant, :mrp_type, "
                        ":planned_deliv_time, :reorder_point, :maximum_stock_level, "
                        ":df_at_plant_level)"
                    ),
                    r,
                )
        session = Session(engine)

        monkeypatch.setattr(extract, "_marc_gaps", lambda session: list(extract.MRP_FIELDS_FROM_ODATA) if not odata_present else [])
        monkeypatch.setattr(
            extract, "_odata_mrp_fields",
            lambda session, labels: {
                (r["Matnr"].lstrip("0") or None, r["Werks"]): {
                    label: r[extract.MRP_FIELDS_FROM_ODATA[label]] for label in labels
                }
                for r in odata_rows
            },
        )
        captured: list[dict] = []
        monkeypatch.setattr(extract, "_upsert", lambda session, model, rows, conflict: captured.extend(rows))
        monkeypatch.setattr(extract, "_safe_batch_size", lambda model, requested, session=None: 5000)

        extract._stage_material_plants(
            session=session, run_id=1, policy=ExtractIngestionPolicy(), rejections=_Rejections(1)
        )
        return {(r["sap_material_number"], r["sap_plant_code"]): r for r in captured}

    def test_odata_lvorm_one_becomes_deletion_flag_true(self, monkeypatch) -> None:
        """The 479-record failure mode, reproduced: the live MARC CSV carries
        none of the MRP columns, so every field including Lvorm comes from
        odata_material_plant, raw, at the merge site."""
        marc_rows = [{"material": "11251001", "plant": "1300", "mrp_type": None,
                      "planned_deliv_time": None, "reorder_point": None,
                      "maximum_stock_level": None, "df_at_plant_level": None}]
        odata_rows = [{"Matnr": "000000000011251001", "Werks": "1300", "Dismm": "PD",
                       "Plifz": "14", "Minbe": "10", "Mabst": "20", "Lvorm": "1"}]

        staged = self._staged_rows(monkeypatch, marc_rows, odata_present=False, odata_rows=odata_rows)

        assert staged[("11251001", "1300")]["deletion_flag"] is True

    def test_odata_lvorm_blank_stays_not_deleted(self, monkeypatch) -> None:
        marc_rows = [{"material": "11251002", "plant": "1300", "mrp_type": None,
                      "planned_deliv_time": None, "reorder_point": None,
                      "maximum_stock_level": None, "df_at_plant_level": None}]
        odata_rows = [{"Matnr": "000000000011251002", "Werks": "1300", "Dismm": "ND",
                       "Plifz": "7", "Minbe": "5", "Mabst": "15", "Lvorm": ""}]

        staged = self._staged_rows(monkeypatch, marc_rows, odata_present=False, odata_rows=odata_rows)

        assert staged[("11251002", "1300")]["deletion_flag"] is False

    def test_other_odata_fields_are_unaffected_by_the_flag_conversion(self, monkeypatch) -> None:
        """DISMM/PLIFZ/MINBE/MABST must reach staging exactly as OData sent
        them -- only df_at_plant_level goes through odata_flag_to_canonical()."""
        marc_rows = [{"material": "11251001", "plant": "1300", "mrp_type": None,
                      "planned_deliv_time": None, "reorder_point": None,
                      "maximum_stock_level": None, "df_at_plant_level": None}]
        odata_rows = [{"Matnr": "000000000011251001", "Werks": "1300", "Dismm": "PD",
                       "Plifz": "14", "Minbe": "10", "Mabst": "20", "Lvorm": "1"}]

        staged = self._staged_rows(monkeypatch, marc_rows, odata_present=False, odata_rows=odata_rows)
        row = staged[("11251001", "1300")]

        assert row["mrp_type"] == "PD"
        assert row["planned_delivery_time_days"] == 14
        assert row["current_reorder_point"] == Decimal("10")
        assert row["current_maximum_stock"] == Decimal("20")

    def test_a_workbook_marc_row_is_unaffected_no_odata_involved(self, monkeypatch) -> None:
        """When raw_marc already carries LVORM (the workbook path), nothing
        from odata_material_plant is merged in, so no conversion runs -- and
        none is needed, since the workbook already spells it "X"."""
        marc_rows = [{"material": "11251003", "plant": "1300", "mrp_type": "PD",
                      "planned_deliv_time": "14", "reorder_point": "10",
                      "maximum_stock_level": "20", "df_at_plant_level": "X"}]

        staged = self._staged_rows(monkeypatch, marc_rows, odata_present=True, odata_rows=[])

        assert staged[("11251003", "1300")]["deletion_flag"] is True


# --- Unit price and currency from the OData valuation set ---------------------


def _valuation_session(rows: str) -> Session:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE odata_material_valuation (Matnr TEXT, Bwkey TEXT, Verpr TEXT, Peinh TEXT, Waers TEXT)"
        ))
        connection.execute(text(f"INSERT INTO odata_material_valuation VALUES {rows}"))
    return Session(engine)


class TestOdataPrices:
    def test_price_is_verpr_per_price_unit_in_the_stated_currency(self) -> None:
        with _valuation_session("('000000000022271519', '1500', '250.00', '10', 'ZAR')") as session:
            prices = extract._odata_prices(session, _Rejections(1))

        assert prices == {"22271519": (Decimal("25.00"), "ZAR")}

    def test_the_highest_valuation_area_wins_and_keeps_its_own_currency(self) -> None:
        rows = "('000000000011251001', '1300', '10.00', '1', 'ZAR'), ('000000000011251001', '3000', '12.50', '1', 'NAD')"
        with _valuation_session(rows) as session:
            prices = extract._odata_prices(session, _Rejections(1))

        assert prices["11251001"] == (Decimal("12.50"), "NAD")

    def test_a_zero_price_unit_is_rejected_not_assumed_to_be_one(self) -> None:
        rejections = _Rejections(1)
        with _valuation_session("('000000000011251001', '1300', '10.00', '0', 'ZAR')") as session:
            prices = extract._odata_prices(session, rejections)

        assert prices == {}
        assert rejections.counts == {RejectionReason.INVALID_QUANTITY: 1}

    def test_no_currency_from_sap_means_none_not_a_guess(self) -> None:
        with _valuation_session("('000000000011251001', '1300', '10.00', '1', '')") as session:
            prices = extract._odata_prices(session, _Rejections(1))

        assert prices["11251001"] == (Decimal("10.00"), None)

    def test_no_valuation_table_means_no_price_not_an_error(self) -> None:
        with Session(create_engine("sqlite://")) as session:
            assert extract._odata_prices(session, _Rejections(1)) == {}

    def test_the_live_csv_mbew_sends_the_price_to_odata(self, monkeypatch) -> None:
        live_mbew = ["MANDT", "MATNR", "BWKEY", "BWTAR", "LVORM", "column_6", "BWPRS"]
        monkeypatch.setattr(extract, "_columns", lambda session, table: live_mbew)
        monkeypatch.setattr(extract, "_odata_prices", lambda session, rejections: {"x": (Decimal(1), "ZAR")})

        assert extract._price_by_material(session=None, rejections=_Rejections(1)) == {"x": (Decimal(1), "ZAR")}
