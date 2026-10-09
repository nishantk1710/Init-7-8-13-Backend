"""Phase A: the staging gate, run pinning, and soft deactivation.

No database server. The three behaviours under test are all decided by SQL
predicates over a handful of rows, so an in-memory SQLite database proves them
exactly as a server would -- and proves them on a laptop, which matters here
because Azure SQL is unreachable outside VZI's VNet and these are the assertions
that must not quietly stop being checked.

What is deliberately NOT claimed by this file: that the sweep is correct against
the real extract. That needs volume and a real snapshot, and is listed in the
end-to-end plan instead.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.initiatives.i7.adapters.extract import deactivate_unseen
from app.initiatives.i7.features.builder import (
    StagingNotReady,
    observation_window,
    resolve_staging_run,
)
from app.models.base import Base
from app.models.i7_staging import (
    StagedConsumption,
    StagedMaterialPlant,
    StagedStock,
    StagingRun,
)

SUCCEEDED = "succeeded"
FAILED = "failed"
RUNNING = "running"


@pytest.fixture
def session():
    """An empty staging schema, per test."""
    engine = create_engine("sqlite://")
    Base.metadata.create_all(
        engine,
        tables=[
            StagingRun.__table__,
            StagedMaterialPlant.__table__,
            StagedStock.__table__,
            StagedConsumption.__table__,
        ],
    )
    with Session(engine) as session:
        yield session


def _run(session, status: str, *, snapshot: bool = False) -> StagingRun:
    run = StagingRun(
        source="normalise_views",
        status=status,
        snapshot_complete=snapshot,
        finished_at=datetime.now(timezone.utc) if status != RUNNING else None,
    )
    session.add(run)
    session.commit()
    return run


def _material_plant(session, run_id: int, material="4000000123", plant="1300"):
    row = StagedMaterialPlant(
        sap_material_number=material,
        sap_plant_code=plant,
        source_table="n_marc",
        staging_run_id=run_id,
    )
    session.add(row)
    session.commit()
    return row


def _consumption(session, run_id: int, period: date, material="4000000123", plant="1300"):
    row = StagedConsumption(
        sap_material_number=material,
        sap_plant_code=plant,
        period=period,
        quantity=1,
        movement_count=1,
        issue_count=1,
        reversal_count=0,
        source_table="n_mseg",
        staging_run_id=run_id,
    )
    session.add(row)
    session.commit()
    return row


# --- The gate -------------------------------------------------------------


class TestResolveStagingRun:
    """Which staging run a feature build is allowed to read, if any."""

    def test_the_newest_succeeded_run_is_chosen(self, session) -> None:
        _run(session, SUCCEEDED)
        newest = _run(session, SUCCEEDED)

        assert resolve_staging_run(session).id == newest.id

    def test_a_failed_run_blocks_nothing_but_is_never_chosen(self, session) -> None:
        """A failed run's partial rows stay in the table -- staging upserts and
        does not roll back -- so the protection is pinning to the succeeded run,
        not the absence of the rows."""
        good = _run(session, SUCCEEDED)
        _run(session, FAILED)

        assert resolve_staging_run(session).id == good.id

    def test_a_running_run_refuses_the_build(self, session) -> None:
        """The defect this whole phase exists for: staging commits in batches,
        so a run still in flight has rows visible to this session that are only
        half of what it will write."""
        _run(session, SUCCEEDED)
        _run(session, RUNNING)

        with pytest.raises(StagingNotReady, match="still running"):
            resolve_staging_run(session)

    def test_no_succeeded_run_at_all_refuses_the_build(self, session) -> None:
        _run(session, FAILED)

        with pytest.raises(StagingNotReady, match="no staging run has succeeded"):
            resolve_staging_run(session)

    def test_an_empty_table_refuses_rather_than_returning_none(self, session) -> None:
        """Raising, not returning None: a caller that treated None as "nothing
        to do" would report a successful build of zero features."""
        with pytest.raises(StagingNotReady):
            resolve_staging_run(session)


# --- Run pinning ----------------------------------------------------------


class TestObservationWindow:
    """The window sets ``n`` for every series through densification, so a stale
    row does not merely add itself -- it lengthens every material's history."""

    def test_the_window_is_scoped_to_the_given_run(self, session) -> None:
        old = _run(session, SUCCEEDED)
        new = _run(session, SUCCEEDED)
        _consumption(session, old.id, date(2020, 1, 1))
        _consumption(session, new.id, date(2026, 3, 1))
        _consumption(session, new.id, date(2026, 5, 1))

        assert observation_window(session, new.id) == (date(2026, 3, 1), date(2026, 5, 1))

    def test_without_a_run_the_window_spans_everything(self, session) -> None:
        """The unscoped form is kept for callers that genuinely mean "everything
        staged"; it is what the pre-Phase-A behaviour was."""
        old = _run(session, SUCCEEDED)
        new = _run(session, SUCCEEDED)
        _consumption(session, old.id, date(2020, 1, 1))
        _consumption(session, new.id, date(2026, 5, 1))

        assert observation_window(session) == (date(2020, 1, 1), date(2026, 5, 1))

    def test_a_run_that_staged_nothing_has_no_window(self, session) -> None:
        run = _run(session, SUCCEEDED)

        assert observation_window(session, run.id) is None


# --- Deactivation ---------------------------------------------------------


class TestDeactivateUnseen:
    """Soft deactivation, and the delta/snapshot distinction that gates it."""

    def test_a_row_absent_from_the_new_run_is_deactivated(self, session) -> None:
        old = _run(session, SUCCEEDED, snapshot=True)
        new = _run(session, SUCCEEDED, snapshot=True)
        gone = _material_plant(session, old.id, material="4000000111")
        kept = _material_plant(session, new.id, material="4000000222")

        assert deactivate_unseen(session, new.id) == 1
        session.commit()

        session.refresh(gone)
        session.refresh(kept)
        assert gone.is_active is False
        assert kept.is_active is True

    def test_the_row_is_never_deleted(self, session) -> None:
        """Soft, so the row and its history survive a mistaken deactivation."""
        old = _run(session, SUCCEEDED, snapshot=True)
        new = _run(session, SUCCEEDED, snapshot=True)
        _material_plant(session, old.id)

        deactivate_unseen(session, new.id)
        session.commit()

        remaining = session.execute(
            select(func.count()).select_from(StagedMaterialPlant)
        ).scalar()
        assert remaining == 1

    def test_a_returning_row_is_reactivated(self, session) -> None:
        """The upsert cannot do this: ``_updatable`` builds its column list from
        the staged dictionaries, which do not carry ``is_active``. Without the
        explicit reactivation a material that came back would be re-staged with
        a current ``staging_run_id`` and stay invisible forever."""
        old = _run(session, SUCCEEDED, snapshot=True)
        new = _run(session, SUCCEEDED, snapshot=True)
        row = _material_plant(session, old.id)

        deactivate_unseen(session, new.id)
        session.commit()
        session.refresh(row)
        assert row.is_active is False

        # The next snapshot carries it again: staging rewrites staging_run_id.
        row.staging_run_id = new.id
        session.commit()
        deactivate_unseen(session, new.id)
        session.commit()

        session.refresh(row)
        assert row.is_active is True

    def test_stock_rows_are_swept_too(self, session) -> None:
        """The feature universe is the UNION of material-plant and stock, so a
        stock row left active would hold a deactivated material-plant in
        scope."""
        old = _run(session, SUCCEEDED, snapshot=True)
        new = _run(session, SUCCEEDED, snapshot=True)
        stock = StagedStock(
            sap_material_number="4000000123",
            sap_plant_code="1300",
            storage_location="0001",
            source_table="n_mard",
            staging_run_id=old.id,
        )
        session.add(stock)
        session.commit()

        assert deactivate_unseen(session, new.id) == 1
        session.commit()

        session.refresh(stock)
        assert stock.is_active is False

    def test_consumption_is_never_swept(self, session) -> None:
        """A movement from March is not retracted by its absence from a June
        extract -- the extract simply does not reach back that far."""
        old = _run(session, SUCCEEDED, snapshot=True)
        new = _run(session, SUCCEEDED, snapshot=True)
        _consumption(session, old.id, date(2026, 3, 1))

        deactivate_unseen(session, new.id)
        session.commit()

        kept = session.execute(
            select(func.count()).select_from(StagedConsumption)
        ).scalar()
        assert kept == 1
        assert not hasattr(StagedConsumption, "is_active")

    def test_already_inactive_rows_are_not_recounted(self, session) -> None:
        """The count stored on the run row answers "what did THIS extract
        drop?", so a row dropped two runs ago must not inflate it."""
        first = _run(session, SUCCEEDED, snapshot=True)
        second = _run(session, SUCCEEDED, snapshot=True)
        third = _run(session, SUCCEEDED, snapshot=True)
        _material_plant(session, first.id)

        assert deactivate_unseen(session, second.id) == 1
        session.commit()
        assert deactivate_unseen(session, third.id) == 0


class TestSweepIsGatedOnSnapshotCompleteness:
    """The decision recorded on the run, not re-derived downstream."""

    def test_a_delta_run_records_that_it_may_not_sweep(self, session) -> None:
        """Absence from a delta means "unchanged". Sweeping on one would
        deactivate the whole catalogue bar the rows that happened to move."""
        run = _run(session, SUCCEEDED, snapshot=False)

        assert run.snapshot_complete is False
        assert run.deactivated == 0

    def test_snapshot_completeness_defaults_to_false(self, session) -> None:
        """Not knowing must never sweep."""
        run = StagingRun(source="normalise_views", status=SUCCEEDED)
        session.add(run)
        session.commit()

        assert run.snapshot_complete is False


class TestInactiveRowsAreExcludedFromEveryRead:
    """Two separate filters, because they answer two separate questions."""

    def _stock(self, session, run_id, location, quantity, active=True):
        row = StagedStock(
            sap_material_number="4000000123",
            sap_plant_code="1300",
            storage_location=location,
            unrestricted_use_stock=quantity,
            is_active=active,
            source_table="n_mard",
            staging_run_id=run_id,
        )
        session.add(row)
        session.commit()
        return row

    def test_stock_from_a_deactivated_location_is_not_summed(self, session) -> None:
        """The universe CTE decides whether a material-plant EXISTS; the stock
        aggregate decides what its stock IS. A material-plant can be entirely
        current while one of its MARD rows left the snapshot, and summing that
        dead location back in overstates on-hand stock -- which is the figure
        ROP is compared against.
        """
        run = _run(session, SUCCEEDED, snapshot=True)
        self._stock(session, run.id, "0001", 10, active=True)
        self._stock(session, run.id, "0002", 999, active=False)

        total, locations = session.execute(
            select(
                func.sum(StagedStock.unrestricted_use_stock),
                func.count(),
            )
            .select_from(StagedStock)
            .where(StagedStock.is_active.is_(True))
        ).one()

        assert total == 10
        assert locations == 1

    def test_the_builders_stock_sql_carries_the_filter(self) -> None:
        """Asserted on the SQL text because the query runs against a real
        database and this file deliberately has none. A regression here is
        silent: the sum is still a number, just a wrong one."""
        from app.initiatives.i7.features.builder import _ATTRIBUTE_SQL, _STOCK_SQL

        # The WHERE clause, not the bare literal: both modules explain the
        # filter in a comment above it, and counting those would pass whether
        # or not the predicate survived.
        assert "WHERE is_active = 1" in _STOCK_SQL
        assert _ATTRIBUTE_SQL.count("WHERE is_active = 1") == 2


# --- Lineage --------------------------------------------------------------


def test_the_fingerprint_travels_with_the_run(session) -> None:
    """"Which ingestion refresh produced this?" has to be answerable from the
    run row alone, without re-deriving anything."""
    run = StagingRun(
        source="normalise_views",
        status=SUCCEEDED,
        snapshot_complete=True,
        source_fingerprint="raw_marc:41:2026-10-09T01:00:00+00:00",
    )
    session.add(run)
    session.commit()

    stored = session.execute(select(StagingRun)).scalars().one()
    assert stored.source_fingerprint == "raw_marc:41:2026-10-09T01:00:00+00:00"
