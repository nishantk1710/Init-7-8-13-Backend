"""The CSV full-pull route: request shape, windows, and the completeness gate.

What is covered here is what has no other safety net. The request string is
tested character by character because four details in it are load-bearing and
each was learned by firing a request that acknowledged and delivered nothing.
The completeness verdict is tested because it is the only thing standing
between a short file and the serving layer.

Nothing here touches CPI, storage or a database.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.ingest import csv_pull
from app.ingest.csv_tables import (
    BY_TABLE,
    CSV_TABLES,
    TRANSACTION_WINDOW_YEARS,
    Reconcile,
    csv_table,
)
from app.models.csv_extract import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    CsvExtractRequest,
)


# --- The request string -----------------------------------------------------


class TestExtractPath:
    """The shape SAP actually answered. Every assertion here is a measurement."""

    def test_matches_the_call_that_delivered(self) -> None:
        assert csv_pull.extract_path(
            "EKPO114002", "EKPO", "20130101", "20131231", ""
        ) == (
            "sap/opu/odata/SAP/ZMM_GET_CSV_SRV/TableExtractSet("
            "RequestId='EKPO114002',TabName='EKPO',"
            "FromDate='20130101',ToDate='20131231',"
            "IsDelta='',MaxRows='')/$value"
        )

    def test_the_service_segment_is_upper_case(self) -> None:
        """/SAP/ here, against /sap/ everywhere else in this codebase.

        The working call used upper case and nothing has tested the other form.
        """
        path = csv_pull.extract_path("R1", "MARA", "19000101", "20270101")
        assert "/SAP/ZMM_GET_CSV_SRV/" in path
        assert "/sap/ZMM_GET_CSV_SRV/" not in path

    def test_dates_are_bare_dats_not_odata_literals(self) -> None:
        """These are key predicates. datetime'...' belongs to $filter."""
        path = csv_pull.extract_path("R1", "MKPF", "20230101", "20260101")
        assert "FromDate='20230101'" in path
        assert "datetime" not in path

    def test_it_asks_for_the_value_stream(self) -> None:
        """GET_ENTITY and GET_ENTITYSET both answer 501 on this service."""
        assert csv_pull.extract_path("R1", "MARA", "1", "2").endswith("/$value")

    def test_is_delta_is_sent_empty(self) -> None:
        """The field exists and may work. A full pull is not where to find out."""
        assert "IsDelta=''" in csv_pull.extract_path("R1", "MARA", "1", "2")

    def test_max_rows_is_carried_when_given(self) -> None:
        assert "MaxRows='100'" in csv_pull.extract_path("R1", "EKPO", "1", "2", "100")


class TestRequestId:
    def test_every_id_is_unique(self) -> None:
        """SAP dedupes on it: a reused id acknowledges and delivers nothing."""
        ids = {csv_pull.new_request_id("EKPO") for _ in range(200)}
        assert len(ids) == 200

    def test_it_fits_the_column(self) -> None:
        assert len(csv_pull.new_request_id("MaterialDocument")) <= 32


# --- Windows ----------------------------------------------------------------


class TestWindows:
    def test_transaction_tables_get_three_years(self) -> None:
        start, end = csv_table("EKPO").window(date(2026, 9, 25))
        assert start == "20230925"
        assert end == "20260925"
        assert 2026 - int(start[:4]) == TRANSACTION_WINDOW_YEARS

    def test_master_tables_get_everything(self) -> None:
        """Three years of MARA is not a meaningful request: a material created
        in 2009 and still stocked today has to be in the dimension."""
        start, end = csv_table("MARA").window(date(2026, 9, 25))
        assert start == "19000101"
        assert end > "20260925", "the window must reach past today"

    def test_no_table_ever_sends_a_blank_date(self) -> None:
        """Every request we fired with blank dates delivered nothing."""
        for spec in CSV_TABLES:
            start, end = spec.window(date(2026, 9, 25))
            assert start and end, f"{spec.sap_table} produced a blank date"
            assert len(start) == 8 and len(end) == 8

    def test_dates_are_chronological(self) -> None:
        for spec in CSV_TABLES:
            start, end = spec.window(date(2026, 9, 25))
            assert start < end, f"{spec.sap_table} window runs backwards"


# --- The table register -----------------------------------------------------


class TestTableRegister:
    def test_all_21_sets_are_covered(self) -> None:
        assert len(CSV_TABLES) == 21

    def test_every_entity_set_is_named_once(self) -> None:
        sets = [t.entity_set for t in CSV_TABLES]
        assert len(set(sets)) == len(sets)

    def test_csv_lands_beside_the_seed_not_on_the_odata_tables(self) -> None:
        """EKPO is 277 columns here and 19 over OData. One shared table would
        mean the delta nulling 258 columns on every row it touched."""
        assert csv_table("EKPO").raw_table == "raw_ekpo"
        assert not csv_table("EKPO").raw_table.startswith("odata_")

    def test_windowed_tables_reconcile_loosely_and_master_exactly(self) -> None:
        assert csv_table("MKPF").reconcile is Reconcile.BOUNDED
        assert csv_table("MARA").reconcile is Reconcile.EXACT

    def test_lookup_is_case_insensitive(self) -> None:
        assert csv_table("ekpo") is BY_TABLE["EKPO"]

    def test_an_unknown_table_says_what_is_known(self) -> None:
        with pytest.raises(KeyError, match="EKPO"):
            csv_table("NOSUCHTABLE")


# --- The completeness gate --------------------------------------------------


def request(**kwargs) -> CsvExtractRequest:
    defaults = dict(
        request_id="R1",
        sap_table="EKPO",
        entity_set="PurchaseOrderItemSet",
        from_date="20230101",
        to_date="20260101",
        max_rows="",
        reconcile="bounded",
        expected_rows=1000,
        received_rows=500,
        received_chunks=1,
    )
    return CsvExtractRequest(**{**defaults, **kwargs})


class TestVerdict:
    """What decides whether a landed file reaches Azure SQL."""

    def test_a_whole_table_extract_must_match_the_count_exactly(self) -> None:
        verdict, _ = csv_pull._verdict(
            request(reconcile="exact", expected_rows=2183, received_rows=2183)
        )
        assert verdict == STATUS_COMPLETE

    def test_a_short_whole_table_extract_is_refused(self) -> None:
        """The failure this gate exists for: a lost chunk reads as a whole file."""
        verdict, detail = csv_pull._verdict(
            request(reconcile="exact", expected_rows=2183, received_rows=1200)
        )
        assert verdict == STATUS_FAILED
        assert "1200" in detail and "2183" in detail

    def test_a_windowed_extract_may_be_smaller_than_the_set(self) -> None:
        """Three years of MKPF is a subset by design, so the count is a ceiling."""
        verdict, _ = csv_pull._verdict(
            request(reconcile="bounded", expected_rows=40651, received_rows=9000)
        )
        assert verdict == STATUS_COMPLETE

    def test_more_rows_than_the_table_holds_is_refused(self) -> None:
        """A window cannot return more than the whole set -- chunks arrived twice."""
        verdict, detail = csv_pull._verdict(
            request(reconcile="bounded", expected_rows=40651, received_rows=81302)
        )
        assert verdict == STATUS_FAILED
        assert "twice" in detail

    def test_chunks_carrying_no_rows_are_refused(self) -> None:
        verdict, detail = csv_pull._verdict(request(received_rows=0))
        assert verdict == STATUS_FAILED
        assert "no data rows" in detail

    def test_no_count_loads_but_says_it_is_unproven(self) -> None:
        """Several sets answer HTTP 500 to $count while serving rows fine. That
        weakens the check to "something arrived", which is recorded, not hidden.
        """
        verdict, detail = csv_pull._verdict(
            request(expected_rows=None, received_rows=500)
        )
        assert verdict == STATUS_COMPLETE
        assert "unverified" in detail


# --- Landing keys -----------------------------------------------------------


class TestLandingKeys:
    """Where chunks are assembled. Two bugs lived here; both lost rows."""

    def test_keys_are_scoped_to_the_request_not_the_day(self, monkeypatch) -> None:
        """A date-keyed path split an extract running across midnight UTC into
        two files. The row tally counted every chunk, so reconciliation passed
        while the loader read only the first file -- a short load reporting
        success, which is the one failure this route exists to prevent.
        """
        from app.ingest import csv_receipt

        monkeypatch.setattr(csv_receipt, "_open_request_id", lambda table: "EKPOABC123")
        data_key, header_key = csv_receipt.landing_keys("EKPO")

        assert data_key == "csv/EKPO/EKPOABC123/EKPO.csv"
        assert header_key == "csv/EKPO/EKPOABC123/_header.csv"

    def test_two_requests_for_one_table_never_share_a_file(self, monkeypatch) -> None:
        """A retry must carry a new RequestId because SAP dedupes on it, so a
        date key would have merged two requests into one indistinguishable file.
        """
        from app.ingest import csv_receipt

        monkeypatch.setattr(csv_receipt, "_open_request_id", lambda table: "FIRST")
        first, _ = csv_receipt.landing_keys("EKPO")
        monkeypatch.setattr(csv_receipt, "_open_request_id", lambda table: "SECOND")
        second, _ = csv_receipt.landing_keys("EKPO")

        assert first != second

    def test_a_chunk_with_nothing_open_is_still_kept(self, monkeypatch) -> None:
        """Unattributable bytes are real bytes. Stored and flagged, not dropped."""
        from app.ingest import csv_receipt

        monkeypatch.setattr(csv_receipt, "_open_request_id", lambda table: None)
        data_key, _ = csv_receipt.landing_keys("EKPO")

        assert data_key.startswith("csv/EKPO/unattributed-")

    def test_everything_lands_under_one_prefix(self, monkeypatch) -> None:
        """STORAGE_URL points at the landing container root."""
        from app.ingest import csv_receipt

        monkeypatch.setattr(csv_receipt, "_open_request_id", lambda table: "R1")
        for table in ("EKPO", "MARA", "CDPOS"):
            assert csv_receipt.landing_keys(table)[0].startswith("csv/")


# --- What the timer actually runs -------------------------------------------


class TestScheduledScope:
    def test_only_sets_with_a_delta_are_scheduled(self) -> None:
        """Fifteen of twenty-one have no delta, and in delta mode each falls
        back to a FULL pull. ChangeDocItemSet is 939,970 rows; an hourly timer
        would have pulled all of them every hour. The CSV route covers those.
        """
        from app.ingest.manifest import specs

        schedulable = [s for s in specs() if s.delta is not None]

        assert 0 < len(schedulable) < len(specs())
        assert all(s.delta is not None for s in schedulable)

    def test_the_sets_the_timer_skips_are_covered_by_the_csv_route(self) -> None:
        """Skipping them is only safe because the other path reaches them."""
        from app.ingest.csv_tables import BY_ENTITY_SET
        from app.ingest.manifest import specs

        skipped = [s.name for s in specs() if s.delta is None]
        uncovered = [name for name in skipped if name not in BY_ENTITY_SET]

        assert not uncovered, f"no route reaches {uncovered}"


class TestCappedVerdict:
    """MaxRows is a probe. Judging it against the whole table calls a working
    delivery broken -- which is how every master table got reported as failed
    on the first live sweep."""

    def test_a_capped_master_table_is_not_failed_for_being_capped(self) -> None:
        verdict, detail = csv_pull._verdict(
            request(sap_table="MARA", reconcile="exact",
                    max_rows="100", expected_rows=2040, received_rows=100)
        )
        assert verdict == STATUS_COMPLETE
        assert "probe" in detail and "2,040" in detail

    def test_more_than_the_cap_is_still_refused(self) -> None:
        verdict, detail = csv_pull._verdict(
            request(max_rows="100", expected_rows=2040, received_rows=250)
        )
        assert verdict == STATUS_FAILED
        assert "twice" in detail

    def test_an_uncapped_pull_still_reconciles_exactly(self) -> None:
        verdict, _ = csv_pull._verdict(
            request(sap_table="MARA", reconcile="exact",
                    max_rows="", expected_rows=2040, received_rows=1200)
        )
        assert verdict == STATUS_FAILED


class TestWindowOverride:
    """Three years is right for production and near-empty in this client:
    99.3% of its purchase orders predate 2026. The window has to be steerable
    or a full pull asks SAP for a period the data does not occupy."""

    def test_explicit_dates_win_over_everything(self) -> None:
        assert csv_table("EKPO").window(
            date(2026, 9, 25), from_date="20130101", to_date="20181231"
        ) == ("20130101", "20181231")

    def test_explicit_dates_override_master_tables_too(self) -> None:
        assert csv_table("MARA").window(
            date(2026, 9, 25), from_date="20130101", to_date="20181231"
        ) == ("20130101", "20181231")

    def test_years_widens_the_span_without_pinning_the_end(self) -> None:
        start, end = csv_table("EKPO").window(date(2026, 9, 25), years=15)
        assert start == "20110925"
        assert end == "20260925"

    def test_the_default_is_unchanged_when_nothing_is_passed(self) -> None:
        assert csv_table("EKPO").window(date(2026, 9, 25)) == ("20230925", "20260925")

    def test_a_half_given_override_is_ignored_not_half_applied(self) -> None:
        """A start with no end must fall back to the computed window rather
        than pairing a real start with a missing end."""
        assert csv_table("EKPO").window(
            date(2026, 9, 25), from_date="20130101"
        ) == ("20230925", "20260925")


class TestTableIdentification:
    """Every header here was copied from SAP's live delivery on 25-Sep.

    The first signature table was written from memory of SAP's key structure
    and got four tables wrong: MBEW's rows were appended into MARA's file and
    MCHB's into MARD's, because a prefix match falls into whichever shorter
    signature a table happens to begin with. CDHDR and CDPOS matched nothing at
    all -- they spell the client column MANDANT, not MANDT.
    """

    OBSERVED = {
        "MARA": ["MANDT", "MATNR", "ERSDA", "ERNAM", "LAEDA"],
        "MAKT": ["MANDT", "MATNR", "SPRAS", "MAKTX", "MAKTG"],
        "MARC": ["MANDT", "MATNR", "WERKS", "", "UMLMC"],
        "MARD": ["MANDT", "MATNR", "WERKS", "LGORT", "PSTAT"],
        "MBEW": ["MANDT", "MATNR", "BWKEY", "BWTAR", "LVORM"],
        "MCHB": ["MANDT", "MATNR", "WERKS", "LGORT", "CHARG"],
        "EINA": ["MANDT", "INFNR", "MATNR", "MATKL", "LIFNR"],
        "EINE": ["MANDT", "INFNR", "EKORG", "ESOKZ", "WERKS"],
        "LFA1": ["MANDT", "LIFNR", "LAND1", "NAME1", "NAME2"],
        "S031": ["MANDT", "SSOUR", "VRSIO", "SPMON", "SPTAG"],
        "S032": ["MANDT", "SSOUR", "VRSIO", "WERKS", "LGORT"],
        "EKPO": ["MANDT", "EBELN", "EBELP", "LOEKZ", "STATU"],
        "EKBE": ["MANDT", "EBELN", "EBELP", "ZEKKN", "VGABE"],
        "EBAN": ["MANDT", "BANFN", "BNFPO", "BSART", "BSTYP"],
        "MKPF": ["MANDT", "MBLNR", "MJAHR", "VGART", "BLART"],
        "MSEG": ["MANDT", "MBLNR", "MJAHR", "ZEILE", "LINE_ID"],
        "RESB": ["MANDT", "RSNUM", "RSPOS", "RSART", "BDART"],
        "CDHDR": ["MANDANT", "OBJECTCLAS", "OBJECTID", "CHANGENR", "USERNAME"],
        "CDPOS": ["MANDANT", "OBJECTCLAS", "OBJECTID", "CHANGENR", "TABNAME"],
    }

    @pytest.mark.parametrize("table", sorted(OBSERVED))
    def test_the_live_header_resolves_to_its_own_table(self, table: str) -> None:
        from app.api.events.csv_upload import table_of

        assert table_of(self.OBSERVED[table]) == table

    def test_no_two_tables_resolve_to_the_same_file(self) -> None:
        """The failure mode, stated directly: two tables sharing one file."""
        from app.api.events.csv_upload import table_of

        resolved = [table_of(h) for h in self.OBSERVED.values()]
        assert len(set(resolved)) == len(resolved)

    def test_change_documents_are_not_missed_for_spelling_mandant(self) -> None:
        from app.api.events.csv_upload import table_of

        assert not table_of(self.OBSERVED["CDHDR"]).startswith("UNKNOWN")
        assert not table_of(self.OBSERVED["CDPOS"]).startswith("UNKNOWN")


class TestRequestIdLength:
    """$metadata says MaxLength=20. Live SAP disagrees.

    Measured 25-Sep: ids of 16 characters acknowledged and delivered nothing,
    every time; ids of 9 to 11 delivered in seconds, every time. This was the
    single cause of every failed CSV pull that day, and it surfaces as a
    15-minute timeout rather than an error, so nothing points at it.
    """

    def test_ids_stay_under_the_measured_working_limit(self) -> None:
        from app.ingest.csv_pull import REQUEST_ID_MAX, new_request_id

        for table in ("MARA", "EKPO", "CDPOS", "MaterialDocumentHeaderSet"):
            assert len(new_request_id(table)) <= REQUEST_ID_MAX

    def test_the_limit_is_below_every_observed_failure(self) -> None:
        from app.ingest.csv_pull import REQUEST_ID_MAX

        assert REQUEST_ID_MAX < 16, "16-character ids delivered nothing"

    def test_ids_are_still_unique(self) -> None:
        """Short is no use if two fires collide -- SAP dedupes on this."""
        from app.ingest.csv_pull import new_request_id

        assert len({new_request_id("EKPO") for _ in range(5000)}) == 5000
