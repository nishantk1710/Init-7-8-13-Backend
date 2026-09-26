"""The two-plant delivery scope (team-lead ruling, 21-Sep-2026).

``docs/Initiative_08_Decisions_21Sep.md`` §2 promises every decision in it is
enforced by a named test. This is that test for the plant scope.

These are unit tests over the shared module and the SQL it generates -- no
database. The integration side (that a real query actually comes back with
in-scope rows only) lives in
``tests/i13/test_movement_metrics_postgres.py::test_movement_history_is_restricted_to_the_in_scope_plants``.
"""

from __future__ import annotations

import pytest

from app.initiatives.i8.views import FUNCTIONS, _IN_SCOPE_PLANT_SQL
from app.shared import plant_scope
from app.shared.plant_scope import (
    IN_SCOPE_PLANTS,
    PLANT_NAMES,
    is_in_scope,
    normalise,
    plant_name,
    sql_literals,
    sql_predicate,
)

# The codes the July extract carries that the ruling puts out of scope. Written
# out here on purpose: this is the one file that should fail loudly if somebody
# widens IN_SCOPE_PLANTS without a decision behind it.
OUT_OF_SCOPE = ("1100", "1200", "1600", "1800", "1820", "2000", "3000", "4000")


class TestTheScopeItself:
    def test_the_scope_is_exactly_black_mountain_and_gamsberg(self) -> None:
        assert IN_SCOPE_PLANTS == ("1300", "1500")

    def test_every_in_scope_plant_has_a_name(self) -> None:
        """A bare code must never reach a screen. Both are documented, so the
        name map covers the scope completely rather than partially."""
        assert set(PLANT_NAMES) == set(IN_SCOPE_PLANTS)

    def test_the_scope_is_immutable(self) -> None:
        """A tuple, so a caller cannot widen the platform's scope in place."""
        with pytest.raises(AttributeError):
            IN_SCOPE_PLANTS.append("1600")  # type: ignore[attr-defined]


class TestIsInScope:
    @pytest.mark.parametrize("code", IN_SCOPE_PLANTS)
    def test_in_scope_codes_are_accepted(self, code: str) -> None:
        assert is_in_scope(code) is True

    @pytest.mark.parametrize("code", OUT_OF_SCOPE)
    def test_out_of_scope_codes_are_rejected(self, code: str) -> None:
        assert is_in_scope(code) is False

    @pytest.mark.parametrize("value", [None, "", "   ", "\t"])
    def test_missing_plant_is_not_in_scope(self, value: str | None) -> None:
        """A row with no plant cannot be attributed to either site, and
        guessing one would put a quantity under a heading no evidence
        supports."""
        assert is_in_scope(value) is False

    def test_surrounding_whitespace_does_not_push_a_row_out_of_scope(self) -> None:
        """The raw layer is all text; a padded cell is still plant 1300."""
        assert is_in_scope("  1300  ") is True

    def test_a_code_that_merely_contains_an_in_scope_code_is_rejected(self) -> None:
        assert is_in_scope("11300") is False
        assert is_in_scope("1300X") is False


class TestNormalise:
    def test_blank_becomes_none_so_absent_and_out_of_scope_stay_distinct(self) -> None:
        assert normalise("   ") is None
        assert normalise(None) is None

    def test_a_real_code_is_trimmed_not_altered(self) -> None:
        assert normalise(" 1500 ") == "1500"


class TestPlantName:
    def test_in_scope_codes_resolve_to_their_documented_names(self) -> None:
        assert plant_name("1300") == "Black Mountain Mining"
        assert plant_name("1500") == "Gamsberg"

    def test_an_unknown_code_returns_the_code_rather_than_an_invented_name(self) -> None:
        """A code is a fact; a made-up site name is not."""
        assert plant_name("1600") == "1600"

    def test_no_plant_stays_no_plant(self) -> None:
        assert plant_name(None) is None
        assert plant_name("  ") is None


class TestGeneratedSql:
    def test_literals_are_quoted_and_comma_separated(self) -> None:
        assert sql_literals() == "'1300', '1500'"

    def test_predicate_trims_before_comparing(self) -> None:
        assert sql_predicate("m.plant") == "TRIM(m.plant) IN ('1300', '1500')"

    def test_the_predicate_names_the_column_it_was_given(self) -> None:
        assert sql_predicate("k.plant").startswith("TRIM(k.plant)")

    @pytest.mark.parametrize("code", OUT_OF_SCOPE)
    def test_no_out_of_scope_code_appears_in_the_generated_sql(self, code: str) -> None:
        assert code not in sql_predicate("plant")
        assert code not in _IN_SCOPE_PLANT_SQL


class TestTheDatabaseFunctionCannotDriftFromPython:
    """The whole reason ``in_scope_plant()`` is generated rather than written
    into a .sql file: two hand-maintained lists drift, and the failure is
    silent -- the API serves a plant the Python rejects, or hides one it
    accepts, and either way the totals stop matching the register."""

    def test_the_function_is_managed_so_drop_views_removes_it(self) -> None:
        assert "in_scope_plant" in FUNCTIONS

    @pytest.mark.parametrize("code", IN_SCOPE_PLANTS)
    def test_the_generated_function_tests_the_same_codes(self, code: str) -> None:
        assert f"'{code}'" in _IN_SCOPE_PLANT_SQL

    def test_the_function_treats_null_as_out_of_scope(self) -> None:
        """``coalesce`` rather than ``returns null on null input``: a NULL
        plant must be excluded, not propagate a NULL that a WHERE clause then
        reads as "not true" by accident."""
        assert "coalesce" in _IN_SCOPE_PLANT_SQL


def test_the_public_surface_is_reachable_from_app_shared() -> None:
    """Initiatives import from ``app.shared``, never from the module directly
    -- same rule criticality follows."""
    from app import shared

    assert shared.IN_SCOPE_PLANTS is plant_scope.IN_SCOPE_PLANTS
    assert shared.is_in_scope is plant_scope.is_in_scope
    assert shared.sql_predicate is plant_scope.sql_predicate
