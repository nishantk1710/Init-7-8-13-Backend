"""W5.2 -- aging, overdue and the status derivations, as pure rules.

No database. Every case here is a decision the register makes about one line,
and each one is stated in the task plan as something that must be got right:
which lines SAP still has open, missing due dates, the grace period, and the
exact aging bands the frontend already renders.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.initiatives.i8 import register
from app.initiatives.i8.aging import (
    AGING_BUCKETS,
    BEYOND_LEAD_TIME,
    NO_DUE_DATE,
    NO_LEAD_TIME,
    ON_TIME,
    OVERDUE,
    WITHIN_LEAD_TIME,
    aging_bucket,
    days_between,
    days_over_lead_time,
    days_remaining,
    elapsed_days,
    lead_time_state,
    overdue_state,
)
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.register import (
    AT_VENDOR,
    PO_ISSUED,
    is_blocked_line,
    is_deleted_line,
    is_delivery_completed,
    is_rfq_line,
    load_repair_lines,
    repair_status,
)

TODAY = date(2026, 9, 11)


def _row(**overrides):
    """One EKPO candidate row, as the view serves it: an open repair line."""
    row = {
        "ebeln": "4500000001",
        "ebelp": "00010",
        "matnr": "8000005632",
        "werks": "1300",
        "pstyp": "3",
        "txz01": "REPAIR OF PUMP",
        "menge": Decimal(1),
        "meins": "EA",
        "netpr": Decimal(100),
        "elikz": "",
        "banfn": None,
        "bnfpo": None,
        "erdat": date(2026, 6, 1),
        "loekz": None,
        "afnam": "10316",
    }
    row.update(overrides)
    return row


# --- aging buckets --------------------------------------------------------


class TestAgingBuckets:
    @pytest.mark.parametrize(
        ("days", "bucket"),
        [
            (0, "0-15"),
            (15, "0-15"),
            (16, "16-30"),
            (30, "16-30"),
            (31, "31-45"),
            (45, "31-45"),
            (46, "46-60"),
            (60, "46-60"),
            (61, "60+"),
            (5000, "60+"),
        ],
    )
    def test_boundaries(self, days: int, bucket: str) -> None:
        assert aging_bucket(days) == bucket

    def test_bands_match_the_frontend(self) -> None:
        """Init-7-8-13-Frontend .../types/repair.ts defines AgingBucket.

        Re-cutting a band here without changing it there produces a value the
        UI cannot render, and the failure shows up as a blank column.
        """
        assert AGING_BUCKETS == ("0-15", "16-30", "31-45", "46-60", "60+")
        assert set(aging_bucket(d) for d in (0, 20, 40, 55, 900)) <= set(AGING_BUCKETS)

    def test_unknown_age_is_not_bucketed(self) -> None:
        assert aging_bucket(None) is None

    def test_negative_age_is_day_zero_not_the_oldest_bucket(self) -> None:
        """The extract contains future dates. Without the clamp they would
        arrive as the most-overdue band, which is the opposite of the truth."""
        assert aging_bucket(-40) == "0-15"

    def test_boundaries_are_configurable(self) -> None:
        """FRS open item 5: aging bands are a placeholder pending VZI
        calibration, and NFR-6 requires them to be configuration, not code."""
        custom = (10, 20)
        assert aging_bucket(5, custom) == "0-10"
        assert aging_bucket(15, custom) == "11-20"
        assert aging_bucket(21, custom) == "20+"
        # The default is untouched -- passing custom boundaries never leaks
        # into a call that didn't ask for them.
        assert aging_bucket(21) == "16-30"
        assert aging_bucket(65) == "60+"


# --- overdue --------------------------------------------------------------


class TestOverdueState:
    def test_missing_due_date_is_its_own_state(self) -> None:
        """They are NOT "not overdue" -- they are the lines nobody agreed a date
        for, and they need to be chaseable."""
        assert overdue_state(due_date=None, today=TODAY, grace_days=7) == NO_DUE_DATE

    def test_grace_period_is_applied(self) -> None:
        due = date(2026, 9, 1)
        # 10 days past due, 7 days grace -> overdue.
        assert overdue_state(due_date=due, today=TODAY, grace_days=7) == OVERDUE
        # Same line, 30 days grace -> not yet.
        assert overdue_state(due_date=due, today=TODAY, grace_days=30) == ON_TIME

    def test_the_grace_boundary_is_exclusive(self) -> None:
        """Due + grace is the last good day, not the first bad one."""
        due = date(2026, 9, 4)  # + 7 days grace == 2026-09-11 == TODAY
        assert overdue_state(due_date=due, today=TODAY, grace_days=7) == ON_TIME
        assert (
            overdue_state(due_date=due, today=date(2026, 9, 12), grace_days=7)
            == OVERDUE
        )

    def test_zero_grace_is_honoured(self) -> None:
        assert (
            overdue_state(due_date=date(2026, 9, 10), today=TODAY, grace_days=0)
            == OVERDUE
        )

    def test_future_due_date_is_on_time(self) -> None:
        assert (
            overdue_state(due_date=date(2028, 4, 30), today=TODAY, grace_days=7)
            == ON_TIME
        )


class TestDaysArithmetic:
    def test_days_between(self) -> None:
        assert days_between(date(2026, 1, 1), date(2026, 3, 2)) == 60

    def test_unknown_end_of_the_clock_is_none_not_zero(self) -> None:
        """None and 0 are different answers, and averaging them together is how
        a vendor with no recorded dispatch ends up looking instantaneous."""
        assert days_between(None, TODAY) is None
        assert days_between(TODAY, None) is None

    def test_days_remaining_goes_negative_once_overdue(self) -> None:
        assert days_remaining(date(2026, 9, 20), TODAY) == 9
        assert days_remaining(date(2026, 9, 1), TODAY) == -10
        assert days_remaining(None, TODAY) is None


# --- status derivation ----------------------------------------------------


class TestRepairStatus:
    """Every register line is open in SAP, so it is one of two things."""

    def test_po_raised_but_nothing_moved(self) -> None:
        assert repair_status(dispatched_at=None) == PO_ISSUED

    def test_a_dispatch_puts_it_at_the_vendor(self) -> None:
        assert repair_status(dispatched_at=date(2026, 1, 1)) == AT_VENDOR


class TestOpenIsSapsCall:
    """The 19-Oct-2026 ruling: a repair line is open while SAP has it open.

    "Delivery completed" (EKPO.ELIKZ) closes a line whatever was received
    against it, and a deleted or blocked line is not open either -- SAP's own
    open-PO count reads it that way. Measured on the 15-Oct data, the register
    called ~732 lines open where SAP counted ~106; this rule gives 104.
    """

    CFG = I8Settings(_env_file=None)

    def test_delivery_completed_closes_a_line(self) -> None:
        assert is_delivery_completed({"elikz": "X"}, self.CFG) is True

    @pytest.mark.parametrize("flag", ["", None])
    def test_a_line_without_the_flag_is_open(self, flag) -> None:
        assert is_delivery_completed({"elikz": flag}, self.CFG) is False

    def test_the_flag_value_is_configuration(self) -> None:
        cfg = I8Settings(_env_file=None, delivery_completed_indicator="Y")
        assert is_delivery_completed({"elikz": "Y"}, cfg) is True
        assert is_delivery_completed({"elikz": "X"}, cfg) is False

    def test_blocked_and_deleted_are_told_apart(self) -> None:
        assert is_blocked_line({"loekz": "S"}, self.CFG) is True
        assert is_deleted_line({"loekz": "S"}, self.CFG) is False
        assert is_deleted_line({"loekz": "L"}, self.CFG) is True
        assert is_blocked_line({"loekz": "L"}, self.CFG) is False
        assert is_blocked_line({"loekz": None}, self.CFG) is False

    def test_closed_lines_are_counted_and_never_built(self) -> None:
        """Each left-out line is counted under the first reason that applies:
        deleted, then delivery completed, then blocked. Nothing here reaches
        the database -- with no open line there is nothing to look up."""
        candidates = [
            _row(ebelp="10", loekz="L", elikz="X"),  # deleted, and closed
            _row(ebelp="20", elikz="X"),  # closed, nothing received
            _row(ebelp="30", loekz="S", elikz="X"),  # blocked, and closed
            _row(ebelp="40", loekz="S"),  # blocked
            _row(ebelp="50", pstyp="0"),  # not a repair line at all
        ]
        lines, stats = load_repair_lines(None, self.CFG, today=TODAY, candidates=candidates)
        assert lines == []
        assert stats.total_lines == stats.open_lines == 0
        assert stats.excluded_deleted_lines == 1
        assert stats.excluded_delivery_completed_lines == 2
        assert stats.excluded_blocked_lines == 1
        assert stats.candidates_scanned == 5

    def test_only_the_open_line_is_built(self, monkeypatch) -> None:
        """The whole ordered quantity is out, the clock runs to today, and the
        line has no receipt to stop it -- goods receipts are not read."""
        monkeypatch.setattr(register, "_evidence", lambda *args, **kwargs: ({}, {}, {}, {}, {}))
        candidates = [
            _row(ebelp="10", elikz="X"),
            _row(ebelp="20", menge=Decimal(2)),
            _row(ebelp="30", loekz="S"),
        ]
        lines, stats = load_repair_lines(None, self.CFG, today=TODAY, candidates=candidates)
        [line] = lines
        assert line.item == "20"
        assert line.repair_status == PO_ISSUED
        assert line.qty_under_repair == Decimal(2)
        assert line.days_open == days_between(date(2026, 6, 1), TODAY)
        assert stats.total_lines == stats.open_lines == 1
        assert stats.excluded_delivery_completed_lines == 1
        assert stats.excluded_blocked_lines == 1


class TestAnRfqIsNotAnOrder:
    """The 21-Oct-2026 ruling: a line on a request for quotation is left out.

    RFQ 6000016330/10 (document type AN) carried item category 3 and, since
    SAP never ticks "Delivery completed" on an RFQ, read as open -- with a
    quantity of 0, so the Overview showed 105 open lines and 104 under repair.
    """

    CFG = I8Settings(_env_file=None)

    @staticmethod
    def _header(bsart):
        return {"bsart": bsart, "lifnr": "501910", "aedat": date(2026, 10, 8)}

    def test_an_rfq_header_marks_the_line(self) -> None:
        assert is_rfq_line(self._header("AN"), self.CFG) is True
        assert is_rfq_line(self._header("ZREP"), self.CFG) is False
        assert is_rfq_line(self._header(None), self.CFG) is False

    def test_a_line_with_no_header_is_kept(self) -> None:
        """Nothing says it is an RFQ, and a header-less repair line is real."""
        assert is_rfq_line(None, self.CFG) is False

    def test_the_rfq_type_is_configuration(self) -> None:
        cfg = I8Settings(_env_file=None, rfq_doc_type="ZRFQ")
        assert is_rfq_line(self._header("ZRFQ"), cfg) is True
        assert is_rfq_line(self._header("AN"), cfg) is False

    def test_the_rfq_line_is_counted_and_never_built(self, monkeypatch) -> None:
        headers = {
            "4500001645": self._header("ZREP"),
            "6000016330": self._header("AN"),
        }
        monkeypatch.setattr(
            register, "_evidence", lambda *args, **kwargs: ({}, {}, headers, {}, {})
        )
        candidates = [
            _row(ebeln="4500001645", ebelp="30"),
            _row(ebeln="6000016330", ebelp="10", menge=Decimal(0)),
            _row(ebeln="4500000002", ebelp="10"),  # no header: kept
        ]
        lines, stats = load_repair_lines(None, self.CFG, today=TODAY, candidates=candidates)
        assert [line.purchasing_document for line in lines] == ["4500001645", "4500000002"]
        assert stats.total_lines == stats.open_lines == 2
        assert stats.excluded_rfq_lines == 1
        # The two figures the Overview put side by side agree again.
        assert sum(line.qty_under_repair for line in lines) == stats.open_lines

    def test_an_rfq_sap_has_closed_is_counted_as_closed(self, monkeypatch) -> None:
        """Counted under the first reason that applies, and the RFQ test is last."""
        monkeypatch.setattr(
            register,
            "_evidence",
            lambda *args, **kwargs: ({}, {}, {"6000016330": self._header("AN")}, {}, {}),
        )
        candidates = [
            _row(ebeln="6000016330", ebelp="10", elikz="X"),
            _row(ebeln="6000016330", ebelp="20"),
        ]
        lines, stats = load_repair_lines(None, self.CFG, today=TODAY, candidates=candidates)
        assert lines == []
        assert stats.excluded_delivery_completed_lines == 1
        assert stats.excluded_rfq_lines == 1


# --- configuration discipline --------------------------------------------


class TestLeadTime:
    """The 21-Sep ruling: aging beyond MARC.PLIFZ is highlighted on every line.

    Lead time is planned delivery time in calendar days, measured PO to
    received -- the same field and span Initiative 07 uses, confirmed with
    Khushi. No grace period, confirmed with the team lead.
    """

    def test_beyond_the_planned_time(self) -> None:
        assert lead_time_state(elapsed=22, lead_time_days=21) == BEYOND_LEAD_TIME

    def test_exactly_on_the_planned_time_is_within_it(self) -> None:
        """21 days against a 21-day lead time is met, not missed."""
        assert lead_time_state(elapsed=21, lead_time_days=21) == WITHIN_LEAD_TIME

    @pytest.mark.parametrize("plifz", [None, 0])
    def test_unmaintained_plifz_never_breaches(self, plifz) -> None:
        """PLIFZ is blank or zero on most non-stock materials.

        Reading it literally puts every such line into breach on the day its PO
        was raised. A register that is entirely red says nothing, so the safe
        failure is not to flag.
        """
        assert lead_time_state(elapsed=9999, lead_time_days=plifz) == NO_LEAD_TIME
        assert days_over_lead_time(elapsed=9999, lead_time_days=plifz) is None

    def test_no_grace_period(self) -> None:
        """One day over is over. Asked explicitly, answered "No not needed".

        Grace exists because a promised date is a commitment somebody made. A
        planned delivery time is already an average with slack in it, and
        discounting it twice moves a threshold nobody agreed.
        """
        assert lead_time_state(elapsed=22, lead_time_days=21) == BEYOND_LEAD_TIME
        assert days_over_lead_time(elapsed=22, lead_time_days=21) == 1

    def test_an_open_line_keeps_ageing(self) -> None:
        """Raised to today: every register line is open in SAP, and goods
        receipts are not read, so nothing stops the clock but SAP closing the
        line -- at which point it leaves the register."""
        raised = date(2026, 1, 1)
        elapsed = elapsed_days(raised_at=raised, today=TODAY)
        assert elapsed == days_between(raised, TODAY)
        assert lead_time_state(elapsed=elapsed, lead_time_days=21) == BEYOND_LEAD_TIME

    def test_a_line_can_be_on_time_and_beyond_lead_time(self) -> None:
        """The two signals are independent, and disagreement is a finding.

        A buyer who wrote a generous delivery date onto the PO makes the line
        ON_TIME while it has already taken longer than the material normally
        takes. Neither answer overrides the other; that is the whole reason the
        lead-time check applies to all lines rather than only the 63 with no
        date at all.
        """
        state = overdue_state(due_date=date(2026, 12, 31), today=TODAY, grace_days=7)
        assert state == ON_TIME
        elapsed = elapsed_days(raised_at=date(2026, 1, 1), today=TODAY)
        assert lead_time_state(elapsed=elapsed, lead_time_days=21) == BEYOND_LEAD_TIME

    def test_it_covers_the_63_lines_with_no_due_date(self) -> None:
        """The gap this was asked for: no EKET row, so no overdue verdict ever.

        Anchored on the PO date rather than the dispatch, so it fires even on
        the 788 lines that were never dispatched -- which is what makes it a
        real answer rather than the same blind spot renamed.
        """
        assert overdue_state(due_date=None, today=TODAY, grace_days=7) == NO_DUE_DATE
        elapsed = elapsed_days(raised_at=date(2026, 6, 1), today=TODAY)
        assert lead_time_state(elapsed=elapsed, lead_time_days=21) == BEYOND_LEAD_TIME

    def test_days_over_is_signed(self) -> None:
        """Positive once past, negative while inside -- it counts up as things
        get worse, the opposite sign convention from daysRemainingInRepair."""
        assert days_over_lead_time(elapsed=30, lead_time_days=21) == 9
        assert days_over_lead_time(elapsed=14, lead_time_days=21) == -7

    def test_unknown_elapsed_is_not_a_breach(self) -> None:
        assert lead_time_state(elapsed=None, lead_time_days=21) == NO_LEAD_TIME


class TestTheLeadTimeReachesTheLine:
    """The wiring, without a database.

    The rules above are pure functions; this is the assurance that a PLIFZ
    value actually arrives on a RepairLine rather than being computed correctly
    somewhere nothing reads.
    """

    def _line(self, *, lead_time, **row_overrides):
        from app.initiatives.i8.register import _build_line

        return _build_line(
            _row(**row_overrides),
            schedule=None,
            dispatch=None,
            header=None,
            vendor_names={},
            lead_time=lead_time,
            today=TODAY,
            cfg=I8Settings(_env_file=None),
        )

    def test_a_breach_reaches_the_line(self) -> None:
        line = self._line(lead_time=21)
        assert line.lead_time_days == 21
        assert line.lead_time_status == "BEYOND_LEAD_TIME"
        assert line.is_beyond_lead_time is True
        assert line.days_over_lead_time == days_between(date(2026, 6, 1), TODAY) - 21

    def test_no_marc_row_is_not_a_breach(self) -> None:
        """Every Gamsberg repair line, today: the July MARC extract has no rows
        for plant 1500 at all."""
        line = self._line(lead_time=None, werks="1500")
        assert line.lead_time_days is None
        assert line.lead_time_status == "NO_LEAD_TIME"
        assert line.days_over_lead_time is None

    def test_a_recent_line_is_within_its_lead_time(self) -> None:
        line = self._line(lead_time=21, erdat=date(2026, 9, 1))
        assert line.days_elapsed == 10
        assert line.lead_time_status == "WITHIN_LEAD_TIME"
        assert line.aging_bucket == "0-15"

    def test_days_open_keeps_ageing_while_the_unit_is_out(self) -> None:
        line = self._line(lead_time=21)
        assert line.days_open == days_between(date(2026, 6, 1), TODAY)
        assert line.days_open == line.days_elapsed
        assert line.aging_bucket == "60+"

    def test_a_zero_plifz_is_coerced_away_at_the_boundary(self) -> None:
        """So no rule downstream has to remember that 0 means "unmaintained"."""
        from app.initiatives.i8.register import _lead_time_days

        assert _lead_time_days(None) is None
        assert _lead_time_days(Decimal(0)) is None
        assert _lead_time_days(Decimal("21")) == 21

    def test_the_po_date_comes_from_aedat(self) -> None:
        """BEDAT is not exposed by CPI, so reading it is a cutover bug with a
        date on it. Initiative 07 anchors on AEDAT for the same reason."""
        from app.initiatives.i8.register import _build_line

        line = _build_line(
            _row(erdat=None),
            schedule=None,
            dispatch=None,
            header={"bsart": "ZREP", "lifnr": "V1", "aedat": date(2026, 5, 20)},
            vendor_names={},
            lead_time=None,
            today=TODAY,
            cfg=I8Settings(_env_file=None),
        )
        assert line.po_date == date(2026, 5, 20)
        assert line.raised_at == date(2026, 5, 20)


class TestNothingIsHardCoded:
    """The repair-PO convention is PENDING SAP TEAM CONFIRMATION.

    If they come back and say the item category is 'L' and not '3', that must
    be one line in .env -- not a code change, a review and a redeploy. These
    tests are what keep that true.
    """

    def test_the_convention_is_configuration(self) -> None:
        cfg = I8Settings(_env_file=None, repair_item_category="L", repair_doc_type="ZFIX")
        assert cfg.repair_item_category == "L"
        assert cfg.repair_doc_type == "ZFIX"

    def test_the_predicate_follows_the_configuration(self) -> None:
        from app.initiatives.i8.register import is_repair_line

        cfg = I8Settings(_env_file=None, repair_item_category="L")
        assert is_repair_line({"pstyp": "L"}, cfg) is True
        assert is_repair_line({"pstyp": "3"}, cfg) is False

    def test_no_repair_convention_literal_outside_config(self) -> None:
        """No bare '3', 'ZREP' or 'X' in the modules that read them.

        Deliberately scoped to the modules that apply the rule. config.py holds
        the defaults, and a docstring may quote the value as evidence -- what
        must not exist is a comparison against a literal in the logic.
        """
        import ast
        import pathlib

        checked = 0
        for name in ("register.py", "universe.py", "service.py", "requisitions.py"):
            path = pathlib.Path("app/initiatives/i8") / name
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Compare):
                    continue
                literals = [
                    n.value
                    for n in [node.left, *node.comparators]
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                ]
                assert "ZREP" not in literals, f"{name}: 'ZREP' compared literally"
                assert "3" not in literals, f"{name}: item category compared literally"
                assert "X" not in literals, f"{name}: delivery-completed flag compared literally"
                assert "AN" not in literals, f"{name}: RFQ document type compared literally"
            checked += 1
        assert checked == 4

    def test_grace_period_is_configuration(self) -> None:
        assert I8Settings(_env_file=None, overdue_grace_days=0).overdue_grace_days == 0
        assert I8Settings(_env_file=None, overdue_grace_days=30).overdue_grace_days == 30

    def test_aging_band_boundaries_are_configuration(self) -> None:
        """FRS open item 5: aging bands, pending VZI's real-data calibration."""
        cfg = I8Settings(_env_file=None, aging_band_boundaries="10,20,30")
        assert cfg.aging_band_boundaries_list == (10, 20, 30)
        assert aging_bucket(25, cfg.aging_band_boundaries_list) == "21-30"

    def test_confidence_threshold_is_configuration(self) -> None:
        """FRS open item 5: coding-candidate confidence threshold, same status."""
        from app.initiatives.i8.coding_candidates import meets_confidence_threshold

        cfg = I8Settings(_env_file=None, coding_candidate_confidence_threshold="high")
        assert cfg.coding_candidate_confidence_threshold == "high"
        assert meets_confidence_threshold("medium", "high") is False
        assert meets_confidence_threshold("high", "high") is True
        # Unscreened never meets even the lowest bar -- it is not a real
        # judgement, however permissively the threshold is set.
        assert meets_confidence_threshold("", "low") is False

    def test_reference_date_can_be_pinned(self) -> None:
        """So a demo is explainable and a test is not a time bomb."""
        assert I8Settings(_env_file=None, reference_date="").reference_date_value is None
        assert I8Settings(
            _env_file=None, reference_date="2026-07-31"
        ).reference_date_value == date(2026, 7, 31)

    def test_a_malformed_reference_date_fails_loudly(self) -> None:
        """At startup, rather than producing wrong aging on every row."""
        with pytest.raises(ValueError):
            I8Settings(_env_file=None, reference_date="31-07-2026").reference_date_value

    def test_the_attestation_cutover_is_configuration(self) -> None:
        """The date attestation starts applying -- still unsupplied by VZI.

        Blank is the shipped default and means "no cutover", which leaves the
        exception behaving exactly as it did before the label existed. Turning
        it on must be one .env line.
        """
        cfg = I8Settings(_env_file=None)
        assert cfg.attestation_cutover_date_value is None
        cfg = I8Settings(_env_file=None, attestation_cutover_date="2026-10-01")
        assert cfg.attestation_cutover_date_value == date(2026, 10, 1)

    def test_a_malformed_cutover_fails_loudly(self) -> None:
        """A wrong cutover silently forgives real misses on one side of it."""
        with pytest.raises(ValueError):
            I8Settings(
                _env_file=None, attestation_cutover_date="01-10-2026"
            ).attestation_cutover_date_value

    def test_plant_names_are_never_invented(self) -> None:
        """Only 1300 and 1500 are documented anywhere in this repository."""
        cfg = I8Settings(_env_file=None)
        # Derived from app/shared/plant_scope.py, so this also guards against
        # I08 drifting back to its own shorter spelling of Black Mountain.
        assert cfg.plant_name_map == {"1300": "Black Mountain Mining", "1500": "Gamsberg"}
        assert "3000" not in cfg.plant_name_map
