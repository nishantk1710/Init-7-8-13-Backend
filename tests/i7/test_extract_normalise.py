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
from app.shared.valuation_price import PriceMissing, Valuation, choose_unit_price

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
                    "safety_stock", "maximum_stock_level", "df_at_plant_level"]
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

    def _staged_rows(
        self, monkeypatch, marc_rows, odata_present, odata_rows, valuations=None, rejections=None, policy=None
    ):
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
                "mrp_controller TEXT, planned_deliv_time TEXT, reorder_point TEXT, "
                "safety_stock TEXT, maximum_stock_level TEXT, df_at_plant_level TEXT)"
            ))
            for r in marc_rows:
                connection.execute(
                    text(
                        "INSERT INTO n_marc VALUES (:material, :plant, :mrp_type, "
                        ":mrp_controller, :planned_deliv_time, :reorder_point, "
                        ":safety_stock, :maximum_stock_level, :df_at_plant_level)"
                    ),
                    {"safety_stock": None, "mrp_controller": None, **r},
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
        monkeypatch.setattr(
            extract, "_valuations_by_plant", lambda session, scope=frozenset(): valuations or {}
        )

        extract._stage_material_plants(
            session=session,
            run_id=1,
            policy=policy or ExtractIngestionPolicy(),
            rejections=rejections or _Rejections(1),
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

    def test_mrp_controller_is_staged_upper_cased_and_blank_stays_null(self, monkeypatch) -> None:
        """DISPO is what the OAR rule keys on. It is staged trimmed and upper-cased
        (as I13's classify_material_scope normalises it), and a blank stays NULL so
        the policy can read "no controller" as not OAR."""
        base = {"plant": "1300", "mrp_type": "PD", "planned_deliv_time": "14",
                "reorder_point": "10", "maximum_stock_level": "20", "df_at_plant_level": None}
        marc_rows = [
            {**base, "material": "11251010", "mrp_controller": " oar "},
            {**base, "material": "11251011", "mrp_controller": "OAR"},
            {**base, "material": "11251012", "mrp_controller": ""},
            {**base, "material": "11251013", "mrp_controller": None},
            {**base, "material": "11251014", "mrp_controller": "K01"},
        ]

        staged = self._staged_rows(monkeypatch, marc_rows, odata_present=True, odata_rows=[])

        assert staged[("11251010", "1300")]["mrp_controller"] == "OAR"
        assert staged[("11251011", "1300")]["mrp_controller"] == "OAR"
        assert staged[("11251012", "1300")]["mrp_controller"] is None
        assert staged[("11251013", "1300")]["mrp_controller"] is None
        assert staged[("11251014", "1300")]["mrp_controller"] == "K01"


# --- Unit price: one plant, one price ------------------------------------------
#
# The price rule itself is tested in tests/test_valuation_price.py. These prove the staging
# adapter feeds it the right rows -- per plant, PEINH applied, currency validated -- and records
# the outcome: the price, or the named reason there is none, counted so it can be reviewed.


def _valuation_session(rows: str) -> Session:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE odata_material_valuation (Matnr TEXT, Bwkey TEXT, Verpr TEXT, Peinh TEXT, Waers TEXT)"
        ))
        connection.execute(text(f"INSERT INTO odata_material_valuation VALUES {rows}"))
    return Session(engine)


def _mbew_session(rows: list[tuple]) -> Session:
    """n_mbew as the normalise view presents it: (material, plant, bwtar, verpr, peinh)."""
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE n_mbew (material TEXT, valuation_area TEXT, valuation_type TEXT, "
            "moving_price TEXT, price_unit TEXT)"
        ))
        for material, plant, bwtar, verpr, peinh in rows:
            connection.execute(
                text("INSERT INTO n_mbew VALUES (:m, :k, :t, :v, :p)"),
                {"m": material, "k": plant, "t": bwtar, "v": verpr, "p": peinh},
            )
    return Session(engine)


MBEW_COLUMNS = ["MATNR", "BWKEY", "BWTAR", "VERPR", "PEINH"]


class TestValuationRow:
    def test_the_price_is_verpr_per_price_unit(self) -> None:
        row = extract._valuation_row(None, "250.00", "10", peinh_present=True, currency="ZAR", currency_present=True)

        assert row.price == Decimal("25.0000")
        assert row.currency == "ZAR"
        assert row.problem is None

    def test_a_price_per_three_units_is_rounded_to_the_column_scale(self) -> None:
        row = extract._valuation_row(None, "100", "3", peinh_present=True)

        assert row.price == Decimal("33.3333")

    @pytest.mark.parametrize("peinh", ["0", "", "-5", "abc"])
    def test_a_present_but_unusable_price_unit_is_refused_not_assumed_to_be_one(self, peinh: str) -> None:
        row = extract._valuation_row(None, "10.00", peinh, peinh_present=True)

        assert row.price is None
        assert row.problem is PriceMissing.BAD_PRICE_UNIT

    def test_an_absent_price_unit_column_means_one_unit(self) -> None:
        row = extract._valuation_row(None, "10.00", None, peinh_present=False)

        assert row.price == Decimal("10.0000")

    @pytest.mark.parametrize("verpr", ["", None, "n/a"])
    def test_an_unreadable_price_is_named(self, verpr) -> None:
        row = extract._valuation_row(None, verpr, "1", peinh_present=True)

        assert row.price is None
        assert row.problem is PriceMissing.UNREADABLE_PRICE

    def test_a_blank_currency_is_a_defect_only_when_the_source_has_a_currency_column(self) -> None:
        stated = extract._valuation_row(None, "10", "1", peinh_present=True, currency="", currency_present=True)
        absent = extract._valuation_row(None, "10", "1", peinh_present=True, currency=None, currency_present=False)

        assert stated.problem is PriceMissing.MISSING_CURRENCY and stated.price is None
        assert absent.problem is None and absent.price == Decimal("10.0000") and absent.currency is None

    def test_the_valuation_type_is_trimmed_and_upper_cased(self) -> None:
        assert extract._valuation_row(" new ", "10", "1", peinh_present=True).valuation_type == "NEW"
        assert extract._valuation_row("", "10", "1", peinh_present=True).valuation_type is None


class TestOdataValuations:
    def test_price_is_verpr_per_price_unit_in_the_stated_currency_at_its_plant(self) -> None:
        with _valuation_session("('000000000022271519', '1500', '250.00', '10', 'ZAR')") as session:
            valuations = extract._odata_valuations(session)

        [row] = valuations[("22271519", "1500")]
        assert (row.price, row.currency, row.problem) == (Decimal("25.0000"), "ZAR", None)

    def test_each_plant_keeps_its_own_price_and_currency_nothing_is_the_highest(self) -> None:
        rows = "('000000000011251001', '1300', '10.00', '1', 'ZAR'), ('000000000011251001', '3000', '12.50', '1', 'NAD')"
        with _valuation_session(rows) as session:
            valuations = extract._odata_valuations(session)

        assert valuations[("11251001", "1300")][0].price == Decimal("10.0000")
        assert valuations[("11251001", "1300")][0].currency == "ZAR"
        assert valuations[("11251001", "3000")][0].price == Decimal("12.5000")
        assert valuations[("11251001", "3000")][0].currency == "NAD"

    def test_a_zero_price_unit_is_kept_as_a_named_problem_not_dropped(self) -> None:
        with _valuation_session("('000000000011251001', '1300', '10.00', '0', 'ZAR')") as session:
            valuations = extract._odata_valuations(session)

        [row] = valuations[("11251001", "1300")]
        assert row.price is None
        assert row.problem is PriceMissing.BAD_PRICE_UNIT

    def test_a_blank_currency_is_rejected_not_staged_with_a_guess(self) -> None:
        with _valuation_session("('000000000011251001', '1300', '10.00', '1', '')") as session:
            valuations = extract._odata_valuations(session)

        [row] = valuations[("11251001", "1300")]
        assert row.price is None
        assert row.problem is PriceMissing.MISSING_CURRENCY

    def test_a_plant_outside_the_scope_is_not_read(self) -> None:
        rows = "('000000000011251001', '1300', '10.00', '1', 'ZAR'), ('000000000011251001', '3000', '12.50', '1', 'NAD')"
        with _valuation_session(rows) as session:
            valuations = extract._odata_valuations(session, frozenset({"1300"}))

        assert set(valuations) == {("11251001", "1300")}

    def test_the_valuation_type_is_read_when_the_set_carries_it(self) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE odata_material_valuation "
                "(Matnr TEXT, Bwkey TEXT, Bwtar TEXT, Verpr TEXT, Peinh TEXT, Waers TEXT)"
            ))
            connection.execute(text(
                "INSERT INTO odata_material_valuation VALUES "
                "('8000000001', '1300', 'NEW', '900.00', '1', 'ZAR'), "
                "('8000000001', '1300', 'REFURBISHD', '300.00', '1', 'ZAR')"
            ))
        with Session(engine) as session:
            valuations = extract._odata_valuations(session)

        assert {r.valuation_type for r in valuations[("8000000001", "1300")]} == {"NEW", "REFURBISHD"}

    def test_a_set_with_no_valuation_area_cannot_place_a_price_so_stages_none(self) -> None:
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE odata_material_valuation (Matnr TEXT, Verpr TEXT)"))
            connection.execute(text("INSERT INTO odata_material_valuation VALUES ('1', '10.00')"))
        with Session(engine) as session:
            assert extract._odata_valuations(session) == {}

    def test_no_valuation_table_means_no_price_not_an_error(self) -> None:
        with Session(create_engine("sqlite://")) as session:
            assert extract._odata_valuations(session) == {}

    def test_the_live_csv_mbew_without_a_price_sends_the_read_to_odata(self, monkeypatch) -> None:
        live_mbew = ["MANDT", "MATNR", "BWKEY", "BWTAR", "LVORM", "column_6", "BWPRS"]
        sentinel = {("x", "1300"): []}
        monkeypatch.setattr(extract, "_columns", lambda session, table: live_mbew)
        monkeypatch.setattr(extract, "_odata_valuations", lambda session, scope=frozenset(): sentinel)

        assert extract._valuations_by_plant(session=None) is sentinel


class TestMbewValuations:
    def test_sodium_cyanide_each_plant_reads_its_own_price_not_the_dormant_plants(self, monkeypatch) -> None:
        """Material 1000000053 as the live MBEW holds it. Staged per material, the highest
        price (7,689.26 from plants 1100 and 1200) went to every plant: ~500x too high."""
        monkeypatch.setattr(extract, "_columns", lambda session, table: MBEW_COLUMNS)
        rows = [
            ("1000000053", "1500", "", "15.45", "1"),
            ("1000000053", "1300", "", "14.58", "1"),
            ("1000000053", "1200", "", "7,689.26", "1"),
            ("1000000053", "1100", "", "7,689.26", "1"),
        ]
        with _mbew_session(rows) as session:
            # The view hands over plain numbers: "7,689.26" is the raw text, the view's is "7689.26".
            session.execute(text("UPDATE n_mbew SET moving_price = '7689.26' WHERE moving_price = '7,689.26'"))
            valuations = extract._valuations_by_plant(session)

        assert choose_unit_price(valuations[("1000000053", "1500")]).price == Decimal("15.4500")
        assert choose_unit_price(valuations[("1000000053", "1300")]).price == Decimal("14.5800")
        assert choose_unit_price(valuations.get(("1000000053", "3000"), [])).missing is PriceMissing.NOT_IN_MBEW

    def test_split_valuation_rows_stay_together_with_their_types(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: MBEW_COLUMNS)
        rows = [
            ("8000000001", "1300", "", "34840.62", "1"),
            ("8000000001", "1300", "NEW", "38122.00", "1"),
            ("8000000001", "1300", "REFURBISHD", "34840.62", "1"),
        ]
        with _mbew_session(rows) as session:
            valuations = extract._valuations_by_plant(session)

        choice = choose_unit_price(valuations[("8000000001", "1300")])
        assert choice.price == Decimal("38122.0000")
        assert choice.valuation_type == "NEW"

    def test_the_price_unit_is_applied_on_this_path_too(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: MBEW_COLUMNS)
        with _mbew_session([("5000000001", "1300", "", "250.00", "10")]) as session:
            valuations = extract._valuations_by_plant(session)

        assert choose_unit_price(valuations[("5000000001", "1300")]).price == Decimal("25.0000")

    def test_a_blank_price_unit_is_refused_when_the_column_exists(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: MBEW_COLUMNS)
        with _mbew_session([("5000000001", "1300", "", "250.00", "")]) as session:
            valuations = extract._valuations_by_plant(session)

        assert choose_unit_price(valuations[("5000000001", "1300")]).missing is PriceMissing.BAD_PRICE_UNIT

    def test_a_delivery_without_a_price_unit_column_means_one_unit(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: ["MATNR", "BWKEY", "BWTAR", "VERPR"])
        with _mbew_session([("5000000001", "1300", "", "250.00", None)]) as session:
            valuations = extract._valuations_by_plant(session)

        assert choose_unit_price(valuations[("5000000001", "1300")]).price == Decimal("250.0000")

    def test_a_plant_outside_the_scope_is_not_read(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: MBEW_COLUMNS)
        rows = [("5000000001", "1300", "", "10.00", "1"), ("5000000001", "1100", "", "99.00", "1")]
        with _mbew_session(rows) as session:
            valuations = extract._valuations_by_plant(session, frozenset({"1300"}))

        assert set(valuations) == {("5000000001", "1300")}

    def test_the_csv_mbew_states_no_currency_and_that_is_not_an_error(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: MBEW_COLUMNS)
        with _mbew_session([("5000000001", "1300", "", "10.00", "1")]) as session:
            valuations = extract._valuations_by_plant(session)

        choice = choose_unit_price(valuations[("5000000001", "1300")])
        assert choice.price == Decimal("10.0000")
        assert choice.currency is None
        assert choice.missing is None


    def test_waers_in_the_extract_is_staged_with_the_price(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: [*MBEW_COLUMNS, "WAERS"])
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE n_mbew (material TEXT, valuation_area TEXT, valuation_type TEXT, "
                "moving_price TEXT, price_unit TEXT, currency TEXT)"
            ))
            connection.execute(text(
                "INSERT INTO n_mbew VALUES ('5000000001', '1300', '', '250.00', '10', 'ZAR')"
            ))
        with Session(engine) as session:
            valuations = extract._valuations_by_plant(session)

        choice = choose_unit_price(valuations[("5000000001", "1300")])
        assert choice.price == Decimal("25.0000")
        assert choice.currency == "ZAR"

    def test_a_blank_waers_when_the_column_is_delivered_is_a_defect(self, monkeypatch) -> None:
        monkeypatch.setattr(extract, "_columns", lambda session, table: [*MBEW_COLUMNS, "WAERS"])
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(text(
                "CREATE TABLE n_mbew (material TEXT, valuation_area TEXT, valuation_type TEXT, "
                "moving_price TEXT, price_unit TEXT, currency TEXT)"
            ))
            connection.execute(text(
                "INSERT INTO n_mbew VALUES ('5000000001', '1300', '', '10.00', '1', '')"
            ))
        with Session(engine) as session:
            valuations = extract._valuations_by_plant(session)

        choice = choose_unit_price(valuations[("5000000001", "1300")])
        assert choice.price is None
        assert choice.missing is not None


def _marc(material: str, plant: str, lvorm: str | None = None) -> dict:
    return {"material": material, "plant": plant, "mrp_type": "PD", "planned_deliv_time": "14",
            "reorder_point": "10", "maximum_stock_level": "20", "df_at_plant_level": lvorm}


class TestPlantUnitPriceStaging:
    """What _stage_material_plants writes: the plant's price, or the reason there is none."""

    def _stage(self, monkeypatch, marc_rows, valuations, policy=None):
        rejections = _Rejections(1)
        helper = TestMaterialPlantStagingAppliesTheOdataConversion()
        staged = helper._staged_rows(
            monkeypatch, marc_rows, odata_present=True, odata_rows=[],
            valuations=valuations, rejections=rejections, policy=policy,
        )
        return staged, rejections

    def test_the_plants_price_valuation_type_and_currency_reach_the_staged_row(self, monkeypatch) -> None:
        valuations = {("8000000001", "1300"): [
            Valuation(None, Decimal("34840.62")),
            Valuation("NEW", Decimal("38122.00"), currency="ZAR"),
            Valuation("REFURBISHD", Decimal("34840.62")),
        ]}

        staged, rejections = self._stage(monkeypatch, [_marc("8000000001", "1300")], valuations)
        row = staged[("8000000001", "1300")]

        assert row["unit_price"] == Decimal("38122.00")
        assert row["currency"] == "ZAR"
        assert row["price_valuation_type"] == "NEW"
        assert row["price_missing_reason"] is None
        assert rejections.counts == {}

    def test_two_plants_of_one_material_stage_two_different_prices(self, monkeypatch) -> None:
        """The cyanide case end to end: not 7,689.26 at both, each plant its own."""
        valuations = {
            ("1000000053", "1500"): [Valuation(None, Decimal("15.45"))],
            ("1000000053", "1300"): [Valuation(None, Decimal("14.58"))],
            ("1000000053", "1200"): [Valuation(None, Decimal("7689.26"))],
        }

        staged, _ = self._stage(
            monkeypatch, [_marc("1000000053", "1500"), _marc("1000000053", "1300")], valuations
        )

        assert staged[("1000000053", "1500")]["unit_price"] == Decimal("15.45")
        assert staged[("1000000053", "1300")]["unit_price"] == Decimal("14.58")

    def test_a_placeholder_price_stages_none_with_the_reason_and_is_counted(self, monkeypatch) -> None:
        valuations = {("8000000002", "1300"): [Valuation("NEW", Decimal("0.01"))]}

        staged, rejections = self._stage(monkeypatch, [_marc("8000000002", "1300")], valuations)
        row = staged[("8000000002", "1300")]

        assert row["unit_price"] is None
        assert row["price_missing_reason"] == PriceMissing.TOKEN_VALUE.value
        assert rejections.counts == {PriceMissing.TOKEN_VALUE.value: 1}

    def test_a_material_plant_with_no_valuation_row_is_counted_not_priced_from_another_plant(self, monkeypatch) -> None:
        valuations = {("5000000003", "1500"): [Valuation(None, Decimal("200.00"))]}

        staged, rejections = self._stage(monkeypatch, [_marc("5000000003", "1300")], valuations)
        row = staged[("5000000003", "1300")]

        assert row["unit_price"] is None
        assert row["price_missing_reason"] == PriceMissing.NOT_IN_MBEW.value
        assert rejections.counts == {PriceMissing.NOT_IN_MBEW.value: 1}

    def test_a_material_plant_flagged_for_deletion_is_not_priced(self, monkeypatch) -> None:
        valuations = {("5000000004", "1300"): [Valuation(None, Decimal("200.00"))]}

        staged, rejections = self._stage(monkeypatch, [_marc("5000000004", "1300", lvorm="X")], valuations)
        row = staged[("5000000004", "1300")]

        assert row["deletion_flag"] is True
        assert row["unit_price"] is None
        assert row["price_missing_reason"] == PriceMissing.DELETED_MATERIAL_PLANT.value
        assert rejections.counts == {PriceMissing.DELETED_MATERIAL_PLANT.value: 1}

    def test_split_valuated_with_only_condition_rows_has_no_price(self, monkeypatch) -> None:
        valuations = {("8000000005", "1300"): [
            Valuation(None, Decimal("5000.00")), Valuation("DAMAGED", Decimal("100.00")),
        ]}

        staged, rejections = self._stage(monkeypatch, [_marc("8000000005", "1300")], valuations)

        assert staged[("8000000005", "1300")]["price_missing_reason"] == PriceMissing.NO_NEW_VALUATION.value
        assert rejections.counts == {PriceMissing.NO_NEW_VALUATION.value: 1}

    def test_the_affected_count_is_visible_by_reason(self, monkeypatch) -> None:
        """What a reviewer reads after a run: how many material-plants have no price, and why."""
        marc = [_marc(f"80000000{n:02d}", "1300") for n in range(1, 8)]
        valuations = {
            ("8000000001", "1300"): [Valuation("NEW", Decimal("900.00"))],            # priced
            ("8000000002", "1300"): [Valuation("NEW", Decimal("0.01"))],              # placeholder
            ("8000000003", "1300"): [Valuation("NEW", Decimal("1.00"))],              # placeholder
            ("8000000004", "1300"): [Valuation("SCRAP", Decimal("50.00"))],           # no NEW
            ("8000000005", "1300"): [Valuation(None, None, problem=PriceMissing.BAD_PRICE_UNIT)],
            # 06 and 07 have no row at all
        }

        staged, rejections = self._stage(monkeypatch, marc, valuations)

        assert rejections.counts == {
            PriceMissing.TOKEN_VALUE.value: 2,
            PriceMissing.NO_NEW_VALUATION.value: 1,
            PriceMissing.BAD_PRICE_UNIT.value: 1,
            PriceMissing.NOT_IN_MBEW.value: 2,
        }
        assert sum(1 for r in staged.values() if r["unit_price"] is not None) == 1

    def test_the_placeholder_floor_comes_from_the_policy(self, monkeypatch) -> None:
        from app.initiatives.i7.adapters.ingestion_policy import ExtractIngestionPolicy

        valuations = {("5000000006", "1300"): [Valuation(None, Decimal("5.00"))]}

        default, _ = self._stage(monkeypatch, [_marc("5000000006", "1300")], valuations)
        raised, _ = self._stage(
            monkeypatch, [_marc("5000000006", "1300")], valuations,
            policy=ExtractIngestionPolicy(price_token_max=Decimal("10")),
        )

        assert default[("5000000006", "1300")]["unit_price"] == Decimal("5.00")
        assert raised[("5000000006", "1300")]["unit_price"] is None

    def test_the_staged_price_is_never_the_highest_across_plants(self, monkeypatch) -> None:
        valuations = {
            ("1000000099", "1300"): [Valuation(None, Decimal("20.00"))],
            ("1000000099", "3000"): [Valuation(None, Decimal("20000.00"))],
        }

        staged, _ = self._stage(monkeypatch, [_marc("1000000099", "1300")], valuations)

        assert staged[("1000000099", "1300")]["unit_price"] == Decimal("20.00")


class TestPlantPriceReachesTheContract:
    def test_the_attributes_carry_the_plant_price_not_the_retired_material_price(self) -> None:
        from app.initiatives.i7.adapters.repository import _build_attributes
        from app.models.i7_staging import StagedMaterial, StagedMaterialPlant

        plant_row = StagedMaterialPlant(
            sap_material_number="1000000053", sap_plant_code="1300", mrp_type="PD",
            unit_price=Decimal("14.58"), currency="ZAR", deletion_flag=False,
        )
        material_row = StagedMaterial(
            sap_material_number="1000000053", description="Sodium cyanide",
            unit_price=Decimal("7689.26"), currency="XXX",  # the retired cross-plant price
        )

        attributes = _build_attributes(plant_row, material_row)

        assert attributes.unit_price == Decimal("14.58")
        assert attributes.currency == "ZAR"

    def test_a_plant_with_no_price_has_none_even_if_the_retired_column_holds_one(self) -> None:
        from app.initiatives.i7.adapters.repository import _build_attributes
        from app.models.i7_staging import StagedMaterial, StagedMaterialPlant

        plant_row = StagedMaterialPlant(
            sap_material_number="1000000053", sap_plant_code="1300", mrp_type="PD", unit_price=None,
            deletion_flag=False,
        )
        material_row = StagedMaterial(sap_material_number="1000000053", unit_price=Decimal("7689.26"))

        assert _build_attributes(plant_row, material_row).unit_price is None


class TestPlantScope:
    """I07 covers Gamsberg (1500) and Black Mountain (1300); the extract
    delivers thirteen plants. VZI confirmed the narrowing on 2026-10-07.

    The scope filters I07's *staging* only -- raw_*, the n_<table> views, I08,
    I13 and the assistant are deliberately untouched by it.
    """

    SCOPE = frozenset({"1500", "1300"})

    def test_an_empty_scope_admits_every_plant(self) -> None:
        """The default. An unset I7_PLANTS must not silently stage nothing."""
        assert extract._out_of_scope("3000", frozenset()) is False
        assert extract._out_of_scope("1300", frozenset()) is False

    def test_a_scoped_plant_is_kept_and_an_unscoped_one_is_not(self) -> None:
        assert extract._out_of_scope("1500", self.SCOPE) is False
        assert extract._out_of_scope("1300", self.SCOPE) is False
        assert extract._out_of_scope("3000", self.SCOPE) is True
        assert extract._out_of_scope("2000", self.SCOPE) is True

    def test_a_null_plant_is_left_for_the_caller_to_reject(self) -> None:
        """MISSING_PLANT is a data fault with its own rejection reason.

        Swallowing it here as "out of scope" would hide a malformed row behind
        a business decision and drop it from the rejection counts.
        """
        assert extract._out_of_scope(None, self.SCOPE) is False

    def test_a_non_numeric_plant_code_compares_as_a_string(self) -> None:
        """PDWB is a real plant in this extract, so the comparison may never
        become an integer one."""
        assert extract._out_of_scope("PDWB", self.SCOPE) is True
        assert extract._out_of_scope("PDWB", frozenset({"PDWB"})) is False

    def test_consumption_outside_the_scope_is_not_aggregated(self) -> None:
        rows = [
            _movement(material="M1", plant="1300", quantity="5"),
            _movement(material="M2", plant="3000", quantity="99"),
            _movement(material="M3", plant="1500", quantity="7"),
        ]
        totals = aggregate_consumption(rows, ISSUES, REVERSALS, _Rejections(1), self.SCOPE)

        plants = {plant for _, plant, _ in totals}
        assert plants == {"1300", "1500"}
        assert not any(m == "M2" for m, _, _ in totals)

    def test_consumption_with_no_scope_keeps_every_plant(self) -> None:
        rows = [_movement(material="M1", plant="1300"), _movement(material="M2", plant="3000")]
        totals = aggregate_consumption(rows, ISSUES, REVERSALS, _Rejections(1))

        assert {plant for _, plant, _ in totals} == {"1300", "3000"}

    def test_an_out_of_scope_row_is_not_counted_as_a_rejection(self) -> None:
        """Out of scope is a business boundary, not a data fault. Counting it
        would bury the real rejections under thousands of entries."""
        rejections = _Rejections(1)
        aggregate_consumption(
            [_movement(plant="3000"), _movement(plant="2000")],
            ISSUES, REVERSALS, rejections, self.SCOPE,
        )
        assert rejections.counts == {}

    def test_no_scope_means_no_material_restriction(self) -> None:
        """``None``, not an empty set: "not configured" and "matched nothing"
        are different, and the second is a mistake worth seeing."""
        assert extract._materials_in_plant_scope(session=None, scope=frozenset()) is None
