"""Numbers written plain at the door (CSV_LOAD_NUMBER_FORMAT), and sets a
sweep leaves out (INGEST_DISABLED_SETS).

Both exist because of what PRD showed on 6 Nov 2026: its CSV extract writes
German numbers while its OData services send plain ones, so no single view
rule could read both -- and its gate-pass extract delivers nothing while the
hourly delta would still re-read three sets nothing consumes.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings, get_settings
from app.ingest import csv_load
from app.ingest.csv_tables import CSV_TABLES, enabled_tables
from app.ingest.manifest import spec_for, specs


@pytest.fixture
def settings_env(monkeypatch):
    """Set env for Settings and clear the cache, restoring both afterwards."""

    def apply(**values: str) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


# --- plain_number ------------------------------------------------------------


class TestPlainNumber:
    @pytest.mark.parametrize(
        "value, expected",
        [
            ("1.393,100", "1393.100"),
            ("6.979.168,890", "6979168.890"),
            ("0,00", "0.00"),
            ("0,989", "0.989"),          # the order of the two replaces matters here
            ("1.000", "1000"),           # grouping only, no decimal part
            ("12,5-", "12.5-"),          # SAP's trailing minus is kept
            ("-12,5", "-12.5"),
            ("5", "5"),                  # a plain integer has nothing to change
        ],
    )
    def test_german_becomes_plain(self, value, expected) -> None:
        assert csv_load.plain_number(value, "german") == expected

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("2,490.00", "2490.00"),
            ("37,897.000", "37897.000"),
            ("0.00", "0.00"),
            ("1,000", "1000"),
            ("2.5-", "2.5-"),
        ],
    )
    def test_us_becomes_plain(self, value, expected) -> None:
        assert csv_load.plain_number(value, "us") == expected

    @pytest.mark.parametrize("value", ["", "   ", "20261105", "ABC", "1,2,3x", "clear lagacy"])
    def test_what_is_not_a_number_is_left_alone(self, value) -> None:
        # Dates, codes and text that happen to sit in a number column must not
        # be rewritten: "20261105" is a day, not twenty million.
        assert csv_load.plain_number(value, "german") == value
        assert csv_load.plain_number(value, "us") == value

    def test_none_stays_none(self) -> None:
        assert csv_load.plain_number(None, "german") is None

    def test_an_unknown_format_changes_nothing(self) -> None:
        assert csv_load.plain_number("1.393,100", "") == "1.393,100"
        assert csv_load.plain_number("1.393,100", "plain") == "1.393,100"

    def test_already_plain_is_unchanged_under_us(self) -> None:
        # What the OData delta writes, and what a converted table holds: the
        # US rule must be a no-op on it, or a second pass would corrupt it.
        assert csv_load.plain_number("4437980.33", "us") == "4437980.33"
        assert csv_load.plain_number("930.521", "us") == "930.521"


# --- number_columns ----------------------------------------------------------


class TestNumberColumns:
    def test_only_the_columns_the_view_reads_as_numbers(self) -> None:
        header = ["MBLNR", "MJAHR", "ZEILE", "BWART", "MATNR", "MENGE", "MEINS", "DMBTR", "BUDAT_MKPF"]
        positions = csv_load.number_columns("mseg", header)
        assert [header[i] for i in positions] == ["MENGE", "DMBTR"]

    def test_case_of_the_header_does_not_matter(self) -> None:
        assert csv_load.number_columns("mseg", ["Menge", "dmbtr", "matnr"]) == [0, 1]

    def test_a_table_the_normalise_layer_does_not_know_has_none(self) -> None:
        assert csv_load.number_columns("zmm_gp_hdr", ["ZZGP_NO", "ZZQUANISS"]) == []

    def test_plain_rows_touches_only_those_positions(self) -> None:
        rows = [("4900000001", "1.393,100", "X", "4.437.980,33"), ("4900000002", "", "Y", "0,00")]
        out = list(csv_load.plain_rows(iter(rows), [1, 3], "german"))
        assert out == [("4900000001", "1393.100", "X", "4437980.33"), ("4900000002", "", "Y", "0.00")]

    def test_plain_rows_with_nothing_to_do_passes_rows_through(self) -> None:
        rows = [("a", "1,5"), ("b", "2,5")]
        assert list(csv_load.plain_rows(iter(rows), [], "german")) == rows


# --- number_settings_problem --------------------------------------------------


class TestNumberSettings:
    def _settings(self, **values) -> Settings:
        return Settings(_env_file=None, **values)

    def test_unset_means_store_as_sent_and_is_always_fine(self) -> None:
        assert csv_load.number_settings_problem(self._settings(csv_number_format="german")) is None
        assert csv_load.number_settings_problem(self._settings(csv_number_format="us")) is None

    def test_plain_storage_needs_the_us_view_rule(self) -> None:
        # The German view rule strips every dot: a plain 1393.100 would read
        # back as 1393100. The pair is refused before anything is written.
        problem = csv_load.number_settings_problem(
            self._settings(csv_load_number_format="german", csv_number_format="german")
        )
        assert problem is not None and "CSV_NUMBER_FORMAT" in problem

    def test_the_intended_pair_is_fine(self) -> None:
        assert csv_load.number_settings_problem(
            self._settings(csv_load_number_format="german", csv_number_format="us")
        ) is None

    def test_an_unknown_load_format_is_refused(self) -> None:
        problem = csv_load.number_settings_problem(
            self._settings(csv_load_number_format="plain", csv_number_format="us")
        )
        assert problem is not None and "CSV_LOAD_NUMBER_FORMAT" in problem


# --- INGEST_DISABLED_SETS ----------------------------------------------------


GATE_PASS = ("GatePassHeaderSet", "GatePassItemSet", "GatePassReturnSet")


class TestDisabledSets:
    def test_gate_pass_is_off_by_default(self, settings_env) -> None:
        settings_env()
        assert get_settings().ingest_disabled_set_list == frozenset(GATE_PASS)

    def test_a_disabled_set_has_no_runnable_delta_and_says_why(self, settings_env) -> None:
        settings_env(INGEST_DISABLED_SETS="GatePassHeaderSet")
        spec = spec_for("GatePassHeaderSet")
        assert spec.disabled
        assert spec.runnable_delta is None
        assert spec.why_not_runnable == "disabled by INGEST_DISABLED_SETS"

    def test_emptying_the_list_re_enables_everything(self, settings_env, monkeypatch) -> None:
        from app.ingest import manifest as manifest_mod

        settings_env(INGEST_DISABLED_SETS="")
        monkeypatch.setattr(manifest_mod, "READ_BROKEN_SETS", {})
        for name in GATE_PASS:
            spec = spec_for(name)
            assert not spec.disabled
            assert spec.runnable_delta is not None, name
        assert len(enabled_tables()) == len(CSV_TABLES)

    def test_sweeps_leave_the_csv_tables_behind_a_disabled_set_out(self, settings_env) -> None:
        settings_env(INGEST_DISABLED_SETS="GatePassHeaderSet,GatePassItemSet,GatePassReturnSet")
        names = {t.sap_table for t in enabled_tables()}
        assert names == {t.sap_table for t in CSV_TABLES} - {"ZMM_GP_HDR", "ZMM_GP_ITEM", "ZMM_GP_IN"}
        assert all(not s.disabled for s in specs() if s.name not in GATE_PASS)

    def test_whitespace_and_blank_entries_are_tolerated(self, settings_env) -> None:
        settings_env(INGEST_DISABLED_SETS=" GatePassHeaderSet , ,VendorSet ")
        assert get_settings().ingest_disabled_set_list == frozenset({"GatePassHeaderSet", "VendorSet"})
        assert spec_for("VendorSet").disabled
