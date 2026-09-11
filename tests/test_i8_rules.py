"""W5.2 -- aging, overdue and the status derivations, as pure rules.

No database. Every case here is a decision the register makes about one line,
and each one is stated in the task plan as something that must be got right:
reversals, partial receipts, missing due dates, the grace period, and the exact
aging bands the frontend already renders.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.initiatives.i8.aging import (
    AGING_BUCKETS,
    NO_DUE_DATE,
    ON_TIME,
    OVERDUE,
    RECEIVED,
    aging_bucket,
    days_between,
    days_remaining,
    overdue_state,
)
from app.initiatives.i8.config import I8Settings
from app.initiatives.i8.register import (
    AT_VENDOR,
    AWAITING_RECEIPT,
    CLOSED,
    FULLY_RECEIVED,
    NOT_YET_SHIPPED,
    PARTIALLY_RECEIVED,
    PO_ISSUED,
    RECEIVED_STATUS,
    receipt_status,
    repair_status,
)

TODAY = date(2026, 9, 11)


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


# --- overdue --------------------------------------------------------------


class TestOverdueState:
    def test_received_wins_over_everything(self) -> None:
        assert (
            overdue_state(
                received_at=date(2026, 5, 1),
                due_date=date(2025, 1, 1),
                today=TODAY,
                grace_days=7,
            )
            == RECEIVED
        )

    def test_missing_due_date_is_its_own_state(self) -> None:
        """63 lines. They are NOT "not overdue" -- they are the lines nobody
        agreed a date for, and they need to be chaseable."""
        assert (
            overdue_state(
                received_at=None, due_date=None, today=TODAY, grace_days=7
            )
            == NO_DUE_DATE
        )

    def test_grace_period_is_applied(self) -> None:
        due = date(2026, 9, 1)
        # 10 days past due, 7 days grace -> overdue.
        assert (
            overdue_state(received_at=None, due_date=due, today=TODAY, grace_days=7)
            == OVERDUE
        )
        # Same line, 30 days grace -> not yet.
        assert (
            overdue_state(received_at=None, due_date=due, today=TODAY, grace_days=30)
            == ON_TIME
        )

    def test_the_grace_boundary_is_exclusive(self) -> None:
        """Due + grace is the last good day, not the first bad one."""
        due = date(2026, 9, 4)  # + 7 days grace == 2026-09-11 == TODAY
        assert (
            overdue_state(received_at=None, due_date=due, today=TODAY, grace_days=7)
            == ON_TIME
        )
        assert (
            overdue_state(
                received_at=None, due_date=due, today=date(2026, 9, 12), grace_days=7
            )
            == OVERDUE
        )

    def test_zero_grace_is_honoured(self) -> None:
        assert (
            overdue_state(
                received_at=None,
                due_date=date(2026, 9, 10),
                today=TODAY,
                grace_days=0,
            )
            == OVERDUE
        )

    def test_future_due_date_is_on_time(self) -> None:
        assert (
            overdue_state(
                received_at=None,
                due_date=date(2028, 4, 30),
                today=TODAY,
                grace_days=7,
            )
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
    def test_po_raised_but_nothing_moved(self) -> None:
        assert (
            repair_status(
                received_qty=Decimal(0),
                ordered_qty=Decimal(1),
                dispatched_at=None,
                delivery_completed=False,
            )
            == PO_ISSUED
        )

    def test_dispatched_and_not_back(self) -> None:
        assert (
            repair_status(
                received_qty=Decimal(0),
                ordered_qty=Decimal(1),
                dispatched_at=date(2026, 1, 1),
                delivery_completed=False,
            )
            == AT_VENDOR
        )

    def test_fully_received(self) -> None:
        assert (
            repair_status(
                received_qty=Decimal(1),
                ordered_qty=Decimal(1),
                dispatched_at=date(2026, 1, 1),
                delivery_completed=False,
            )
            == RECEIVED_STATUS
        )

    def test_received_and_signed_off_is_closed(self) -> None:
        assert (
            repair_status(
                received_qty=Decimal(1),
                ordered_qty=Decimal(1),
                dispatched_at=date(2026, 1, 1),
                delivery_completed=True,
            )
            == CLOSED
        )

    def test_delivery_complete_alone_does_not_close_a_line(self) -> None:
        """The measurement behind this: 1,025 of the 1,225 repair lines carry
        ELIKZ='X' and only 436 have any goods receipt. Treating the flag as
        closure would report 589 units back in stock that never came back."""
        assert (
            repair_status(
                received_qty=Decimal(0),
                ordered_qty=Decimal(1),
                dispatched_at=None,
                delivery_completed=True,
            )
            == PO_ISSUED
        )

    def test_a_fully_reversed_receipt_is_not_a_receipt(self) -> None:
        """A 101 followed by its 102 nets to zero. The line is still open."""
        assert (
            repair_status(
                received_qty=Decimal(0),
                ordered_qty=Decimal(1),
                dispatched_at=date(2026, 1, 1),
                delivery_completed=True,
            )
            == AT_VENDOR
        )


class TestReceiptStatus:
    def test_not_yet_shipped(self) -> None:
        assert (
            receipt_status(
                received_qty=Decimal(0), ordered_qty=Decimal(5), dispatched_at=None
            )
            == NOT_YET_SHIPPED
        )

    def test_awaiting_receipt_once_dispatched(self) -> None:
        assert (
            receipt_status(
                received_qty=Decimal(0),
                ordered_qty=Decimal(5),
                dispatched_at=date(2026, 1, 1),
            )
            == AWAITING_RECEIPT
        )

    def test_partial_receipt_is_not_received(self) -> None:
        """No line in the July extract is partially received, so this rule is
        only exercised here. It is implemented because a partial receipt is
        ordinary in a live client, and finding out at cutover that the register
        rounds it up to "Received" would mean reporting units back that are
        still at the vendor."""
        assert (
            receipt_status(
                received_qty=Decimal(2),
                ordered_qty=Decimal(5),
                dispatched_at=date(2026, 1, 1),
            )
            == PARTIALLY_RECEIVED
        )

    def test_fully_received(self) -> None:
        assert (
            receipt_status(
                received_qty=Decimal(5),
                ordered_qty=Decimal(5),
                dispatched_at=date(2026, 1, 1),
            )
            == FULLY_RECEIVED
        )

    def test_over_receipt_counts_as_received(self) -> None:
        assert (
            receipt_status(
                received_qty=Decimal(6),
                ordered_qty=Decimal(5),
                dispatched_at=date(2026, 1, 1),
            )
            == FULLY_RECEIVED
        )


# --- configuration discipline --------------------------------------------


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
        """No bare '3' or 'ZREP' in the modules that read them.

        Deliberately scoped to the modules that apply the rule. config.py holds
        the defaults, and a docstring may quote the value as evidence -- what
        must not exist is a comparison against a literal in the logic.
        """
        import ast
        import pathlib

        checked = 0
        for name in ("register.py", "universe.py", "service.py", "vendors.py"):
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
            checked += 1
        assert checked == 4

    def test_grace_period_is_configuration(self) -> None:
        assert I8Settings(_env_file=None, overdue_grace_days=0).overdue_grace_days == 0
        assert I8Settings(_env_file=None, overdue_grace_days=30).overdue_grace_days == 30

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

    def test_plant_names_are_never_invented(self) -> None:
        """Only 1300 and 1500 are documented anywhere in this repository."""
        cfg = I8Settings(_env_file=None)
        assert cfg.plant_name_map == {"1300": "Black Mountain", "1500": "Gamsberg"}
        assert "3000" not in cfg.plant_name_map
