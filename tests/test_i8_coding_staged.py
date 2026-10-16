"""The staged FR-2 coding screen answers exactly what the view-backed one does.

Seeds a small SAP tenant into ``raw_ekpo`` and ``raw_eban`` (SAP field names, as
the CSV route loads them), builds the normalise and I08 views, then runs the
keyword screen twice -- ``staged=False`` reading ``v_ekpo``/``v_eban`` over the
whole extract, ``staged=True`` reading the narrowed copies ``staged.py`` makes
-- and compares every candidate, every line behind it, every twin and every
statistic.

Why this test is the important half of the change
-------------------------------------------------
Staging is only safe if the narrowing predicate keeps every row the screen's
own predicate would have matched. That is an argument about two pieces of SQL
agreeing, and an argument is not evidence. The tenant below is shaped so that
each way the two could disagree shows up as a differing candidate:

* text that mentions repair in lower case, where the screen matches on
  ``upper(...)`` and the narrowing must too
* a keyword appearing mid-word and at the very start of the text
* an 80-series material carrying a suspect's exact text -- the twin, which is
  read from a DIFFERENTLY narrowed copy (80-series, any text) than the
  candidates (any material, repair text), so a mix-up between the two staged
  copies is visible
* a repair-language line at an out-of-scope plant, which both must drop
* a line with no text at all, which neither may count as a hit but both must
  count in the denominator
* a deleted requisition and a converted one, whose rules run in Python over
  whatever the SQL returned
* keys zero-padded differently between EKPO and EBAN

Needs a database (skipped without DATABASE_URL); creates and drops its own raw
tables, so it runs on the empty CI database.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy import text

from app.core.config import get_settings

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")
pytestmark = [needs_db]

#: 80-series: already coded repairable. Never a candidate; the twin side.
CODED = "8000000001"
CODED_TWIN = "8000000002"
#: Not 80-series: what the screen is looking for.
SUSPECT = "1000000777"
SUSPECT_TWINNED = "1000000888"
PLAIN = "5000000123"

#: The text SUSPECT_TWINNED shares with CODED_TWIN, letter for letter.
SHARED = "GEARBOX OVERHAUL AND REPAIR"

RAW: dict[str, list[str]] = {
    "raw_ekpo": [
        "EBELN", "EBELP", "LOEKZ", "AEDAT", "TXZ01", "MATNR", "WERKS", "MENGE", "NETPR",
        "ELIKZ", "PSTYP", "MEINS", "BANFN", "BNFPO", "MTART", "AFNAM", "CREATIONDATE",
    ],
    "raw_eban": [
        "BANFN", "BNFPO", "LOEKZ", "TXZ01", "MATNR", "WERKS", "MENGE", "BADAT",
        "LFDAT", "PSTYP", "EBELN", "EBELP", "LIFNR", "LGORT",
    ],
    # The New Item Price's source, read through its own staged copy
    # (staged.valuation_sources), narrowed to the candidates by literal key.
    "raw_mbew": ["MATNR", "BWKEY", "BWTAR", "VERPR", "PEINH"],
}

ROWS: dict[str, list[tuple]] = {
    "raw_ekpo": [
        # A suspect: repair language, not 80-series. The keyword sits mid-text.
        ("4500000001", "10", "", "01.06.2026", "PUMP SEAL REPAIR KIT", SUSPECT, "1300", "1", "100",
         "", "0", "EA", "", "", "ERSA", "JONESP", "01.06.2026"),
        # The same suspect again, lower case, and the keyword leads the text.
        ("4500000001", "20", "", "01.06.2026", "repair of pump housing", SUSPECT, "1300", "1", "100",
         "", "0", "EA", "", "", "ERSA", "JONESP", "01.06.2026"),
        # A suspect whose text an 80-series material also carries -- the twin.
        ("4500000002", "10", "", "02.06.2026", SHARED, SUSPECT_TWINNED, "1300", "1", "900",
         "", "0", "EA", "", "", "ERSA", "SMITHK", "02.06.2026"),
        # The twin itself: 80-series, identical text. Already coded, so never a
        # candidate -- but it must be reachable through the TWIN staging.
        ("4100000003", "10", "", "03.06.2026", SHARED, CODED_TWIN, "1300", "1", "900",
         "", "3", "EA", "", "", "ZREP", "SMITHK", "03.06.2026"),
        # 80-series with repair language: already coded, counted as such.
        ("4100000004", "10", "", "04.06.2026", "MOTOR REPAIR", "000000008000000001", "1300", "1", "500",
         "", "3", "EA", "", "", "ZREP", "MILLERJ", "04.06.2026"),
        # Repair language at an out-of-scope plant: both ways must drop it.
        ("4500000005", "10", "", "05.06.2026", "CONVEYOR REPAIR", PLAIN, "3000", "1", "100",
         "", "0", "EA", "", "", "ERSA", "JONESP", "05.06.2026"),
        # Text, but no repair language: in the denominator, not a hit.
        ("4500000006", "10", "", "06.06.2026", "SAFETY GLOVES", PLAIN, "1300", "100", "2",
         "", "0", "PR", "", "", "ERSA", "JONESP", "06.06.2026"),
        # No text at all: in neither.
        ("4500000007", "10", "", "07.06.2026", "", PLAIN, "1300", "1", "2",
         "", "0", "PR", "", "", "ERSA", "JONESP", "07.06.2026"),
    ],
    "raw_eban": [
        # A requisition suspect, its key zero-padded unlike the EKPO rows.
        ("0010000001", "10", "", "BEARING REPAIR REQUIRED", SUSPECT, "1300", "1", "01.05.2026",
         "01.07.2026", "0", "", "", "", "0001"),
        # Deleted: dropped in Python, from whatever the SQL returned.
        ("0010000002", "10", "L", "DELETED PUMP REPAIR", SUSPECT, "1300", "1", "02.05.2026",
         "02.07.2026", "0", "", "", "", "0001"),
        # Converted: it became a PO line, so counting it too would double it.
        ("0010000003", "10", "", "PUMP SEAL REPAIR KIT", SUSPECT, "1300", "1", "03.05.2026",
         "03.07.2026", "0", "4500000001", "10", "", "0001"),
        # Out of scope.
        ("0010000004", "10", "", "CONVEYOR REPAIR", PLAIN, "3000", "1", "04.05.2026",
         "04.07.2026", "0", "", "", "", "0001"),
        # Text, no repair language.
        ("0010000005", "10", "", "SAFETY GLOVES", PLAIN, "1300", "1", "05.05.2026",
         "05.07.2026", "0", "", "", "", "0001"),
    ],
    # Whole numbers only: they read the same whichever CSV_NUMBER_FORMAT the
    # run declares, and the notation is not what this test is about.
    "raw_mbew": [
        # The suspect, zero-padded as the CSV pads it: one row, the price.
        ("000000001000000777", "1300", "", "4500", "1"),
        # Out of scope: both ways must ignore it.
        ("000000001000000777", "3000", "", "99", "1"),
        # Split-valuated: NEW is the price, the blank aggregate is not.
        (SUSPECT_TWINNED, "1300", "", "700", "1"),
        (SUSPECT_TWINNED, "1300", "NEW", "900", "1"),
        (SUSPECT_TWINNED, "1300", "REFURBISHD", "700", "1"),
        # Not a candidate, so never asked about.
        (PLAIN, "1300", "", "1", "1"),
    ],
}

SOURCES = ("PO", "PR")


@pytest.fixture(scope="module")
def tenant():
    from app.core.db import get_engine
    from app.initiatives.i8 import views
    from app.shared import sap_normalise

    engine = get_engine()
    with engine.begin() as conn:
        for table, columns in RAW.items():
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            conn.execute(
                text(f"CREATE TABLE {table} ({', '.join(f'{c} NVARCHAR(200)' for c in columns)})")
            )
            marks = ", ".join(f":p{i}" for i in range(len(columns)))
            conn.execute(
                text(f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({marks})"),
                [{f"p{i}": v for i, v in enumerate(row)} for row in ROWS[table]],
            )
    sap_normalise.ensure_views(engine)
    views.ensure_views(engine)
    yield
    with engine.begin() as conn:
        for table in RAW:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))


@pytest.fixture()
def screened(tenant):
    """(view-backed screen, staged screen, staged again) over the same tenant."""
    from app.core.db import get_sessionmaker
    from app.initiatives.i8.coding_candidates import screen

    db = get_sessionmaker()()
    try:
        direct = screen(db, use_model=False, sources=SOURCES, staged=False)
        staged = screen(db, use_model=False, sources=SOURCES, staged=True)
        # Run twice on one session: the staging must clean up after itself, or
        # the second run trips over its own temp tables.
        again = screen(db, use_model=False, sources=SOURCES, staged=True)
    finally:
        db.rollback()
        db.close()
    return direct, staged, again


def _comparable(candidates):
    """Candidates as plain data, in a stable order."""
    return [
        (
            c.material_id,
            c.verdict,
            tuple(sorted(c.plants)),
            tuple(sorted((line.source, line.document, line.document_item, line.short_text)
                         for line in c.lines)),
            tuple(sorted(c.texts)),
            tuple(sorted(c.twins)),
            c.is_corroborated,
            c.new_item_prices,
        )
        for c in sorted(candidates, key=lambda c: c.material_id)
    ]


class TestTheStagedScreenMatches:
    def test_the_same_candidates(self, screened) -> None:
        direct, staged, again = screened
        assert _comparable(staged[0]) == _comparable(direct[0])
        assert _comparable(again[0]) == _comparable(direct[0])

    def test_the_same_statistics(self, screened) -> None:
        direct, staged, _ = screened
        # Everything except the staging's own numbers, which exist only on the
        # staged run and are the point of it.
        ignore = {"staged_rows_kept", "staged_rows_scanned"}
        a = {k: v for k, v in dataclasses.asdict(direct[1]).items() if k not in ignore}
        b = {k: v for k, v in dataclasses.asdict(staged[1]).items() if k not in ignore}
        assert b == a

    def test_it_found_the_suspects_and_not_the_coded_ones(self, screened) -> None:
        """A sanity check on the tenant itself: a parity test over two empty
        results would pass while proving nothing."""
        direct, _, _ = screened
        found = {c.material_id for c in direct[0]}
        assert SUSPECT in found
        assert SUSPECT_TWINNED in found
        # 80-series materials are removed by partition(), never screened.
        assert CODED not in found and CODED_TWIN not in found
        # Out of scope, and no repair language.
        assert PLAIN not in found

    def test_the_twin_is_found_through_the_staged_copy(self, screened) -> None:
        """The corroboration comes from the 80-series staging, not the keyword
        one -- a material whose text mentions repair is not necessarily the
        material that proves the convention was applied elsewhere."""
        _, staged, _ = screened
        twinned = next(c for c in staged[0] if c.material_id == SUSPECT_TWINNED)
        assert twinned.is_corroborated
        assert [material for material, _ in twinned.twins] == [CODED_TWIN]
        assert twinned.twins[0][1] == SHARED

    def test_the_new_item_price_is_read_through_the_staged_copy(self, screened) -> None:
        """Parity alone would pass over two empty price lists. The suspect's key
        is zero-padded in MBEW and not in EKPO, so this also proves the
        narrowing matches on the normalised key."""
        from decimal import Decimal

        _, staged, _ = screened
        by_material = {c.material_id: c.new_item_prices for c in staged[0]}
        [suspect] = by_material[SUSPECT]
        assert (suspect.plant, suspect.price) == ("1300", Decimal("4500"))
        [twinned] = by_material[SUSPECT_TWINNED]
        assert (twinned.price, twinned.valuation_type) == (Decimal("900"), "NEW")

    def test_lower_case_text_is_matched(self, screened) -> None:
        """The screen matches on upper(text); the narrowing must too, or a row
        the screen would have found never reaches it."""
        _, staged, _ = screened
        suspect = next(c for c in staged[0] if c.material_id == SUSPECT)
        texts = {line.short_text for line in suspect.lines}
        assert "repair of pump housing" in texts


class TestTheStagingReportsItsSelectivity:
    def test_the_staged_run_says_what_it_kept_and_scanned(self, screened) -> None:
        _, staged, _ = screened
        stats = staged[1]
        assert stats.staged_rows_kept, "a staged run must report what staging kept"
        # Every source read, plus the twin copy.
        assert "v_ekpo" in stats.staged_rows_kept
        assert "v_eban" in stats.staged_rows_kept
        assert "v_ekpo (twins)" in stats.staged_rows_kept
        # The denominator the keyword filter has to be selective against.
        assert stats.staged_rows_scanned["v_ekpo"] >= stats.staged_rows_kept["v_ekpo"]

    def test_the_direct_run_reports_no_staging(self, screened) -> None:
        direct, _, _ = screened
        assert direct[1].staged_rows_kept == {}
        assert direct[1].staged_rows_scanned == {}

    def test_the_denominator_is_not_the_narrowed_copy(self, screened) -> None:
        """The bug this guards: counting the staged candidates as the
        denominator makes every source read as 100% repair language."""
        direct, staged, _ = screened
        assert staged[1].lines_with_text == direct[1].lines_with_text
        assert staged[1].lines_with_text > staged[1].lines_with_repair_language
