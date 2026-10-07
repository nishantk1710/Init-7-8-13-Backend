"""The opener's material search -- by number or by name.

No database. Each source the search reads (MAKT, the two ZMM065 reports, MARC,
MARD) is a list of rows on a fake session, which answers a query by the table it
names and applies the query's own LIKE / IN parameters. That is enough to test
what the search does with what comes back -- precedence, normalising, ranking,
scope -- without pretending to test SQL Server.

The flow predicates themselves are not re-tested here (see ``test_router.py``
for why). What is tested is that the hint uses the same precedence the router
does, so the badge and the session can never disagree.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import ProgrammingError

from app.assistant.material_search import parse_query, search
from app.assistant.router import Flow
from app.core.db import get_db
from app.main import app


@dataclass(frozen=True)
class _Makt:
    material: str
    material_description: str
    language_key: str = "E"


@dataclass(frozen=True)
class _Zmm:
    mat_code: str
    plant: str
    material_description: str


@dataclass(frozen=True)
class _Marc:
    material: str
    plant: str
    mrp_type: str | None = None


@dataclass(frozen=True)
class _Mard:
    material: str
    plant: str


def _like(value: str, pattern: str) -> bool:
    regex = "^" + re.escape(pattern).replace("%", ".*") + "$"
    return re.match(regex, value or "", flags=re.IGNORECASE) is not None


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchall(self) -> list:
        return self._rows

    def fetchmany(self, size: int) -> list:
        return self._rows[:size]


@dataclass
class _FakeDb:
    makt: list[_Makt] = field(default_factory=list)
    zmm_bmm: list[_Zmm] | None = field(default_factory=list)
    zmm_gb: list[_Zmm] | None = field(default_factory=list)
    marc: list[_Marc] = field(default_factory=list)
    mard: list[_Mard] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)

    def rollback(self) -> None:
        pass

    def execute(self, query, params=None):  # noqa: ANN001 - mirrors Session.execute
        sql = str(query)
        params = params or {}
        self.queries.append(sql)
        words = [v for k, v in params.items() if k.startswith("w")]
        in_scope = lambda plant: plant in ("1300", "1500")  # noqa: E731

        if "FROM n_makt" in sql:
            rows = self.makt
            if "number_like" in params:
                rows = [r for r in rows if _like(r.material, params["number_like"])]
            return _Result([r for r in rows if all(_like(r.material_description, w) for w in words)])

        for table, rows in (("raw_zmm065_bmm", self.zmm_bmm), ("raw_zmm065_gb", self.zmm_gb)):
            if f"FROM {table}" in sql:
                if rows is None:
                    raise ProgrammingError(sql, params, Exception("no such table"))
                rows = [r for r in rows if in_scope(r.plant)]
                if "number_like" in params:
                    rows = [r for r in rows if _like(r.mat_code, params["number_like"])]
                return _Result([r for r in rows if all(_like(r.material_description, w) for w in words)])

        for table, rows in (("n_marc", self.marc), ("n_mard", self.mard)):
            if f"FROM {table}" in sql:
                rows = [r for r in rows if in_scope(r.plant)]
                if "prefix" in params:
                    rows = [r for r in rows if _like(r.material, params["prefix"])]
                if "materials" in params:
                    rows = [r for r in rows if r.material in params["materials"]]
                return _Result(rows)

        raise AssertionError(f"unexpected query: {sql}")


class TestReadingTheQuery:
    def test_digits_are_a_number_prefix_without_leading_zeros(self) -> None:
        assert parse_query("0000008000").number_prefix == "8000"

    def test_spaces_and_dashes_inside_a_number_are_ignored(self) -> None:
        assert parse_query("8000 005-632").number_prefix == "8000005632"

    def test_text_becomes_upper_case_words(self) -> None:
        assert parse_query("slurry  pump, impeller").words == ("SLURRY", "PUMP", "IMPELLER")

    def test_wildcards_never_reach_the_pattern(self) -> None:
        assert parse_query("pu%mp_").words == ("PU", "MP")

    @pytest.mark.parametrize("raw", ["", " ", "p", None])
    def test_too_short_is_nothing(self, raw) -> None:  # noqa: ANN001
        assert parse_query(raw).empty


class TestByName:
    def test_every_word_must_match(self) -> None:
        db = _FakeDb(
            makt=[_Makt("1000000123", "IMPELLER, SLURRY PUMP"), _Makt("1000000999", "PUMP SEAL")],
            marc=[_Marc("1000000123", "1300", "PD"), _Marc("1000000999", "1300", "PD")],
        )
        found = search(db, "slurry pump")
        assert [m.material_id for m in found] == ["1000000123"]

    def test_a_description_found_only_in_zmm065_still_matches(self) -> None:
        """ZMM065 names most repair materials MAKT does not."""
        db = _FakeDb(
            zmm_bmm=[_Zmm("000000008000005632", "1300", "PUMP, CENTRIFUGAL")],
            marc=[_Marc("8000005632", "1300", "ND")],
        )
        [match] = search(db, "centrifugal")
        assert match.material_id == "8000005632"
        assert match.description == "PUMP, CENTRIFUGAL"

    def test_makt_english_text_wins_over_other_languages_and_the_report(self) -> None:
        db = _FakeDb(
            makt=[
                _Makt("8000005632", "PUMPE ZENTRIFUGAL", language_key="D"),
                _Makt("8000005632", "PUMP CENTRIFUGAL", language_key="E"),
            ],
            zmm_bmm=[_Zmm("8000005632", "1300", "PUMP CENTRIF (REPORT)")],
            marc=[_Marc("8000005632", "1300")],
        )
        [match] = search(db, "pump")
        assert match.description == "PUMP CENTRIFUGAL"

    def test_a_missing_report_table_is_skipped_not_an_error(self) -> None:
        db = _FakeDb(
            makt=[_Makt("8000005632", "PUMP CENTRIFUGAL")],
            zmm_bmm=None,
            zmm_gb=None,
            marc=[_Marc("8000005632", "1300")],
        )
        assert [m.material_id for m in search(db, "pump")] == ["8000005632"]


class TestByNumber:
    def test_a_prefix_matches_materials_with_no_description_at_all(self) -> None:
        db = _FakeDb(marc=[_Marc("8000005632", "1300", "ND")])
        [match] = search(db, "800000")
        assert match.material_id == "8000005632"
        assert match.description is None

    def test_a_padded_number_finds_the_short_form(self) -> None:
        db = _FakeDb(marc=[_Marc("8000005632", "1300")])
        assert [m.material_id for m in search(db, "000000008000005632")] == ["8000005632"]

    def test_the_exact_number_comes_first(self) -> None:
        db = _FakeDb(
            marc=[_Marc("80000056321", "1300"), _Marc("8000005632", "1300")],
        )
        assert search(db, "8000005632")[0].material_id == "8000005632"


class TestPlants:
    def test_one_row_per_plant(self) -> None:
        db = _FakeDb(
            makt=[_Makt("8000005632", "PUMP CENTRIFUGAL")],
            marc=[_Marc("8000005632", "1300")],
            mard=[_Mard("8000005632", "1500")],
        )
        assert sorted(m.plant for m in search(db, "pump")) == ["1300", "1500"]

    def test_gamsberg_is_found_without_a_marc_row(self) -> None:
        """MARC has no 1500 rows in the July extract."""
        db = _FakeDb(zmm_gb=[_Zmm("8000005632", "1500", "PUMP CENTRIFUGAL")])
        [match] = search(db, "pump")
        assert (match.plant, match.plant_name) == ("1500", "Gamsberg")

    def test_out_of_scope_plants_are_never_suggested(self) -> None:
        db = _FakeDb(
            makt=[_Makt("8000005632", "PUMP CENTRIFUGAL")],
            marc=[_Marc("8000005632", "1200")],
            mard=[_Mard("8000005632", "2000")],
        )
        assert search(db, "pump") == []


class TestTheFlowHint:
    def test_eighty_series_is_i08_even_when_also_oar(self) -> None:
        """The router's precedence: I08 wins."""
        db = _FakeDb(marc=[_Marc("8000005632", "1300", "ND")])
        assert search(db, "8000005632")[0].flow_hint is Flow.I08

    def test_oar_by_mrp_type_is_i13(self) -> None:
        db = _FakeDb(marc=[_Marc("1000000123", "1300", "PD")])
        assert search(db, "1000000123")[0].flow_hint is Flow.I13

    def test_without_a_marc_row_a_non_eighty_series_part_is_none(self) -> None:
        """No MRP type is known, so the router would say none too."""
        db = _FakeDb(mard=[_Mard("1000000123", "1500")])
        assert search(db, "1000000123")[0].flow_hint is Flow.NONE

    def test_parts_the_assistant_covers_come_before_ones_it_does_not(self) -> None:
        db = _FakeDb(
            makt=[_Makt("1000000001", "PUMP GASKET"), _Makt("1000000123", "PUMP IMPELLER")],
            marc=[_Marc("1000000001", "1300", "VB"), _Marc("1000000123", "1300", "PD")],
        )
        assert [m.material_id for m in search(db, "pump")] == ["1000000123", "1000000001"]


class TestBounds:
    def test_the_limit_is_applied(self) -> None:
        db = _FakeDb(marc=[_Marc(f"800000{i:04d}", "1300") for i in range(30)])
        assert len(search(db, "8000", limit=5)) == 5

    def test_a_short_query_asks_the_database_nothing(self) -> None:
        db = _FakeDb()
        assert search(db, "p") == []
        assert db.queries == []


class TestTheRoute:
    def _client(self, db: _FakeDb) -> TestClient:
        app.dependency_overrides[get_db] = lambda: db
        return TestClient(app)

    def teardown_method(self) -> None:
        app.dependency_overrides.pop(get_db, None)

    def test_serves_camel_case_items_and_the_hint_note(self) -> None:
        db = _FakeDb(
            makt=[_Makt("8000005632", "PUMP CENTRIFUGAL")],
            marc=[_Marc("8000005632", "1300", "ND")],
        )
        response = self._client(db).get("/api/assistant/materials", params={"q": "pump"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["items"] == [
            {
                "materialId": "8000005632",
                "description": "PUMP CENTRIFUGAL",
                "plant": "1300",
                "plantName": "Black Mountain Mining",
                "flowHint": "i08",
                "mrpType": "ND",
            }
        ]
        assert "Hint only" in body["note"]

    def test_a_first_keystroke_is_an_empty_list_not_an_error(self) -> None:
        response = self._client(_FakeDb()).get("/api/assistant/materials", params={"q": "p"})
        assert response.status_code == 200
        assert response.json()["items"] == []
