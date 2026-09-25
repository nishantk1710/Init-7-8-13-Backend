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
from sqlalchemy import text

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
        """1,181: the 1,225 item-category-3 lines less the 44 SAP has deleted.
        If this says 743, someone inner-joined EKKO and 438 genuine repair lines
        have silently disappeared."""
        lines, stats = register
        assert len(lines) == 1181
        assert stats.excluded_deleted_lines == 44

    def test_deleted_lines_are_excluded_and_blocked_ones_flagged(self, register) -> None:
        """Measured against the view directly, not against the code's own count."""
        from app.core.db import get_engine

        lines, stats = register
        with get_engine().connect() as connection:
            deleted, blocked = connection.execute(
                text(
                    "select count(*) filter (where loekz = 'L'), "
                    "count(*) filter (where loekz = 'S') "
                    "from v_ekpo where pstyp = '3'"
                )
            ).one()
            deleted_keys = {
                (row.ebeln, row.ebelp)
                for row in connection.execute(
                    text("select ebeln, ebelp from v_ekpo where pstyp = '3' and loekz = 'L'")
                )
            }
        assert stats.excluded_deleted_lines == deleted
        assert stats.blocked_lines == blocked == sum(1 for line in lines if line.po_blocked)
        assert not deleted_keys & {line.key for line in lines}

    def test_the_header_less_lines_are_still_present(self, register) -> None:
        """438 repair lines have no EKKO header in this extract (455 before
        deleted lines were excluded), because raw_ekko starts at 07-Jan-2025 and
        raw_ekpo reaches further back. They are real repair lines and must be in
        the register."""
        lines, stats = register
        assert stats.lines_with_po_header == 743
        assert len(lines) - stats.lines_with_po_header == 438

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
        assert stats.distinct_materials == 365

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
        # 82,718 before the two-plant scope ruling of 21-Sep-2026.
        assert len(candidates) == 80880
        assert len(repair) == 1225
        assert len(candidates) > len(repair) * 10

    def test_the_sap_client_refuses_a_pstyp_filter(self) -> None:
        """The other half of the ruling, at the boundary that will matter after
        cutover: the CPI client must not send this filter at all.

        **The recorded verdict changed on 15-Sep, and the ruling did not.**
        Nobody weakened this test to make it pass. SAP used to accept a Pstyp
        filter, return HTTP 200, and silently ignore it (IGNORED); it now
        refuses the call outright (REJECTED_HTTP_500). Both verdicts mean the
        same thing for us -- *you still cannot send this filter* -- so "apply
        the Pstyp filter on the EKPO pull, not in the query" is still exactly
        right, and no code changed with this assertion.

        The change is loud rather than silent, which is strictly better: an
        IGNORED filter produces confident wrong numbers, a 500 produces an
        error. The transition is evidenced in the 15-Sep sweep, reproduced
        independently hours apart -- see the task plan section 3.2.

        The assertion is deliberately against the REJECTED constant rather than
        the string, so the vocabulary lives in one place.
        """
        from app.integrations.sap.errors import UnsupportedFilterError
        from app.integrations.sap.filters import REJECTED, check_filter, verdict_for

        assert verdict_for("PurchaseOrderItemSet", "Pstyp") == REJECTED

        # This is the assertion that carries the ruling, and it is unchanged:
        # the client refuses the filter either way. check_filter() rejects
        # IGNORED and REJECTED alike, so the guard did not weaken when the
        # verdict moved.
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
        """437 received / 744 open, net of reversals (788 open before the 44
        deleted lines -- none of which had a receipt -- were excluded).

        The task plan quotes 444 / 781 from a raw count of 101 movements. The
        difference is the 7 lines whose only receipt was fully reversed -- and
        netting them off is exactly what the plan's own test case asks for.
        """
        _lines, stats = register
        assert stats.received_lines == 437
        assert stats.open_lines == 744
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
        """59 lines have no EKET schedule line at all; 57 of those are still
        open and therefore chaseable. Neither is a silent "not overdue". (63 /
        61 before deleted lines were excluded.)"""
        _lines, stats = register
        assert stats.lines_without_due_date == 59
        assert stats.no_due_date_lines == 57

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

    def test_aging_band_boundaries_change_the_distribution_and_nothing_else(
        self, db
    ) -> None:
        narrow, narrow_stats = load_repair_lines(
            db,
            I8Settings(_env_file=None, aging_band_boundaries="5,10"),
            today=REFERENCE_DATE,
        )
        wide, wide_stats = load_repair_lines(
            db,
            I8Settings(_env_file=None, aging_band_boundaries="1000,2000"),
            today=REFERENCE_DATE,
        )
        narrow_buckets = {line.aging_bucket for line in narrow}
        wide_buckets = {line.aging_bucket for line in wide}
        assert narrow_buckets != wide_buckets
        assert narrow_stats.total_lines == wide_stats.total_lines
        assert narrow_stats.open_lines == wide_stats.open_lines
        assert len(narrow) == len(wide)

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


class TestLeadTimeAgainstTheExtract:
    """MARC.PLIFZ over the real 1,225 lines.

    Every assertion here is about COVERAGE rather than about a particular
    breach count, because the breach count is only meaningful next to the
    population it was measured over -- and that population has a known hole in
    it (see the first test).
    """

    def test_gamsberg_has_no_lead_time_at_all(self, register) -> None:
        """Not a bug here -- a gap in the delivery.

        The July MARC extract has ZERO rows for Gamsberg -- in scope it is
        plant 1300 only (see app/seed/manifest.py). Both in-scope plants carry
        repair lines, so every Gamsberg line resolves to NO_LEAD_TIME until
        a MARC extract covering 1500 arrives. Asserted rather than assumed, so
        the day that extract lands this test fails and says so.
        """
        lines, _stats = register
        gamsberg = [line for line in lines if line.plant == "1500"]
        assert gamsberg, "expected repair lines at Gamsberg"
        assert all(line.lead_time_days is None for line in gamsberg)
        assert all(line.lead_time_status == "NO_LEAD_TIME" for line in gamsberg)

    def test_coverage_is_reported_not_absorbed(self, register) -> None:
        """A breach count without its population is not a number anyone can use."""
        lines, stats = register
        assert stats.lines_with_lead_time == sum(
            1 for line in lines if line.lead_time_days
        )
        assert stats.lines_with_lead_time < stats.total_lines, (
            "if this ever equals the total, MARC now covers every plant and the "
            "Gamsberg caveat in the UAT pack is out of date"
        )

    def test_a_breach_never_comes_from_an_unmaintained_plifz(self, register) -> None:
        """PLIFZ is 0 or blank on most non-stock materials. Reading it literally
        would put every such line into breach on the day its PO was raised."""
        lines, _stats = register
        for line in lines:
            if line.lead_time_status != "NO_LEAD_TIME":
                assert line.lead_time_days and line.lead_time_days > 0

    def test_the_two_signals_are_independent(self, register) -> None:
        """The reason the check runs on all lines and not only the 63.

        If lead time were merely a fallback for a missing due date, no line
        could ever be both ON_TIME and BEYOND_LEAD_TIME. Lines in that state are
        the finding the ruling asked for.
        """
        lines, _stats = register
        assert any(
            line.overdue_status in {"ON_TIME", "RECEIVED"}
            and line.lead_time_status == "BEYOND_LEAD_TIME"
            for line in lines
        )

    def test_it_reaches_lines_that_were_never_dispatched(self, register) -> None:
        """Anchored on the PO date, not the dispatch.

        788 lines have no dispatch movement at all. Anchoring the lead-time
        clock on the 541 would have left every one of them unmeasurable -- the
        same blind spot the check was asked for to close.
        """
        lines, _stats = register
        measured = [
            line
            for line in lines
            if line.dispatched_at is None and line.lead_time_status != "NO_LEAD_TIME"
        ]
        assert measured, "the lead-time clock must not depend on a dispatch date"

    def test_the_clock_stops_at_the_receipt(self, register) -> None:
        lines, _stats = register
        for line in lines:
            if line.received_at is not None and line.raised_at is not None:
                assert line.days_elapsed == (line.received_at - line.raised_at).days


class TestUniverse:
    def test_detection_is_not_a_mara_lookup(self, universe) -> None:
        """The measurement that decides the whole design.

        MARA knows 362 of the series. The union of every source knows 3,145.
        Gating on the material master would discard roughly eight in nine.

        The two-plant scope ruling of 21-Sep-2026 moved the totals (3,605
        materials over 5 plants before it) but not the conclusion, which is
        the point of pinning the ratio as well as the count: narrowing the
        scope does not make the material master a usable gate.
        """
        _rows, stats = universe
        assert stats.by_source["mara"] == 362
        assert stats.materials == 3145
        assert stats.plants == 2
        assert stats.materials > stats.by_source["mara"] * 8

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
        # The five tiers now come from the shared W3.4 enum, not an I08 copy
        # of the list. That is the assertion that matters: I08 cannot drift to a
        # sixth tier of its own, because it no longer owns the vocabulary.
        from app.shared import CriticalityTier

        KNOWN_TIERS = {tier.value for tier in CriticalityTier}

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
        assert unknown.total_lines == 438
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
