"""W5.1 -- 80-series detection and material-number normalisation.

This is the task plan's section 13 table, one test per row, plus the properties
that keep the rule honest as the code around it changes.

None of these tests needs a database. That is the point of putting the rule in
its own module: the thing every other number in I08 depends on can be checked
in milliseconds, everywhere, including a machine with nothing configured.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.material_number import (
    is_eighty_series,
    normalise,
    same_material,
    series_like_patterns,
)
from tests.i8_support import needs_views


@pytest.fixture
def cfg() -> I8Settings:
    """Settings with no .env underneath, so a local file cannot change a result."""
    return I8Settings(_env_file=None)


# --- normalise ------------------------------------------------------------


class TestNormalise:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("000000008000000000", "8000000000"),  # live CPI, 18 chars padded
            ("8000005632", "8000005632"),  # July extract form
            ("00008000005632", "8000005632"),  # partially padded
            ("  8000005632  ", "8000005632"),  # whitespace from a spreadsheet
            ("0", None),  # all zeros is not a material
            ("000", None),
            ("", None),
            ("   ", None),
            (None, None),
        ],
    )
    def test_canonical_form(self, raw: str | None, expected: str | None) -> None:
        assert normalise(raw) == expected

    def test_padded_and_unpadded_agree(self) -> None:
        """The whole point of ruling 5.1.

        The same material arrives from CPI padded and from the extract
        unpadded. If these two ever stop agreeing, every join in I08 silently
        returns nothing at cutover.
        """
        assert normalise("000000008000005632") == normalise("8000005632")

    def test_is_idempotent(self) -> None:
        once = normalise("000000008000005632")
        assert normalise(once) == once

    def test_does_not_raise_on_missing_material(self) -> None:
        """raw_mseg has 34,975 rows with no material number.

        A normalisation that throws on those turns a known data gap into an
        outage on every request that touches movements.
        """
        for value in (None, "", "   ", "\t"):
            assert normalise(value) is None


# --- is_eighty_series -----------------------------------------------------


class TestIsEightySeries:
    @pytest.mark.parametrize(
        ("raw", "expected", "why"),
        [
            ("8000005632", True, "extract form"),
            ("000000008000000000", True, "CPI zero-padded form -- ruling 5.1"),
            ("8000000007", True, "lowest material on a real repair PO"),
            ("8000006059", True, "highest material in the MARA extract"),
            ("5000000800", False, "contains 800; this is what kills a contains test"),
            ("1800000000", False, "contains 80 but does not start with it"),
            ("80", False, "too short"),
            ("800000000", False, "nine digits -- one short"),
            ("80000056321", False, "eleven digits -- one long"),
            ("80ABCDEFGH", False, "not numeric"),
            ("", False, "blank"),
            ("   ", False, "whitespace only"),
            (None, False, "missing, as in 34,975 MSEG rows"),
        ],
    )
    def test_table(self, raw: str | None, expected: bool, why: str, cfg) -> None:
        assert is_eighty_series(raw, cfg) is expected, why

    def test_startswith_not_contains(self, cfg) -> None:
        """The single most dangerous way to get this wrong.

        5000000800 is a ten-digit numeric material containing '800'. Every
        guard except the prefix passes on it, so a `contains` test adds a
        non-repairable consumable to the universe and nothing errors.
        """
        assert is_eighty_series("5000000800", cfg) is False
        assert is_eighty_series("8000000005", cfg) is True

    def test_length_guard_is_load_bearing(self, cfg) -> None:
        """Without it, the bare string '80' is a repairable material."""
        assert is_eighty_series("80", cfg) is False


# --- configuration, not constants ----------------------------------------


class TestDetectionIsConfigurable:
    def test_prefix_comes_from_configuration(self) -> None:
        """VZI could add a second repairable series without a code change."""
        cfg = I8Settings(_env_file=None, series_prefixes="80,81")
        assert cfg.series_prefix_list == ("80", "81")
        assert is_eighty_series("8100000001", cfg) is True
        assert is_eighty_series("8000000001", cfg) is True

    def test_changing_the_prefix_changes_the_answer(self) -> None:
        cfg = I8Settings(_env_file=None, series_prefixes="90")
        assert is_eighty_series("8000005632", cfg) is False
        assert is_eighty_series("9000005632", cfg) is True

    def test_length_comes_from_configuration(self) -> None:
        cfg = I8Settings(_env_file=None, material_number_length=8)
        assert is_eighty_series("80000056", cfg) is True
        assert is_eighty_series("8000005632", cfg) is False

    def test_patterns_are_generated_not_written(self) -> None:
        """No literal '80' in any query -- the prefilter is derived."""
        cfg = I8Settings(_env_file=None, series_prefixes="80,81")
        assert series_like_patterns(cfg) == ["80%", "81%"]


# --- same_material --------------------------------------------------------


class TestSameMaterial:
    def test_compares_normalised_forms(self) -> None:
        assert same_material("000000008000005632", "8000005632") is True

    def test_two_unknowns_are_not_a_match(self) -> None:
        """An absent material number is not evidence that two rows agree."""
        assert same_material(None, None) is False
        assert same_material("", "") is False

    def test_different_materials_do_not_match(self) -> None:
        assert same_material("8000005632", "8000005633") is False


# --- the prefilter contract, against the real database --------------------


@needs_views
class TestPrefilterIsLooserThanThePredicate:
    """The database prefilter must never exclude a row the real test accepts.

    The universe query narrows candidates with a LIKE before applying
    is_eighty_series() in Python. That is only safe while the LIKE is strictly
    weaker than the predicate. If someone ever "optimises" the prefilter by
    adding the length or digit guard to it, this test is what catches it.
    """

    @pytest.mark.parametrize("view", ["v_mard", "v_ekpo", "v_zmm065"])
    def test_every_material_the_predicate_accepts_is_in_the_prefilter(
        self, view: str
    ) -> None:
        from app.core.db import get_engine

        cfg = I8Settings(_env_file=None)
        patterns = series_like_patterns(cfg)

        with get_engine().connect() as connection:
            prefiltered = {
                row[0]
                for row in connection.execute(
                    text(f"select distinct matnr from {view} where matnr like any(:p)"),
                    {"p": patterns},
                )
            }
            everything = {
                row[0]
                for row in connection.execute(
                    text(f"select distinct matnr from {view} where matnr is not null")
                )
            }

        accepted = {m for m in everything if is_eighty_series(m, cfg)}
        assert accepted, f"{view} has no 80-series materials -- check the fixture"

        missed = accepted - prefiltered
        assert not missed, (
            f"{len(missed)} materials in {view} pass is_eighty_series but the "
            f"prefilter excludes them, e.g. {sorted(missed)[:5]}"
        )

    def test_the_prefilter_carries_no_guard_of_its_own(self) -> None:
        """Looseness proved structurally, not by whatever the data happens to hold.

        Every material in the July extract that starts with '80' also happens
        to be ten digits, so on this data the prefilter and the predicate agree
        exactly. That coincidence is not the contract -- a future extract, or
        the CPI projection, will contain values where they differ. What must
        stay true is that the SQL side carries only the prefix, so the Python
        predicate is what rejects anything.
        """
        import fnmatch

        cfg = I8Settings(_env_file=None)
        patterns = [p.replace("%", "*") for p in series_like_patterns(cfg)]

        for value in ("80", "80ABCDEFGH", "800000000", "80000056321"):
            assert any(fnmatch.fnmatch(value, p) for p in patterns), (
                f"{value!r} must reach Python -- the prefilter may not reject it"
            )
            assert is_eighty_series(value, cfg) is False, (
                f"{value!r} must then be rejected by the predicate, not the SQL"
            )
