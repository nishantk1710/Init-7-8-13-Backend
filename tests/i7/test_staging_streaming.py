"""Staging reads: streamed instead of fetched whole, scoped in SQL, same results.

The change under test is a memory fix. Movements, goods receipts and schedule
lines used to be fetched with ``.all()`` -- over a million rows apiece on the
full extract -- and the plant scope was applied afterwards, in Python. They are
now streamed into the aggregators that already folded them, and the plant scope
is pre-filtered in SQL.

So the one thing that must NOT change is the answer. Every test that touches
results compares the new path with the old one on the same rows, and the data
includes the cases a naive ``plant IN (...)`` gets wrong: a padded plant, a blank
and a NULL plant, and a malformed row at an out-of-scope plant (which is counted
today and must still be).

In-memory SQLite, with plain tables standing in for the ``n_<table>`` views. What
this does not prove: that the driver streams on Azure SQL, or the real peak
memory. Both need the VNet and are measured there.
"""

from __future__ import annotations

import inspect
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine.cursor import CursorResult
from sqlalchemy.engine.result import Result
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.initiatives.i7.adapters import extract
from app.initiatives.i7.adapters.extract import (
    _consumption_statement,
    _consumption_totals,
    _plant_scope_clause,
    _Rejections,
    _stage_consumption,
    _stage_purchase_orders,
    _stream,
    aggregate_consumption,
    receipts_by_line,
    schedule_dates_by_line,
)
from app.initiatives.i7.adapters.ingestion_policy import ExtractIngestionPolicy
from app.initiatives.i7.adapters.validation import RejectionReason
from app.models.base import Base
from app.models.i7_staging import StagedConsumption, StagedPurchaseOrder

SCOPE = ("1300", "1500")

DDL = [
    """CREATE TABLE n_mseg (material TEXT, plant TEXT, posting_date TEXT, quantity TEXT,
                           debit_credit_ind TEXT, movement_type TEXT, base_unit_of_measure TEXT)""",
    """CREATE TABLE n_ekbe (purchasing_document TEXT, item TEXT, posting_date TEXT,
                           quantity TEXT, po_history_category TEXT, movement_type TEXT)""",
    "CREATE TABLE n_eket (purchasing_document TEXT, item TEXT, delivery_date TEXT)",
    """CREATE TABLE n_ekpo (purchasing_document TEXT, item TEXT, material TEXT, plant TEXT,
                           order_quantity TEXT, planned_deliv_time TEXT, deletion_indicator TEXT)""",
    """CREATE TABLE n_ekko (purchasing_document TEXT, created_on TEXT, supplier TEXT,
                           deletion_indicator TEXT)""",
]


@pytest.fixture
def session():
    # StaticPool: every connection is the same in-memory database.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        for statement in DDL:
            connection.execute(text(statement))
    Base.metadata.create_all(
        engine, tables=[StagedConsumption.__table__, StagedPurchaseOrder.__table__]
    )
    with Session(engine) as session:
        yield session


def _insert(session, table: str, rows: list[dict]) -> None:
    for row in rows:
        columns = ", ".join(row)
        binds = ", ".join(f":{key}" for key in row)
        session.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({binds})"), row)
    session.commit()


def _movement(material, plant, posted, quantity="1", shkzg="H", bwart="201", unit="EA"):
    return dict(material=material, plant=plant, posting_date=posted, quantity=quantity,
                debit_credit_ind=shkzg, movement_type=bwart, base_unit_of_measure=unit)


def _movements(session) -> None:
    """Every case the plant scope can get wrong, plus ordinary ones."""
    _insert(session, "n_mseg", [
        _movement("M1", "1300", "2026-03-02", "5"),                      # issue
        _movement("M1", "1300", "2026-03-28", "2", shkzg="S", bwart="202"),  # reversal nets it
        _movement("M1", "1500", "2026-04-01", "3"),                      # second scoped plant
        _movement("M2", "9999", "2026-04-01", "7"),                      # out of scope
        _movement("M3", "1300 ", "2026-05-05", "4", bwart="261"),        # padded plant: IN scope
        _movement("M4", "", "2026-05-06"),                               # blank plant: rejected
        _movement("M5", None, "2026-05-06"),                             # NULL plant: rejected
        _movement("M6", "1300", "2026-05-07", bwart="101"),              # not a consumption type
        _movement("M7", "1300", "not-a-date"),                           # bad date: rejected
        _movement("M8", "1300", "2026-05-08", quantity="abc"),           # bad quantity: rejected
        _movement(None, "1300", "2026-05-09"),                           # no material: rejected
        _movement(None, "9999", "2026-05-09"),                           # no material, OUT of scope
        _movement("", "9999", "2026-05-09"),                             # blank material, OUT of scope
        _movement("M9", "9999", "not-a-date"),                           # bad date, out of scope: skipped
    ])


def _policy(plants=SCOPE) -> ExtractIngestionPolicy:
    return ExtractIngestionPolicy(plants=plants)


def _legacy_totals(session, policy):
    """The path this change replaced: fetch every row with ``.all()``, then
    aggregate, with the plant scope applied only in Python."""
    rejections = _Rejections(1)
    rows = session.execute(
        extract._CONSUMPTION_SQL, {"movement_types": list(policy.consumption.all_movement_types)}
    ).all()
    totals = aggregate_consumption(
        rows,
        frozenset(policy.consumption.issue_movement_types),
        frozenset(policy.consumption.reversal_movement_types),
        rejections,
        policy.plant_scope,
    )
    return totals, rejections.counts


# --- Consumption: same answer --------------------------------------------


class TestConsumptionIsUnchanged:
    def test_scoped_totals_equal_the_old_path(self, session) -> None:
        _movements(session)
        policy = _policy()

        old_totals, _ = _legacy_totals(session, policy)
        new_rejections = _Rejections(1)
        new_totals = _consumption_totals(session, policy, new_rejections)

        assert new_totals == old_totals
        assert new_totals  # the fixture is not vacuous

    def test_rejection_counts_equal_the_old_path(self, session) -> None:
        """The counts are what an operator reads to judge a run, so they have to
        survive the SQL filter untouched -- including the malformed rows at
        plants that are out of scope."""
        _movements(session)
        policy = _policy()

        _, old_counts = _legacy_totals(session, policy)
        new_rejections = _Rejections(1)
        _consumption_totals(session, policy, new_rejections)

        assert new_rejections.counts == old_counts
        assert old_counts  # something was rejected, or this proves nothing

    def test_the_expected_numbers(self, session) -> None:
        """Pins the fixture, so an equivalence test that went green by both
        sides being empty or wrong cannot hide behind itself."""
        _movements(session)

        totals = _consumption_totals(session, _policy(), _Rejections(1))

        assert totals[("M1", "1300", date(2026, 3, 1))]["quantity"] == Decimal("3")  # 5 - 2
        assert totals[("M1", "1500", date(2026, 4, 1))]["quantity"] == Decimal("3")
        assert totals[("M3", "1300", date(2026, 5, 1))]["quantity"] == Decimal("4")  # padded plant
        assert not any(plant == "9999" for _, plant, _ in totals)
        assert not any(material == "M6" for material, _, _ in totals)  # BWART 101

    def test_blank_and_null_plants_are_rejected_not_dropped(self, session) -> None:
        _movements(session)
        rejections = _Rejections(1)

        _consumption_totals(session, _policy(), rejections)

        assert rejections.counts[RejectionReason.MISSING_PLANT] == 2

    def test_malformed_rows_at_out_of_scope_plants_are_still_counted(self, session) -> None:
        """Python rejects a missing material BEFORE it looks at the plant, so a
        row with no material at plant 9999 is counted today. A SQL filter on the
        plant alone would drop it and the count would quietly change."""
        _movements(session)
        rejections = _Rejections(1)

        _consumption_totals(session, _policy(), rejections)

        # one at plant 1300, one NULL and one blank at plant 9999
        assert rejections.counts[RejectionReason.MISSING_MATERIAL] == 3

    def test_an_empty_scope_reads_every_plant(self, session) -> None:
        """No ``I7_PLANTS`` means no filter, in SQL as in Python."""
        _movements(session)
        policy = _policy(plants=())

        old_totals, _ = _legacy_totals(session, policy)
        new_totals = _consumption_totals(session, policy, _Rejections(1))

        assert new_totals == old_totals
        assert any(plant == "9999" for _, plant, _ in new_totals)

    def test_no_movements_at_all_is_an_empty_result_not_an_error(self, session) -> None:
        assert _consumption_totals(session, _policy(), _Rejections(1)) == {}
        assert _stage_consumption(session, 1, _policy(), _Rejections(1)) == 0


# --- Consumption: scoped in SQL and streamed ------------------------------


class TestConsumptionIsScopedAndStreamed:
    def _fetched(self, session, scope):
        params = {"movement_types": ["201", "202", "261", "262"]}
        if scope:
            params["plant_scope"] = sorted(scope)
        return list(_stream(session, _consumption_statement(frozenset(scope)), params))

    def test_the_plant_scope_is_applied_in_sql(self, session) -> None:
        """Fewer rows leave the database. The out-of-scope movement is not
        fetched at all, where before it was fetched and discarded."""
        _movements(session)

        scoped = self._fetched(session, SCOPE)
        everything = self._fetched(session, ())

        assert len(scoped) < len(everything)
        assert "9999" not in {row.plant for row in scoped if row.material not in (None, "")}

    def test_the_clause_is_absent_without_a_scope(self) -> None:
        assert _plant_scope_clause("plant", frozenset()) == ""

    def test_nothing_is_fetched_whole(self, session, monkeypatch) -> None:
        """The point of the change. If anything on the staging read path calls
        ``Result.all`` or ``fetchall`` again, this fails."""
        _movements(session)
        _insert(session, "n_ekbe", [dict(purchasing_document="45", item="10", posting_date="2026-02-01",
                                         quantity="5", po_history_category="E", movement_type="101")])
        _insert(session, "n_eket", [dict(purchasing_document="45", item="10", delivery_date="2026-01-15")])

        def forbidden(self, *args, **kwargs):
            raise AssertionError("a staging read fetched its whole result")

        monkeypatch.setattr(Result, "all", forbidden)
        monkeypatch.setattr(Result, "fetchall", forbidden)

        _consumption_totals(session, _policy(), _Rejections(1))
        receipts_by_line(
            _stream(session, extract._GOODS_RECEIPT_SQL,
                    {"gr_category": "E", "gr_movement": "101"}),
            _Rejections(1),
        )
        schedule_dates_by_line(_stream(session, extract._SCHEDULE_SQL))

    def test_rows_are_pulled_in_chunks(self, session, monkeypatch) -> None:
        _movements(session)
        chunks = []
        original = CursorResult.yield_per

        def spy(self, num):
            chunks.append(num)
            return original(self, num)

        monkeypatch.setattr(CursorResult, "yield_per", spy)
        monkeypatch.setattr(extract, "STREAM_CHUNK", 3)

        _consumption_totals(session, _policy(), _Rejections(1))

        assert chunks == [3]

    def test_the_stream_is_lazy(self, session) -> None:
        """A generator, so nothing is read until an aggregator asks."""
        stream = _stream(session, "SELECT 1")
        assert inspect.isgenerator(stream)
        stream.close()

    def test_the_result_is_closed_when_the_consumer_gives_up(self, session, monkeypatch) -> None:
        """An aggregator that raises part-way must not leave a result open: on
        SQL Server that would make the next statement on the connection fail
        with "Connection is busy"."""
        _movements(session)
        opened = []
        original = CursorResult.yield_per

        def spy(self, num):
            opened.append(self)
            return original(self, num)

        monkeypatch.setattr(CursorResult, "yield_per", spy)
        stream = _stream(session, "SELECT material FROM n_mseg")
        next(stream)

        stream.close()

        assert opened and opened[0].closed


class TestConsumptionStagingIsIdempotent:
    def test_running_it_twice_does_not_duplicate_rows(self, session) -> None:
        _movements(session)

        first = _stage_consumption(session, 1, _policy(), _Rejections(1))
        session.commit()
        second = _stage_consumption(session, 2, _policy(), _Rejections(2))
        session.commit()

        stored = session.execute(select(func.count()).select_from(StagedConsumption)).scalar()
        assert first == second == stored
        runs = {r for (r,) in session.execute(select(StagedConsumption.staging_run_id)).all()}
        assert runs == {2}  # every row re-stamped by the run that wrote it last


# --- Purchase orders -------------------------------------------------------


def _purchase_orders(session) -> None:
    _insert(session, "n_ekko", [
        dict(purchasing_document=d, created_on="2026-01-01", supplier="V1", deletion_indicator="")
        for d in ("4500000001", "4500000002", "4500000003", "4500000004")
    ])
    _insert(session, "n_ekpo", [
        dict(purchasing_document="4500000001", item="10", material="M1", plant="1300",
             order_quantity="5", planned_deliv_time="10", deletion_indicator=""),
        dict(purchasing_document="4500000002", item="10", material="M2", plant="9999",
             order_quantity="5", planned_deliv_time="10", deletion_indicator=""),     # out of scope
        dict(purchasing_document="4500000003", item="10", material="M3", plant=" 1500",
             order_quantity="5", planned_deliv_time="10", deletion_indicator=""),     # padded: in scope
        dict(purchasing_document="4500000004", item="10", material="M4", plant="",
             order_quantity="5", planned_deliv_time="10", deletion_indicator=""),     # blank plant
        dict(purchasing_document="", item="20", material="M5", plant="9999",
             order_quantity="5", planned_deliv_time="10", deletion_indicator=""),     # no document, OUT of scope
        dict(purchasing_document="4500000001", item="", material="M6", plant="9999",
             order_quantity="5", planned_deliv_time="10", deletion_indicator=""),     # no item, OUT of scope
        dict(purchasing_document="4500000001", item="30", material="", plant="1300",
             order_quantity="5", planned_deliv_time="10", deletion_indicator=""),     # no material: not selected
    ])
    _insert(session, "n_ekbe", [
        dict(purchasing_document="4500000001", item="10", posting_date="2026-01-20",
             quantity="5", po_history_category="E", movement_type="101"),
        dict(purchasing_document="4500000001", item="10", posting_date="not-a-date",
             quantity="1", po_history_category="E", movement_type="101"),
        dict(purchasing_document="4500000001", item="10", posting_date="2026-01-21",
             quantity="1", po_history_category="Q", movement_type="101"),  # wrong category
        dict(purchasing_document="4500000003", item="10", posting_date="2026-02-10",
             quantity="2", po_history_category="E", movement_type="101"),
    ])
    _insert(session, "n_eket", [
        dict(purchasing_document="4500000001", item="10", delivery_date="2026-01-18"),
        dict(purchasing_document="4500000001", item="10", delivery_date="2026-01-12"),
    ])


def _staged_orders(session):
    rows = session.execute(select(StagedPurchaseOrder)).scalars().all()
    return sorted(
        (r.purchasing_document, r.item, r.sap_material_number, r.sap_plant_code,
         r.lead_time_days, r.quantity_received, r.planned_delivery_date)
        for r in rows
    )


class TestPurchaseOrdersAreUnchanged:
    def test_the_scoped_run_stages_what_the_unscoped_run_would_have(
        self, session, monkeypatch
    ) -> None:
        """Run once the new way, clear, run again forcing the old unscoped SQL,
        and compare what was staged and what was rejected."""
        _purchase_orders(session)
        policy = _policy()

        new_rejections = _Rejections(1)
        new_count = _stage_purchase_orders(session, 1, policy, new_rejections)
        session.commit()
        new_rows = _staged_orders(session)

        session.execute(delete(StagedPurchaseOrder))
        session.commit()
        monkeypatch.setattr(
            extract, "_purchase_order_statement", lambda scope: text(extract._PURCHASE_ORDER_SQL)
        )
        old_rejections = _Rejections(1)
        old_count = _stage_purchase_orders(session, 1, policy, old_rejections)
        session.commit()
        old_rows = _staged_orders(session)

        assert new_rows == old_rows
        assert new_count == old_count
        assert new_rejections.counts == old_rejections.counts
        assert new_rows  # not vacuous

    def test_the_expected_orders(self, session) -> None:
        _purchase_orders(session)

        _stage_purchase_orders(session, 1, _policy(), _Rejections(1))
        session.commit()

        staged = {(d, i): (m, p) for d, i, m, p, *_ in _staged_orders(session)}
        assert staged[("4500000001", "10")] == ("M1", "1300")
        assert staged[("4500000003", "10")] == ("M3", "1500")  # padded plant, cleaned
        assert ("4500000002", "10") not in staged               # plant 9999
        assert ("4500000004", "10") not in staged               # blank plant -> rejected

    def test_malformed_lines_at_out_of_scope_plants_are_still_counted(self, session) -> None:
        _purchase_orders(session)
        rejections = _Rejections(1)

        _stage_purchase_orders(session, 1, _policy(), rejections)

        assert rejections.counts[RejectionReason.MISSING_PURCHASING_DOCUMENT] == 2  # no document, no item
        assert rejections.counts[RejectionReason.MISSING_PLANT] == 1

    def test_receipts_and_schedule_streams_equal_the_whole_read(self, session) -> None:
        _purchase_orders(session)
        params = {"gr_category": "E", "gr_movement": "101"}

        streamed = receipts_by_line(
            _stream(session, extract._GOODS_RECEIPT_SQL, params), _Rejections(1)
        )
        whole = receipts_by_line(
            extract._read(session, extract._GOODS_RECEIPT_SQL, params), _Rejections(1)
        )
        assert streamed == whole
        assert streamed[("4500000001", "10")] == (date(2026, 1, 20), Decimal("5"))

        assert schedule_dates_by_line(_stream(session, extract._SCHEDULE_SQL)) == (
            schedule_dates_by_line(extract._read(session, extract._SCHEDULE_SQL))
        )

    def test_the_purchase_order_statement_is_scoped_only_with_a_scope(self) -> None:
        assert "plant_scope" in str(extract._purchase_order_statement(frozenset(SCOPE)))
        assert "plant_scope" not in str(extract._purchase_order_statement(frozenset()))


# --- Isolation -------------------------------------------------------------


def test_nothing_outside_i07_imports_the_staging_adapter() -> None:
    """The reason this change cannot reach I08 or I13: neither imports the code
    that changed. They read the shared n_<table> views, which this leaves alone.
    """
    pattern = re.compile(r"^\s*(from|import)\s+app\.initiatives\.i7\.adapters", re.MULTILINE)
    root = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "initiatives" + "/i7" not in path.as_posix() and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
