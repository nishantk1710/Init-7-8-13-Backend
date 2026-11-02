"""Usable vs damaged stock on the I08 universe (27-Oct-2026), without a database.

MARD counts every unit on the shelf; SAP keeps each unit's condition in MCHB's
batch field (the default source) or MBEW's valuation type. These pin the pure
split (:func:`universe.condition_stock`), the settings that drive it, the one
grouped query per source, what the snapshot build stages for it, and the rules
echo. ``test_i8_staged_build.py`` checks the same split on SQL Server, staged
and direct.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.initiatives.i8 import staged, universe
from app.initiatives.i8.config import STOCK_CONDITION_SOURCES, I8Settings
from app.initiatives.i8.universe import ConditionStock, condition_stock

USABLE = ("NEW", "REFURBISHD")
DAMAGED = ("DAMAGED",)


def _row(material: str, plant: str | None, condition: str | None, quantity) -> dict:
    return {
        "matnr": material,
        "werks": plant,
        "stock_condition": condition,
        "quantity": None if quantity is None else Decimal(str(quantity)),
    }


def _split(rows, source: str = "MCHB") -> dict:
    return condition_stock(rows, source=source, usable=USABLE, damaged=DAMAGED)


# --- the pure split ---------------------------------------------------------------


def test_mchb_rows_split_into_usable_and_damaged() -> None:
    split = _split([
        _row("8000003325", "1300", "NEW", 1),
        _row("8000003325", "1300", "REFURBISHD", 1),
        _row("8000003325", "1300", "DAMAGED", 1),
    ])
    assert split == {
        ("8000003325", "1300"): ConditionStock(
            usable=Decimal(2),
            damaged=Decimal(1),
            by_condition=(("DAMAGED", Decimal(1)), ("NEW", Decimal(1)), ("REFURBISHD", Decimal(1))),
            source="MCHB",
        )
    }


def test_each_plant_is_split_on_its_own() -> None:
    split = _split([
        _row("8000003325", "1300", "NEW", 2),
        _row("8000003325", "1500", "DAMAGED", 3),
    ])
    assert split[("8000003325", "1300")].usable == 2
    assert split[("8000003325", "1300")].damaged == 0
    assert split[("8000003325", "1500")].usable == 0
    assert split[("8000003325", "1500")].damaged == 3


def test_rows_of_one_condition_are_summed_and_compared_upper_case() -> None:
    """MCHB is one row per storage location, so the SQL's sums can still meet
    again here -- and a lower-case or padded value is the same condition."""
    split = _split([
        _row("8000003325", "1300", "NEW", "1.5"),
        _row("8000003325", "1300", " new ", "0.5"),
    ])
    assert split[("8000003325", "1300")].by_condition == (("NEW", Decimal("2.0")),)
    assert split[("8000003325", "1300")].usable == Decimal("2.0")


def test_mbew_blank_aggregate_is_skipped_and_scrap_is_neither() -> None:
    """MBEW values a split part per condition AND on a blank-BWTAR row that adds
    them up: counting that row would double the stock. SCRAP is kept in the
    breakdown but counted as neither usable nor damaged."""
    split = _split(
        [
            _row("8000004885", "1500", None, 7),
            _row("8000004885", "1500", "", 7),
            _row("8000004885", "1500", "NEW", 3),
            _row("8000004885", "1500", "DAMAGED", 2),
            _row("8000004885", "1500", "SCRAP", 2),
        ],
        source="MBEW",
    )
    part = split[("8000004885", "1500")]
    assert part.usable == 3
    assert part.damaged == 2
    assert dict(part.by_condition) == {"NEW": 3, "DAMAGED": 2, "SCRAP": 2}
    assert part.source == "MBEW"


def test_a_part_with_no_condition_rows_has_no_split() -> None:
    """Unknown, not zero: no entry, so the universe row keeps None."""
    split = _split([
        _row("8000000001", "1300", "NEW", 1),
        _row("8000000002", "1300", "", 4),  # only the aggregate: says nothing
        _row("8000000003", "1300", "NEW", None),  # no readable quantity
    ])
    assert set(split) == {("8000000001", "1300")}
    assert _split([]) == {}


def test_zero_stock_conditions_still_make_the_condition_known() -> None:
    """MCHB keeps a row per condition even at zero: the part's condition is
    known, and it holds nothing usable."""
    part = _split([_row("8000000001", "1300", "NEW", 0), _row("8000000001", "1300", "DAMAGED", 0)])[
        ("8000000001", "1300")
    ]
    assert (part.usable, part.damaged) == (0, 0)


def test_the_condition_lists_come_from_the_caller() -> None:
    split = condition_stock(
        [_row("8000000001", "1300", "NEW", 1), _row("8000000001", "1300", "REFURBISHD", 2)],
        source="MCHB",
        usable=("NEW",),
        damaged=("REFURBISHD",),
    )
    assert (split[("8000000001", "1300")].usable, split[("8000000001", "1300")].damaged) == (1, 2)


# --- the universe row ------------------------------------------------------------------


def test_the_universe_row_defaults_to_an_unknown_split() -> None:
    from tests.assistant.conftest import universe_row

    row = universe_row(stock_on_hand=Decimal(3))
    assert row.usable_stock is None
    assert row.damaged_stock is None
    assert row.stock_by_condition == ()
    assert row.stock_condition_source is None


# --- the settings -------------------------------------------------------------------------


def test_the_defaults() -> None:
    cfg = I8Settings(_env_file=None)
    assert cfg.stock_condition_source == "MCHB"
    assert cfg.usable_stock_condition_list == ("NEW", "REFURBISHD")
    assert cfg.damaged_stock_condition_list == ("DAMAGED",)
    assert STOCK_CONDITION_SOURCES == ("MCHB", "MBEW")


def test_the_source_is_upper_cased() -> None:
    assert I8Settings(_env_file=None, stock_condition_source=" mbew ").stock_condition_source == "MBEW"


@pytest.mark.parametrize("value", ["MARD", "", "MCHB,MBEW"])
def test_an_unknown_source_is_a_startup_error(value) -> None:
    with pytest.raises(ValidationError, match="I8_STOCK_CONDITION_SOURCE"):
        I8Settings(_env_file=None, stock_condition_source=value)


def test_the_env_var_is_read(monkeypatch) -> None:
    monkeypatch.setenv("I8_STOCK_CONDITION_SOURCE", "MBEW")
    monkeypatch.setenv("I8_USABLE_STOCK_CONDITIONS", "new, refurbishd,NEW")
    cfg = I8Settings(_env_file=None)
    assert cfg.stock_condition_source == "MBEW"
    assert cfg.usable_stock_condition_list == ("NEW", "REFURBISHD")


def test_a_condition_in_both_lists_is_a_startup_error() -> None:
    with pytest.raises(ValidationError, match="both name NEW"):
        I8Settings(_env_file=None, usable_stock_conditions="NEW", damaged_stock_conditions="DAMAGED, new")


def test_the_error_does_not_quote_other_settings() -> None:
    """A model-level validator's error quotes the whole input, secrets and all;
    this one quotes only the field it checks."""
    with pytest.raises(ValidationError) as error:
        I8Settings(_env_file=None, damaged_stock_conditions="NEW", series_prefixes="80")
    assert "series_prefixes" not in str(error.value)


# --- the query ------------------------------------------------------------------------------


class _Db:
    """Records the one statement and answers it with ``rows``."""

    def __init__(self, rows) -> None:
        self.rows = rows
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), dict(params or {})))
        return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: self.rows))


@pytest.mark.parametrize(("source", "view"), [("MCHB", "v_mchb"), ("MBEW", "v_mbew")])
def test_one_grouped_read_of_the_configured_source(source, view) -> None:
    db = _Db([_row("8000000001", "1300", "NEW", 1)])
    cfg = I8Settings(_env_file=None, stock_condition_source=source)
    split = universe.fetch_condition_stock(db, cfg, "(matnr like :pattern0)", {"pattern0": "80%"})

    [(sql, params)] = db.statements
    assert re.search(rf"\bfrom {view}\b", sql)
    assert "group by" in sql and "(matnr like :pattern0)" in sql
    assert params == {"pattern0": "80%"}
    assert split[("8000000001", "1300")].source == source


def test_the_mbew_read_leaves_out_the_blank_aggregate_row() -> None:
    sql = universe.CONDITION_STOCK_SQL["MBEW"]
    assert "bwtar is not null" in sql and "bwtar <> ''" in sql
    assert "sum(lbkum)" in sql
    assert "sum(clabs)" in universe.CONDITION_STOCK_SQL["MCHB"]
    assert set(universe.CONDITION_STOCK_SQL) == set(STOCK_CONDITION_SOURCES)


def test_a_staged_build_reads_its_staged_copy() -> None:
    db = _Db([])
    cfg = I8Settings(_env_file=None)
    sources = staged.Sources(views={"v_mchb": "#i8s_v_mchb"})
    universe.fetch_condition_stock(db, cfg, "1 = 1", {}, sources)
    assert "from #i8s_v_mchb" in db.statements[0][0]


# --- what the snapshot build stages -------------------------------------------------------


def test_each_source_is_a_staged_view_with_its_own_body() -> None:
    assert set(staged.CONDITION_STOCK) == set(STOCK_CONDITION_SOURCES)
    for view, table, _plant in staged.CONDITION_STOCK.values():
        assert view in staged.VIEWS
        assert f"from {table}" in re.sub(r"\s+", " ", staged._body(view)).lower()


class _Recorder:
    """Every statement staging runs, each answered with a count of 0."""

    def __init__(self) -> None:
        self.sql: list[str] = []

    def execute(self, statement, params=None):
        self.sql.append(str(statement))
        return SimpleNamespace(scalar_one=lambda: 0)


@pytest.mark.parametrize(
    ("source", "staged_name", "other"),
    [("MCHB", "#i8b_mchb", "#i8b_mbew"), ("MBEW", "#i8b_mbew", "#i8b_mchb")],
)
def test_only_the_configured_source_is_staged(monkeypatch, source, staged_name, other) -> None:
    from app.shared import optional_tables

    monkeypatch.setattr(optional_tables, "existing", lambda db, tables: [])
    db = _Recorder()
    sources = staged._stage(db, I8Settings(_env_file=None, stock_condition_source=source))

    narrowing = [s for s in db.sql if f"INTO {staged_name} " in s]
    assert len(narrowing) == 1
    assert "80%" in narrowing[0]  # 80-series only
    assert ("valuation_area" if source == "MBEW" else "TRIM(COALESCE(plant") in narrowing[0]
    assert not any(f"INTO {other} " in s for s in db.sql)
    view = staged.CONDITION_STOCK[source][0]
    assert sources.views[view] == f"#i8s_{view}"
    # Staged under the snapshot's own names, never the coding screen's.
    assert not any("#i8cb_mbew" in s or "#i8cs_v_mbew" in s for s in db.sql)


def test_mseg_is_staged_without_an_or_across_the_two_lookups(monkeypatch) -> None:
    """27-Oct-2026: one WHERE with "PO in staged POs OR reservation in staged
    reservations" ran past SQL Server's 600 s limit on nonprod's ~3.5M MSEG
    rows. Each half must be its own statement against an indexed temp table."""
    from app.shared import optional_tables

    monkeypatch.setattr(optional_tables, "existing", lambda db, tables: [])
    db = _Recorder()
    staged._stage(db, I8Settings(_env_file=None))

    into = [s for s in db.sql if "SELECT * INTO #i8b_mseg " in s]
    assert len(into) == 1 and "#i8s_docs" in into[0] and "reservation" not in into[0]
    insert = [s for s in db.sql if s.startswith("INSERT INTO #i8b_mseg ")]
    assert len(insert) == 1
    assert "IN (SELECT rsnum FROM #i8s_rsv)" in insert[0]
    assert "movement_type IN ('261', '262')" in insert[0]
    assert any("CREATE CLUSTERED INDEX ix_i8s_rsv ON #i8s_rsv (rsnum)" in s for s in db.sql)
    # The staged reservations are built before MSEG reads them.
    first_rsv = next(i for i, s in enumerate(db.sql) if "INTO #i8s_rsv" in s)
    assert first_rsv < db.sql.index(insert[0])


# --- the rules echo and the stored snapshot ------------------------------------------------


def test_the_rules_echo_the_stock_settings(monkeypatch) -> None:
    from app.api.i8 import router
    from app.initiatives.i8.register import RegisterStats
    from app.initiatives.i8.universe import UniverseStats

    monkeypatch.setattr(router, "get_criticality_source", lambda: SimpleNamespace(name="fake"))
    snapshot = SimpleNamespace(
        reference_date=date(2026, 10, 1),
        built_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        build_seconds=1.0,
        source_loaded_at=None,
        register_stats=RegisterStats(total_lines=0, open_lines=0),
        universe_stats=UniverseStats(rows=0, materials=0, plants=0),
    )
    cfg = I8Settings(_env_file=None, stock_condition_source="MBEW")
    rules = router.get_snapshot_info(snapshot, cfg).model_dump(by_alias=True)["rules"]
    assert rules["stockConditionSource"] == "MBEW"
    assert rules["usableStockConditions"] == "NEW, REFURBISHD"
    assert rules["damagedStockConditions"] == "DAMAGED"
