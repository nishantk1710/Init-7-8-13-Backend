"""How current the I08 screens say they are.

Two numbers, pointing opposite ways on purpose:

* the load time takes the OLDEST source load -- everything is at least this
  fresh;
* the coverage watermark takes the NEWEST activity -- nothing after this has
  reached us.

Each is the bound that cannot overstate freshness, and the tests below are
mostly about keeping them pointing the right way.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.initiatives.i8 import service

UTC = timezone.utc


class FakeRows:
    """What ``db.execute(select(...))`` hands back."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class FakeDb:
    """A session that answers the two queries freshness needs, and nothing else."""

    def __init__(self, *, runs: list[tuple] | None = None, maxima: dict | None = None) -> None:
        self.runs = runs or []
        self.maxima = maxima or {}
        self.scalar_calls: list[str] = []

    def execute(self, statement):
        sql = str(statement)
        if "ingestion_run" in sql:
            return FakeRows(self.runs)
        self.scalar_calls.append(sql)
        value = self.maxima.get(sql)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(scalar=lambda: value)


def at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 11, day, hour, 0, tzinfo=UTC)


class TestSourceState:
    """One grouped query over ingestion_run, read two ways."""

    def _state(self, runs: list[tuple]) -> service.SourceState:
        return service.source_state(FakeDb(runs=runs), reference_date=date(2026, 11, 27))

    def test_reports_the_oldest_load_as_well_as_the_newest(self) -> None:
        state = self._state(
            [("raw_ekpo", 2, at(27)), ("raw_resb", 1, at(3)), ("raw_mara", 3, at(27))]
        )
        # The screen shows the oldest: raw_resb stopped loading on the 3rd, and
        # a screen saying "updated the 27th" would be wrong about the register.
        assert state.oldest_loaded_at == at(3)
        assert state.loaded_at == at(27)

    def test_lists_every_source_table_oldest_first(self) -> None:
        state = self._state([("raw_ekpo", 2, at(27)), ("raw_resb", 1, at(3))])
        named = [t.table for t in state.per_table]
        assert named[0] == "raw_resb", "the laggard is the row worth reading"
        assert set(named) == set(service.SOURCE_TABLES), "every source is accounted for"

    def test_a_table_that_never_loaded_sorts_last_not_first(self) -> None:
        # None is not "very old": prod has never had a ZMM065 report uploaded,
        # and sorting it as the oldest would bury the table that is genuinely
        # behind underneath one nobody has ever filled.
        state = self._state([("raw_ekpo", 2, at(27)), ("raw_resb", 1, at(3))])
        assert state.per_table[0].table == "raw_resb"
        assert state.per_table[-1].loaded_at is None

    def test_a_never_loaded_table_does_not_erase_the_answer(self) -> None:
        state = self._state([("raw_ekpo", 1, at(27))])
        assert state.oldest_loaded_at == at(27), "one unfilled table must not blank the chip"

    def test_nothing_loaded_at_all_is_none_rather_than_a_guess(self) -> None:
        state = self._state([])
        assert state.oldest_loaded_at is None
        assert state.loaded_at is None


class TestActivityCoverage:
    """The newest thing that actually happened, not the newest date present."""

    def _coverage(self, maxima: dict, *, today: date = date(2026, 11, 27)) -> date | None:
        keyed = {f"select max({c}) from {v}": maxima.get(v) for v, c in service.ACTIVITY_DATES}
        return service.activity_coverage(FakeDb(maxima=keyed), reference_date=today)

    def test_takes_the_newest_posting_across_the_activity_views(self) -> None:
        assert self._coverage(
            {"v_ekko": date(2026, 11, 5), "v_eban": date(2026, 11, 2), "v_mseg": date(2026, 11, 5)}
        ) == date(2026, 11, 5)

    def test_reads_only_creations_and_postings(self) -> None:
        # Measured on the production copy: EKET.EINDT reaches 2030-09-30 and
        # RESB.BDTER 2028-04-30, because both are dates something is WANTED by.
        # Neither may be in this list, or the screen claims to cover 2030.
        views = {view for view, _ in service.ACTIVITY_DATES}
        assert views == {"v_ekko", "v_eban", "v_mseg"}
        assert "v_eket" not in views and "v_resb" not in views

    def test_a_date_in_the_future_is_a_fault_not_coverage(self) -> None:
        assert self._coverage(
            {"v_ekko": date(2030, 9, 30), "v_eban": date(2026, 11, 5), "v_mseg": None}
        ) == date(2026, 11, 5)

    def test_one_unreadable_view_still_leaves_the_others_bounding_it(self) -> None:
        assert self._coverage(
            {"v_ekko": SQLAlchemyError("no such view"), "v_eban": date(2026, 11, 5), "v_mseg": None}
        ) == date(2026, 11, 5)

    def test_nothing_readable_is_none_rather_than_today(self) -> None:
        assert self._coverage({"v_ekko": None, "v_eban": None, "v_mseg": None}) is None


class TestFreshness:
    """What the screens are told, with and without a snapshot."""

    @pytest.fixture(autouse=True)
    def _clean_state(self):
        before = (service._state.snapshot, service._state.building_since)
        yield
        service._state.snapshot, service._state.building_since = before

    def test_describes_the_snapshot_on_screen_not_the_raw_layer_now(self) -> None:
        # A reload since the build describes data nobody is looking at yet.
        service._state.snapshot = SimpleNamespace(
            reference_date=date(2026, 11, 27),
            source_oldest_loaded_at=at(3),
            source_loaded_at=at(27),
            source_tables=(service.TableLoad("raw_resb", at(3)),),
            coverage_to=date(2026, 11, 5),
            built_at=at(27, 11),
        )
        service._state.building_since = None
        state = service.freshness(FakeDb(runs=[("raw_ekpo", 9, at(27))]))
        assert state.loaded_at == at(3)
        assert state.coverage_to == date(2026, 11, 5)
        assert state.built_at == at(27, 11)

    def test_answers_during_the_first_build_instead_of_going_blank(self) -> None:
        # The route takes no snapshot dependency precisely so this works: a
        # cold start is when somebody most wants to know why the screen is bare.
        service._state.snapshot = None
        service._state.building_since = at(27, 11)
        state = service.freshness(FakeDb(runs=[("raw_ekpo", 1, at(27)), ("raw_resb", 2, at(3))]))
        assert state.built_at is None
        assert state.building_since == at(27, 11)
        assert state.loaded_at == at(3), "load times are known before any build"
        assert state.coverage_to is None, "coverage is measured during the build"
