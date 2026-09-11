"""W5.1 and W5.2 against the seeded database -- the measured figures.

Every number asserted here was measured on 11-Sep-2026 and is quoted in the
task plan. They are in the suite because they are the cheapest possible alarm:
if the register silently starts returning 770 lines instead of 1,225, the cause
is an inner join to EKKO, and nothing else in the system would say so.

The whole module skips when the extracts are not loaded, which is the normal
state in CI -- the ~800 MB delivery will never live there.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.core.db import get_sessionmaker
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.register import (
    fetch_candidate_lines,
    is_repair_line,
    load_repair_lines,
    open_repair_index,
)
from app.initiatives.i8.universe import load_universe
from app.initiatives.i8.vendors import UNKNOWN_VENDOR, vendor_turnaround
from tests.i8_support import needs_views

pytestmark = needs_views

# Pinned so these assertions are about the data, not about what day it is.
REFERENCE_DATE = date(2026, 9, 11)


@pytest.fixture(scope="module")
def cfg() -> I8Settings:
    return I8Settings(_env_file=None)


@pytest.fixture(scope="module")
def db():
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="module")
def register(db, cfg):
    """Built once for the module -- it reads roughly 400,000 rows."""
    return load_repair_lines(db, cfg, today=REFERENCE_DATE)


@pytest.fixture(scope="module")
def universe(db, cfg, register):
    """The W5.1 read model, with W5.2's open repairs joined in."""
    lines, _stats = register
    return load_universe(db, cfg, open_repair_index=open_repair_index(lines))


@pytest.fixture(scope="module")
def vendors(register):
    lines, _stats = register
    return vendor_turnaround(lines)


# --- Layer 1: identifying repair lines ------------------------------------


class TestRepairLineIdentification:
    def test_repair_line_count(self, register) -> None:
        """1,225. If this says 770, someone inner-joined EKKO and 455 genuine
        repair lines have silently disappeared."""
        lines, _ = register
        assert len(lines) == 1225

    def test_the_header_less_lines_are_still_present(self, register) -> None:
        """455 repair lines have no EKKO header in this extract, because
        raw_ekko starts at 07-Jan-2025 and raw_ekpo reaches further back.
        They are real repair lines and must be in the register."""
        lines, stats = register
        assert stats.lines_with_po_header == 770
        assert len(lines) - stats.lines_with_po_header == 455

    def test_zrep_corroborates_but_does_not_gate(self, register) -> None:
        """Every line that HAS a header says ZREP -- so the convention holds --
        and the ones without a header are kept anyway."""
        _lines, stats = register
        assert stats.lines_corroborated_by_doc_type == stats.lines_with_po_header

    def test_every_repair_line_is_on_an_eighty_series_material(
        self, register
    ) -> None:
        """1,225 of 1,225. The two independent signals -- item category and the
        material-number convention -- agree completely."""
        lines, stats = register
        assert stats.lines_on_eighty_series == len(lines)

    def test_distinct_materials_on_repair_lines(self, register) -> None:
        _lines, stats = register
        assert stats.distinct_materials == 371

    def test_the_register_is_keyed_on_document_and_item(self, register) -> None:
        """Material + repair-PO-LINE grain, as the task name says. If the key
        were the document, the register would collapse to 140-odd rows."""
        lines, _ = register
        keys = {line.key for line in lines}
        assert len(keys) == len(lines)
        assert len({line.purchasing_document for line in lines}) < len(lines)


# --- Ruling 5.2: the Pstyp filter -----------------------------------------


class TestPstypIsFilteredInPython:
    """SAP accepts a $filter on Pstyp, answers HTTP 200 and ignores it.

    These are the tests that catch the ruling regressing. The failure it
    guards against is silent: no exception, no error status, just a register
    full of ordinary purchase orders.
    """

    def test_the_candidate_pull_carries_no_item_category_predicate(self) -> None:
        from app.initiatives.i8.register import _CANDIDATES_SQL

        sql = _CANDIDATES_SQL.lower()
        statement = sql.split("where", 1)[1] if "where" in sql else sql
        assert "pstyp" not in statement, (
            "The EKPO pull must not filter on item category. SAP ignores that "
            "filter and returns HTTP 200 with the whole set."
        )

    def test_the_pull_really_does_return_non_repair_rows(self, db, cfg) -> None:
        """Proves the Python predicate is doing the work.

        If the SQL had quietly acquired the filter, the pull and the filtered
        result would be the same size and the predicate would be decorative.
        """
        candidates = fetch_candidate_lines(db)
        repair = [row for row in candidates if is_repair_line(row, cfg)]
        assert len(candidates) == 82718
        assert len(repair) == 1225
        assert len(candidates) > len(repair) * 10

    def test_the_sap_client_refuses_a_pstyp_filter(self) -> None:
        """The other half of the ruling, at the boundary that will matter after
        cutover: the CPI client must not send this filter at all."""
        from app.integrations.sap.errors import UnsupportedFilterError
        from app.integrations.sap.filters import check_filter, verdict_for

        assert verdict_for("PurchaseOrderItemSet", "Pstyp") == "IGNORED"
        with pytest.raises(UnsupportedFilterError):
            check_filter("PurchaseOrderItemSet", "Pstyp eq '3'")

    def test_the_filters_the_pull_does_use_are_honoured_by_sap(self) -> None:
        """Werks and Matnr are the only narrowing the pull applies."""
        from app.integrations.sap.filters import verdict_for

        assert verdict_for("PurchaseOrderItemSet", "Werks") == "HONOURED"
        assert verdict_for("PurchaseOrderItemSet", "Matnr") == "HONOURED"


# --- Layer 2 and 3: lifecycle, aging and overdue --------------------------


class TestLifecycleAndAging:
    def test_open_and_received_counts(self, register) -> None:
        """437 received / 788 open, net of reversals.

        The task plan quotes 444 / 781 from a raw count of 101 movements. The
        difference is the 7 lines whose only receipt was fully reversed -- and
        netting them off is exactly what the plan's own test case asks for.
        """
        _lines, stats = register
        assert stats.received_lines == 437
        assert stats.open_lines == 788
        assert stats.received_lines + stats.open_lines == stats.total_lines

    def test_reversals_are_netted_not_counted(self, register) -> None:
        lines, stats = register
        assert stats.lines_with_reversals == 19
        reversed_lines = [line for line in lines if line.reversals]
        fully_reversed = [line for line in reversed_lines if line.received_qty <= 0]
        assert len(fully_reversed) == 7
        for line in fully_reversed:
            assert line.received_at is None, (
                "A receipt that was entirely reversed must not read as a return"
            )
            assert line.is_open

    def test_no_due_date_lines(self, register) -> None:
        """63 lines have no EKET schedule line at all; 61 of those are still
        open and therefore chaseable. Neither is a silent "not overdue"."""
        _lines, stats = register
        assert stats.lines_without_due_date == 63
        assert stats.no_due_date_lines == 61

    def test_no_due_date_never_crashes_and_never_passes_silently(
        self, register
    ) -> None:
        lines, _ = register
        undated = [line for line in lines if line.due_date is None]
        assert undated
        for line in undated:
            assert line.overdue_status in ("NO_DUE_DATE", "RECEIVED")
            assert line.is_overdue is False
            assert line.days_remaining is None

    def test_aging_buckets_are_populated_and_valid(self, register) -> None:
        from app.initiatives.i8.aging import AGING_BUCKETS

        lines, _ = register
        buckets = {line.aging_bucket for line in lines}
        assert buckets
        assert buckets <= set(AGING_BUCKETS)

    def test_every_line_has_a_raised_date(self, register) -> None:
        """EKPO.creation_date is populated on all 1,225, which is why daysOpen
        can be reported even for the 455 lines with no PO header."""
        lines, _ = register
        assert all(line.raised_at is not None for line in lines)
        assert all(line.days_open is not None for line in lines)

    def test_dispatch_is_only_attached_where_the_data_supports_it(
        self, register
    ) -> None:
        """286 lines have an attachable 541, and NONE of them is still open.

        A finding rather than a defect: in this extract dispatch movements
        exist only for repairs that have already come back, so the at-vendor
        clock cannot be shown for open work.
        """
        _lines, stats = register
        assert stats.lines_with_dispatch == 286
        assert stats.open_lines_with_dispatch == 0

    def test_days_at_vendor_needs_both_ends_of_the_clock(self, register) -> None:
        lines, _ = register
        for line in lines:
            if line.dispatched_at is None:
                assert line.days_at_vendor is None
            else:
                assert line.days_at_vendor is not None

    def test_grace_period_changes_the_overdue_count_and_nothing_else(
        self, db
    ) -> None:
        strict, strict_stats = load_repair_lines(
            db, I8Settings(_env_file=None, overdue_grace_days=0), today=REFERENCE_DATE
        )
        lenient, lenient_stats = load_repair_lines(
            db, I8Settings(_env_file=None, overdue_grace_days=365), today=REFERENCE_DATE
        )
        assert strict_stats.overdue_lines > lenient_stats.overdue_lines
        assert strict_stats.total_lines == lenient_stats.total_lines
        assert strict_stats.open_lines == lenient_stats.open_lines
        assert strict_stats.received_lines == lenient_stats.received_lines
        assert len(strict) == len(lenient)

    def test_the_reference_date_moves_the_aging_not_the_population(
        self, db, register
    ) -> None:
        _lines, stats = register
        earlier, earlier_stats = load_repair_lines(
            db, I8Settings(_env_file=None), today=date(2026, 7, 31)
        )
        assert earlier_stats.total_lines == stats.total_lines
        assert earlier_stats.overdue_lines < stats.overdue_lines


# --- W5.1 universe --------------------------------------------------------


class TestUniverse:
    def test_detection_is_not_a_mara_lookup(self, universe) -> None:
        """The measurement that decides the whole design.

        MARA knows 362 of the series. The union of every source knows 3,605.
        Gating on the material master would discard roughly nine in ten.
        """
        _rows, stats = universe
        assert stats.by_source["mara"] == 362
        assert stats.materials == 3605
        assert stats.materials > stats.by_source["mara"] * 9

    def test_every_row_passes_the_predicate(self, universe, cfg) -> None:
        from app.initiatives.i8.material_number import is_eighty_series

        rows, _ = universe
        assert all(is_eighty_series(row.material_id, cfg) for row in rows)

    def test_stock_is_summed_across_storage_locations(self, universe) -> None:
        """MARD is one row per bin. Taking the first row under-reports stock,
        which is the exact failure I08 exists to prevent."""
        rows, _ = universe
        multi_bin = [row for row in rows if row.storage_locations > 1]
        assert multi_bin, "expected materials held in more than one bin"
        assert any(row.stock_on_hand and row.stock_on_hand > 0 for row in multi_bin)

    def test_partial_sources_render_as_null_never_as_a_default(
        self, universe
    ) -> None:
        rows, stats = universe
        assert stats.with_criticality < stats.rows
        assert stats.with_reorder_point < stats.rows
        assert any(row.criticality is None for row in rows)
        assert any(row.material_type is None for row in rows)

    def test_criticality_is_never_invented(self, universe) -> None:
        from app.initiatives.i8.criticality import KNOWN_TIERS

        rows, _ = universe
        tiers = {row.criticality for row in rows if row.criticality}
        assert tiers <= KNOWN_TIERS
        # And the unknown ones are genuinely unknown, not quietly NORMAL.
        assert any(row.criticality is None for row in rows)

    def test_marc_covers_only_black_mountain(self, universe) -> None:
        """A known delivery gap, recorded in app/seed/manifest.py. Gamsberg
        rows must render with a null reorder point, not a zero."""
        rows, _ = universe
        gamsberg = [row for row in rows if row.plant == "1500"]
        assert gamsberg
        assert all(row.reorder_point is None for row in gamsberg)

    def test_open_repairs_link_the_two_read_models(self, universe, register) -> None:
        lines, _ = register
        rows, stats = universe
        assert stats.with_open_repair > 0
        flagged = {(row.material_id, row.plant) for row in rows if row.has_open_repair}
        expected = set(open_repair_index(lines))
        assert flagged == expected


# --- Layer 4: vendor analytics --------------------------------------------


class TestVendorTurnaround:
    def test_open_repairs_are_excluded_from_the_average(self, vendors) -> None:
        """Including them makes the slowest vendor look fastest, because its
        repairs have not come back to be counted yet."""
        for vendor in vendors:
            if vendor.avg_turnaround_days is not None:
                assert vendor.turnaround_sample > 0
                assert vendor.turnaround_sample <= vendor.received_count

    def test_a_vendor_with_no_completed_repair_has_no_average(self, vendors) -> None:
        never_returned = [v for v in vendors if v.received_count == 0]
        assert never_returned
        for vendor in never_returned:
            assert vendor.avg_turnaround_days is None
            assert vendor.on_time_rate is None

    def test_header_less_lines_are_grouped_not_dropped(self, vendors, register) -> None:
        lines, _ = register
        unknown = next(v for v in vendors if v.vendor == UNKNOWN_VENDOR)
        assert unknown.total_lines == 455
        assert sum(v.total_lines for v in vendors) == len(lines)

    def test_unnamed_vendors_keep_their_code(self, vendors) -> None:
        """LFA1 resolves only 4 of the 61 repair vendors. The other 57 must
        still appear -- with their code -- rather than be hidden."""
        named = [v for v in vendors if v.vendor_name]
        assert 0 < len(named) < len(vendors)
        for vendor in vendors:
            assert vendor.vendor

    def test_no_personal_data_reaches_the_vendor_model(self) -> None:
        """POPIA. raw_lfa1 has 179 columns including date of birth and BEE
        ownership; v_lfa1 promotes four business fields and the analytics
        expose two of them."""
        from app.initiatives.i8.vendors import VendorTurnaround

        fields = set(VendorTurnaround.__dataclass_fields__)
        forbidden = {"date_of_birth", "telephone_1", "street", "postal_code", "sex"}
        assert not fields & forbidden
        assert fields & {"vendor", "vendor_name"}
