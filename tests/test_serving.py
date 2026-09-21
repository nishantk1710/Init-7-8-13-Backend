"""The serving layer build.

The database half is exercised by the migration step in CI. What is tested here
is the row-level transform, which is where the decisions are: which join key,
which rows are in scope, and what happens to a row that is incomplete.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.models import Base, MaterialPlant
from app.serving.material_plant import BuildResult, _dimension_row

RUN_DATE = date(2026, 9, 21)

# MARC gives the padded form, as OData does for a numeric material.
MARC = {
    "Matnr": "000000002000000270",
    "Werks": "1300",
    "Dismm": "ND",
    "Eisbe": "              2.000",
    "Minbe": "              4.000",
    "Mabst": "             11.000",
    "Losgr": "              1.000",
    "Plifz": "14",
}


def build_one(marc=None, mara=None, makt=None):
    result = BuildResult()
    row = _dimension_row(
        marc if marc is not None else MARC,
        mara or {},
        makt or {},
        RUN_DATE,
        result,
    )
    return row, result


# --- The join key -----------------------------------------------------------


def test_lookups_match_on_the_padded_key() -> None:
    """The bug this layer exists to prevent.

    MARA may carry the short form where MARC carries the padded one. Matching
    the raw strings returns nothing for exactly the rows that differ, and an
    empty result reads as "no such material".
    """
    row, _ = build_one(
        mara={"000000002000000270": {"Mtart": "ERSA", "Matkl": "MRO01", "Meins": "EA"}},
        makt={"000000002000000270": {"Maktx": "BEARING ROLLER"}},
    )

    assert row["mtart"] == "ERSA"
    assert row["maktx"] == "BEARING ROLLER"


def test_the_stored_key_is_always_padded() -> None:
    """Half-padded data is worse than none: it joins for some rows, not others."""
    row, _ = build_one({**MARC, "Matnr": "2000000270"})

    assert row["matnr"] == "000000002000000270"


def test_an_alphanumeric_material_is_not_padded() -> None:
    row, _ = build_one({**MARC, "Matnr": "SPARE-12"})

    assert row["matnr"] == "SPARE-12"


# --- Missing master data ----------------------------------------------------


def test_a_material_with_no_mara_row_still_gets_a_dimension_row() -> None:
    """MARA covered 93.4% of MARC on 21-Sep.

    An inner join would drop the other 6.6% -- real material-plant
    combinations that stock and movements reference. A row with a null
    description beats a movement pointing at a material the dimension has
    never heard of.
    """
    row, result = build_one()

    assert row is not None
    assert row["mtart"] is None
    assert row["maktx"] is None
    assert result.missing_mara == 1
    assert result.missing_maktx == 1


def test_a_row_without_both_key_halves_is_skipped_and_counted() -> None:
    """Unaddressable, unjoinable, uncorrectable. Counted, not silently dropped."""
    row, result = build_one({**MARC, "Werks": "   "})

    assert row is None
    assert result.skipped_no_key == 1


# --- Scope ------------------------------------------------------------------


def test_nd_and_pd_are_in_oar_scope() -> None:
    """The 08-Sep VZI ruling, resolved once here rather than in every query."""
    for mrp in ("ND", "PD"):
        row, result = build_one({**MARC, "Dismm": mrp})
        assert row["is_oar"] is True
        assert result.oar_rows == 1


def test_everything_else_is_out_of_scope() -> None:
    for mrp in ("VB", "V1", "M0", ""):
        row, result = build_one({**MARC, "Dismm": mrp})
        assert row["is_oar"] is False, mrp
        assert result.oar_rows == 0


def test_a_blank_mrp_type_is_a_value_not_a_missing_one() -> None:
    """47% of rows in the dev client have no MRP type at all."""
    row, _ = build_one({**MARC, "Dismm": "  "})

    assert row["dismm"] is None
    assert row["is_oar"] is False


# --- Types ------------------------------------------------------------------


def test_space_padded_decimals_become_numbers() -> None:
    """'              2.000' compares to other strings, and wrongly:
    '10' sorts before '9'."""
    row, _ = build_one()

    assert row["eisbe"] == Decimal("2.000")
    assert row["minbe"] == Decimal("4.000")
    assert row["mabst"] == Decimal("11.000")


def test_lead_time_is_a_whole_number() -> None:
    row, _ = build_one()

    assert row["plifz"] == 14


def test_an_unreadable_figure_is_null_rather_than_a_failed_build() -> None:
    row, _ = build_one({**MARC, "Eisbe": "n/a", "Plifz": ""})

    assert row["eisbe"] is None
    assert row["plifz"] is None
    assert row["matnr"] == "000000002000000270", "the rest of the row survives"


# --- Provenance -------------------------------------------------------------


def test_every_row_records_which_pull_built_it() -> None:
    """Otherwise "why does this disagree with SAP" has no answer."""
    row, _ = build_one()

    assert row["source_run_date"] == RUN_DATE


# --- Registration -----------------------------------------------------------


def test_the_serving_model_is_in_the_metadata() -> None:
    """A model nothing imports is absent from Base.metadata, so its table
    silently never gets a migration."""
    assert "material_plant" in Base.metadata.tables
    assert MaterialPlant.__tablename__ == "material_plant"


def test_the_watermark_model_is_in_the_metadata() -> None:
    assert "ingest_watermark" in Base.metadata.tables
