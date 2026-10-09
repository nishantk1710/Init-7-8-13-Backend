"""Is the raw layer a complete I07 snapshot? -- the resolver behind
``snapshot_complete``.

In-memory SQLite, because every question here is a SQL predicate over a few
rows. What is NOT proven: that a real sweep populates these columns the way the
fixtures do. That needs the VNet and is listed as a prerequisite.

The bias under test is deliberate and one-directional. A false negative costs a
day of dead rows staying in scope. A false positive runs the deactivation sweep
over a partial refresh and marks most of the catalogue inactive. So every test
that introduces doubt asserts False.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.initiatives.i7.snapshot import I7_CSV_TABLES, resolve_snapshot
from app.models.base import Base
from app.models.csv_extract import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    CsvExtractRequest,
)
from app.models.ingestion import IngestionRun

SWEEP = "S202610090100ab"
BASE = datetime(2026, 10, 9, 1, 0, tzinfo=timezone.utc)

#: Which tables reconcile exactly vs against a ceiling, from csv_tables.
EXACT = {"MARA", "MAKT", "MARC", "MARD", "MBEW"}


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine, tables=[CsvExtractRequest.__table__, IngestionRun.__table__]
    )
    with Session(engine) as session:
        yield session


def _request(
    session,
    table: str,
    *,
    sweep_id: str | None = SWEEP,
    status: str = STATUS_COMPLETE,
    expected: int | None = 100,
    received: int = 100,
    loaded: bool = True,
    max_rows: str = "",
    offset_minutes: int = 0,
):
    row = CsvExtractRequest(
        request_id=f"F{table[:4]}{offset_minutes:08d}",
        sap_table=table,
        entity_set=f"{table}Set",
        from_date="20230101",
        to_date="20261009",
        max_rows=max_rows,
        status=status,
        reconcile="exact" if table in EXACT else "bounded",
        expected_rows=expected,
        received_rows=received,
        sweep_id=sweep_id,
        fired_at=BASE + timedelta(minutes=offset_minutes),
        completed_at=BASE + timedelta(minutes=offset_minutes + 1),
        loaded_at=BASE + timedelta(minutes=offset_minutes + 2) if loaded else None,
    )
    session.add(row)
    session.commit()
    return row


def _enrichment(session, *, minutes_after: int = 30, status: str = "succeeded"):
    """A successful MaterialPlantSet load after the sweep."""
    at = BASE + timedelta(minutes=minutes_after)
    session.add(
        IngestionRun(
            source_file="ingest/ZMM_KPI02_ADD_SRV/MaterialPlantSet/2026-10-09",
            target_table="odata_material_plant",
            row_count=2000,
            status=status,
            started_at=at,
            finished_at=at,
        )
    )
    session.commit()


def _delta(session, table: str, *, minutes_after: int):
    """An OData delta merge into an I07 raw table."""
    at = BASE + timedelta(minutes=minutes_after)
    session.add(
        IngestionRun(
            source_file=f"odata:ingest/SRV/{table}/2026-10-09",
            target_table=table,
            row_count=12,
            status="succeeded",
            started_at=at,
            finished_at=at,
        )
    )
    session.commit()


def _complete_sweep(session, **overrides):
    """All ten tables, reconciled and loaded, plus the enrichment."""
    for index, table in enumerate(I7_CSV_TABLES):
        _request(session, table, offset_minutes=index, **overrides)
    _enrichment(session)


# --- The happy path -------------------------------------------------------


def test_a_complete_sweep_plus_enrichment_is_a_snapshot(session) -> None:
    _complete_sweep(session)

    verdict = resolve_snapshot(session)

    assert verdict.complete is True
    assert bool(verdict) is True
    assert verdict.sweep_id == SWEEP


def test_the_verdict_names_the_sweep_it_accepted(session) -> None:
    """Lineage: the pipeline records which sweep it judged complete."""
    _complete_sweep(session)

    verdict = resolve_snapshot(session)

    assert SWEEP in verdict.describe()
    assert verdict.completed_at is not None


# --- Missing evidence -----------------------------------------------------


class TestIncompleteEvidence:
    def test_an_empty_database_is_not_a_snapshot(self, session) -> None:
        verdict = resolve_snapshot(session)

        assert verdict.complete is False
        assert "no CSV sweep" in verdict.describe()

    @pytest.mark.parametrize("missing", I7_CSV_TABLES)
    def test_any_single_missing_table_disqualifies_the_sweep(
        self, session, missing
    ) -> None:
        for index, table in enumerate(I7_CSV_TABLES):
            if table != missing:
                _request(session, table, offset_minutes=index)
        _enrichment(session)

        verdict = resolve_snapshot(session)

        assert verdict.complete is False
        assert missing in verdict.describe()

    @pytest.mark.parametrize("bad_status", [STATUS_FAILED, STATUS_TIMEOUT])
    def test_a_failed_or_timed_out_table_disqualifies_the_sweep(
        self, session, bad_status
    ) -> None:
        for index, table in enumerate(I7_CSV_TABLES):
            status = bad_status if table == "MSEG" else STATUS_COMPLETE
            _request(session, table, status=status, offset_minutes=index)
        _enrichment(session)

        assert resolve_snapshot(session).complete is False

    def test_a_delivered_but_unloaded_table_disqualifies_the_sweep(
        self, session
    ) -> None:
        """In storage is not in SQL. Staging reads the database."""
        for index, table in enumerate(I7_CSV_TABLES):
            _request(session, table, loaded=(table != "EKPO"), offset_minutes=index)
        _enrichment(session)

        assert resolve_snapshot(session).complete is False


# --- Reconciliation -------------------------------------------------------


class TestReconciliation:
    def test_a_short_exact_table_disqualifies_the_sweep(self, session) -> None:
        """MARA reconciles exactly: a short file means chunks were lost."""
        for index, table in enumerate(I7_CSV_TABLES):
            short = table == "MARA"
            _request(
                session,
                table,
                received=90 if short else 100,
                offset_minutes=index,
            )
        _enrichment(session)

        verdict = resolve_snapshot(session)

        assert verdict.complete is False
        assert "MARA" in verdict.describe()

    def test_a_windowed_table_under_its_ceiling_is_fine(self, session) -> None:
        """MSEG is windowed by design, so $count is a ceiling, not a target."""
        for index, table in enumerate(I7_CSV_TABLES):
            windowed = table == "MSEG"
            _request(
                session,
                table,
                received=40 if windowed else 100,
                offset_minutes=index,
            )
        _enrichment(session)

        assert resolve_snapshot(session).complete is True

    def test_a_windowed_table_over_its_ceiling_disqualifies_the_sweep(
        self, session
    ) -> None:
        """More than the whole set means chunks were delivered twice."""
        for index, table in enumerate(I7_CSV_TABLES):
            over = table == "EKBE"
            _request(
                session, table, received=500 if over else 100, offset_minutes=index
            )
        _enrichment(session)

        assert resolve_snapshot(session).complete is False

    def test_a_table_with_no_count_disqualifies_the_sweep(self, session) -> None:
        """``csv_pull._verdict`` calls this COMPLETE and says completeness is
        "not proven". Not proven is what disqualifies it here."""
        for index, table in enumerate(I7_CSV_TABLES):
            unverifiable = table == "EKKO"
            _request(
                session,
                table,
                expected=None if unverifiable else 100,
                offset_minutes=index,
            )
        _enrichment(session)

        assert resolve_snapshot(session).complete is False

    def test_a_maxrows_probe_is_never_a_snapshot(self, session) -> None:
        """100 of 100 reconciles, and is still 100 rows of a 2,040-row table.
        Treating it as a snapshot would deactivate the other 1,940."""
        for index, table in enumerate(I7_CSV_TABLES):
            _request(session, table, max_rows="100", offset_minutes=index)
        _enrichment(session)

        assert resolve_snapshot(session).complete is False


# --- Sweep membership -----------------------------------------------------


class TestSweepMembership:
    """Membership is read from sweep_id and never inferred from timing."""

    def test_tables_from_two_sweeps_do_not_combine(self, session) -> None:
        """The failure this column exists to prevent: ten reconciled tables
        that were never fired together reading as one snapshot."""
        half = len(I7_CSV_TABLES) // 2
        for index, table in enumerate(I7_CSV_TABLES):
            _request(
                session,
                table,
                sweep_id=SWEEP if index < half else "S202610080100cd",
                offset_minutes=index,
            )
        _enrichment(session)

        assert resolve_snapshot(session).complete is False

    def test_requests_without_a_sweep_id_are_ignored(self, session) -> None:
        """A single-table --csv-pull is not a sweep of one."""
        for index, table in enumerate(I7_CSV_TABLES):
            _request(session, table, sweep_id=None, offset_minutes=index)
        _enrichment(session)

        verdict = resolve_snapshot(session)

        assert verdict.complete is False
        assert "no CSV sweep" in verdict.describe()

    def test_a_single_table_pull_beside_a_sweep_does_not_complete_it(
        self, session
    ) -> None:
        """Nine in the sweep, the tenth pulled separately minutes later. Any
        timestamp-proximity rule would wrongly accept this."""
        for index, table in enumerate(I7_CSV_TABLES[:-1]):
            _request(session, table, offset_minutes=index)
        _request(session, I7_CSV_TABLES[-1], sweep_id=None, offset_minutes=11)
        _enrichment(session)

        assert resolve_snapshot(session).complete is False

    def test_the_newest_qualifying_sweep_wins(self, session) -> None:
        """An older complete sweep must not mask a newer broken one -- the
        question is whether the raw layer is a snapshot NOW."""
        for index, table in enumerate(I7_CSV_TABLES):
            _request(session, table, sweep_id="S_old", offset_minutes=index)
        for index, table in enumerate(I7_CSV_TABLES):
            if table != "MARC":  # newer sweep is incomplete
                _request(
                    session,
                    table,
                    sweep_id="S_new",
                    offset_minutes=100 + index,
                )
        _enrichment(session, minutes_after=200)

        verdict = resolve_snapshot(session)

        assert verdict.complete is False
        assert verdict.sweep_id == "S_new"


# --- Enrichment -----------------------------------------------------------


class TestEnrichment:
    def test_a_sweep_without_the_enrichment_is_not_a_snapshot(self, session) -> None:
        """CSV MARC carries no DISMM/PLIFZ/MINBE/MABST at all, so without
        MaterialPlantSet the OAR rule has nothing to evaluate."""
        for index, table in enumerate(I7_CSV_TABLES):
            _request(session, table, offset_minutes=index)

        verdict = resolve_snapshot(session)

        assert verdict.complete is False
        assert "MaterialPlantSet" in verdict.describe()

    def test_enrichment_older_than_the_sweep_does_not_count(self, session) -> None:
        """Loaded before the sweep, it describes the previous extract."""
        for index, table in enumerate(I7_CSV_TABLES):
            _request(session, table, offset_minutes=index)
        _enrichment(session, minutes_after=-60)

        assert resolve_snapshot(session).complete is False

    def test_a_failed_enrichment_does_not_count(self, session) -> None:
        for index, table in enumerate(I7_CSV_TABLES):
            _request(session, table, offset_minutes=index)
        _enrichment(session, status="failed")

        assert resolve_snapshot(session).complete is False


# --- Deltas after the sweep -----------------------------------------------


class TestDeltasInvalidateTheSnapshot:
    """A snapshot plus unmerged increments is not a snapshot: rows the delta
    added were never in the sweep, and sweeping would deactivate them."""

    def test_a_delta_after_the_sweep_disqualifies_it(self, session) -> None:
        _complete_sweep(session)
        _delta(session, "raw_ekpo", minutes_after=60)

        verdict = resolve_snapshot(session)

        assert verdict.complete is False
        assert "raw_ekpo" in verdict.describe()

    def test_a_delta_before_the_sweep_is_harmless(self, session) -> None:
        """The CSV full pull REPLACES the table, so anything merged before it
        was overwritten."""
        _delta(session, "raw_ekpo", minutes_after=-120)
        _complete_sweep(session)

        assert resolve_snapshot(session).complete is True

    def test_a_csv_load_after_the_sweep_is_not_mistaken_for_a_delta(
        self, session
    ) -> None:
        """Only ``odata:``-prefixed rows are deltas; the CSV loader's own audit
        rows must not disqualify the sweep that wrote them."""
        _complete_sweep(session)
        at = BASE + timedelta(minutes=60)
        session.add(
            IngestionRun(
                source_file="csv:EKPO:FEKPO12345678",
                target_table="raw_ekpo",
                row_count=50,
                status="succeeded",
                started_at=at,
                finished_at=at,
            )
        )
        session.commit()

        assert resolve_snapshot(session).complete is True

    def test_the_enrichment_itself_is_not_treated_as_a_delta(self, session) -> None:
        """MaterialPlantSet lands AFTER the sweep by design -- it is part of the
        full refresh, so counting it as a delta would make every snapshot fail."""
        _complete_sweep(session)

        assert resolve_snapshot(session).complete is True
