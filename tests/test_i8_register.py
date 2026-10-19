"""W5.1 and W5.2 against the seeded database -- the measured figures.

Every number asserted here was measured on 11-Sep-2026 and is quoted in the
task plan. They are in the suite because they are the cheapest possible alarm:
if the register silently starts returning 770 lines instead of 1,225, the cause
is an inner join to EKKO, and nothing else in the system would say so.

The whole module skips when the extracts are not loaded, which is the normal
state in CI -- the ~800 MB delivery will never live there.

Since 19-Oct-2026 the register holds only the lines SAP still has open ("Delivery
completed" not set, not deleted, not blocked). The counts that depend on that
rule were never measured on this extract under it, so they are measured here,
against the view, rather than quoted.
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
from tests.i8_support import needs_views

pytestmark = needs_views

# Pinned so these assertions are about the data, not about what day it is.
REFERENCE_DATE = date(2026, 9, 11)

# The repair lines SAP still has open, as the view holds them: item category 3,
# not deleted ('L') or blocked ('S'), and not "Delivery completed" ('X').
OPEN_IN_SAP = (
    "pstyp = '3' and ebeln is not null and ebelp is not null "
    "and coalesce(loekz, '') not in ('L', 'S') and coalesce(elikz, '') <> 'X'"
)


def _measured(sql: str):
    """One row from the view, measured directly rather than through the code."""
    from app.core.db import get_engine

    with get_engine().connect() as connection:
        return connection.execute(text(sql)).one()


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


# --- Layer 1: identifying repair lines ------------------------------------


class TestRepairLineIdentification:
    def test_the_register_is_what_sap_has_open(self, register) -> None:
        """The 1,225 item-category-3 lines, each counted once: open, or left out
        as deleted (44), delivery completed or blocked -- measured against the
        view directly, not against the code's own count."""
        lines, stats = register
        deleted, closed, blocked, still_open = _measured(
            "select count(*) filter (where loekz = 'L'), "
            "count(*) filter (where coalesce(loekz, '') <> 'L' and elikz = 'X'), "
            "count(*) filter (where loekz = 'S' and coalesce(elikz, '') <> 'X'), "
            f"count(*) filter (where {OPEN_IN_SAP}) "
            "from v_ekpo where pstyp = '3' and ebeln is not null and ebelp is not null"
        )
        assert stats.excluded_deleted_lines == deleted == 44
        assert stats.excluded_delivery_completed_lines == closed
        assert stats.excluded_blocked_lines == blocked
        assert len(lines) == stats.total_lines == stats.open_lines == still_open
        assert deleted + closed + blocked + still_open == 1225

    def test_no_line_sap_has_closed_is_in_the_register(self, register) -> None:
        from app.core.db import get_engine

        lines, _stats = register
        with get_engine().connect() as connection:
            closed_keys = {
                (row.ebeln, row.ebelp)
                for row in connection.execute(
                    text(
                        "select ebeln, ebelp from v_ekpo where pstyp = '3' "
                        "and (loekz in ('L', 'S') or elikz = 'X')"
                    )
                )
            }
        assert not closed_keys & {line.key for line in lines}

    def test_the_header_less_lines_are_still_present(self, register) -> None:
        """raw_ekko starts at 07-Jan-2025 and raw_ekpo reaches further back, so
        some repair lines have no EKKO header. They are real repair lines: an
        inner join to EKKO would silently drop them."""
        lines, stats = register
        (headerless,) = _measured(
            f"select count(*) from v_ekpo p where {OPEN_IN_SAP} "
            "and not exists (select 1 from v_ekko h where h.ebeln = p.ebeln)"
        )
        assert len(lines) - stats.lines_with_po_header == headerless

    def test_zrep_corroborates_but_does_not_gate(self, register) -> None:
        """Every line that HAS a header says ZREP -- so the convention holds --
        and the ones without a header are kept anyway."""
        _lines, stats = register
        assert stats.lines_corroborated_by_doc_type == stats.lines_with_po_header

    def test_every_repair_line_is_on_an_eighty_series_material(
        self, register
    ) -> None:
        """The two independent signals -- item category and the material-number
        convention -- agree completely."""
        lines, stats = register
        assert stats.lines_on_eighty_series == len(lines)

    def test_distinct_materials_on_repair_lines(self, register) -> None:
        _lines, stats = register
        (materials,) = _measured(f"select count(distinct matnr) from v_ekpo where {OPEN_IN_SAP}")
        assert stats.distinct_materials == materials

    def test_the_register_is_keyed_on_document_and_item(self, register) -> None:
        """Material + repair-PO-LINE grain, as the task name says. If the key
        were the document, lines on one PO would collapse into one row."""
        lines, _ = register
        keys = {line.key for line in lines}
        assert len(keys) == len(lines)
        assert len({line.purchasing_document for line in lines}) <= len(lines)


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
    def test_every_line_is_open_and_out_in_full(self, register) -> None:
        """Goods receipts are not read: a line is open until SAP closes it, and
        its whole ordered quantity is out until then."""
        lines, stats = register
        assert stats.open_lines == stats.total_lines == len(lines)
        assert all(line.qty_under_repair == line.ordered_qty for line in lines)

    def test_no_due_date_lines(self, register) -> None:
        """Lines with no EKET schedule line at all. They are chaseable, never a
        silent "not overdue"."""
        lines, stats = register
        assert stats.no_due_date_lines == sum(1 for line in lines if line.due_date is None)

    def test_no_due_date_never_crashes_and_never_passes_silently(
        self, register
    ) -> None:
        lines, _ = register
        for line in lines:
            if line.due_date is None:
                assert line.overdue_status == "NO_DUE_DATE"
                assert line.is_overdue is False
                assert line.days_remaining is None

    def test_aging_buckets_are_populated_and_valid(self, register) -> None:
        from app.initiatives.i8.aging import AGING_BUCKETS

        lines, _ = register
        buckets = {line.aging_bucket for line in lines}
        assert buckets
        assert buckets <= set(AGING_BUCKETS)

    def test_every_line_has_a_raised_date(self, register) -> None:
        """EKPO.creation_date is populated on every repair line, which is why
        daysOpen can be reported even for the lines with no PO header."""
        lines, _ = register
        assert all(line.raised_at is not None for line in lines)
        assert all(line.days_open is not None for line in lines)

    def test_a_dispatch_is_what_puts_a_line_at_the_vendor(self, register) -> None:
        lines, stats = register
        assert stats.lines_with_dispatch == sum(1 for line in lines if line.dispatched_at)
        for line in lines:
            expected = "At Vendor" if line.dispatched_at else "PO Issued"
            assert line.repair_status == expected

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
        assert strict_stats.overdue_lines >= lenient_stats.overdue_lines
        assert strict_stats.total_lines == lenient_stats.total_lines
        assert strict_stats.open_lines == lenient_stats.open_lines
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
        assert earlier_stats.overdue_lines <= stats.overdue_lines


# --- W5.1 universe --------------------------------------------------------


class TestLeadTimeAgainstTheExtract:
    """MARC.PLIFZ over the real open repair lines.

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
        assert all(line.lead_time_days is None for line in gamsberg)
        assert all(line.lead_time_status == "NO_LEAD_TIME" for line in gamsberg)

    def test_coverage_is_reported_not_absorbed(self, register) -> None:
        """A breach count without its population is not a number anyone can use."""
        lines, stats = register
        assert stats.lines_with_lead_time == sum(
            1 for line in lines if line.lead_time_days
        )
        if any(line.plant == "1500" for line in lines):
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

    def test_it_reaches_lines_that_were_never_dispatched(self, register) -> None:
        """Anchored on the PO date, not the dispatch.

        Most open lines have no dispatch movement at all. Anchoring the
        lead-time clock on the 541 would have left every one of them
        unmeasurable -- the same blind spot the check was asked for to close.
        (That the two signals are independent is pinned in test_i8_rules.py.)
        """
        lines, _stats = register
        for line in lines:
            if line.dispatched_at is None and line.lead_time_days:
                assert line.lead_time_status != "NO_LEAD_TIME"

    def test_the_clock_runs_to_the_reference_date(self, register) -> None:
        """Every line is open in SAP, so nothing stops its clock early."""
        lines, _stats = register
        for line in lines:
            assert line.days_elapsed == (REFERENCE_DATE - line.raised_at).days


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


# --- Vendors on the line ---------------------------------------------------
#
# No vendor turnaround since 19-Oct-2026: it was dispatch to goods receipt over
# closed repairs, and the register reads neither receipts nor closed lines.


class TestVendorsOnTheLine:
    def test_unnamed_vendors_keep_their_code(self, register) -> None:
        """LFA1 resolves few repair vendors to a name. The rest must still
        appear -- with their code -- rather than be hidden."""
        lines, stats = register
        assert stats.vendors_resolved_to_a_name <= stats.distinct_vendors
        for line in lines:
            if line.vendor_name:
                assert line.vendor

    def test_no_personal_data_reaches_the_repair_line(self) -> None:
        """POPIA. raw_lfa1 has 179 columns including date of birth and BEE
        ownership; v_lfa1 promotes four business fields and a repair line
        carries two of them."""
        from app.initiatives.i8.register import RepairLine

        fields = set(RepairLine.__dataclass_fields__)
        forbidden = {"date_of_birth", "telephone_1", "street", "postal_code", "sex"}
        assert not fields & forbidden
        assert {"vendor", "vendor_name"} <= fields
