"""I08 FR-5 -- the assessment the assistant serves before asking anything.

Assembly over built parts, so most of what matters here is the one number this
module adds: **wait for the repair, or buy a new one?**

That comparison is the whole reason the assessment exists, and it is the one
place a plausible implementation gets the answer backwards.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.initiatives.i8.reservation_assistant import build
from tests.assistant.conftest import repair_line, universe_row

TODAY = date(2026, 7, 31)


def _assessment(rows=None, lines=None, today=TODAY):
    return build(
        material_id="8000005632",
        plant="1300",
        universe_rows=rows if rows is not None else [universe_row()],
        repair_lines=lines if lines is not None else [repair_line()],
        today=today,
    )


class TestWaitOrBuy:
    def test_a_repair_arriving_sooner_than_a_new_order_favours_waiting(self) -> None:
        assessment = _assessment(
            rows=[universe_row(planned_delivery_days=30)],
            lines=[repair_line(due_date=date(2026, 8, 10))],  # 10 days away
        )
        assert assessment.waiting_beats_buying(TODAY) is True
        assert "waiting looks faster" in assessment.headline(TODAY)

    def test_a_repair_further_out_than_a_new_order_favours_buying(self) -> None:
        assessment = _assessment(
            rows=[universe_row(planned_delivery_days=30)],
            lines=[repair_line(due_date=date(2026, 12, 1))],
        )
        assert assessment.waiting_beats_buying(TODAY) is False
        assert "buying may genuinely be the faster route" in assessment.headline(TODAY)


class TestTheOverdueTrap:
    """The bug this module was corrected for, pinned so it cannot return.

    An overdue repair has a due date in the PAST. A naive comparison finds that
    "sooner" than any positive lead time and recommends waiting -- for a unit
    that is already late with no new forecast. 695 of the 788 open repair lines
    in this extract are overdue, so the naive answer would have been wrong on
    88% of them, in the direction that costs money and keeps a machine down.
    """

    def test_an_overdue_repair_cannot_be_compared(self) -> None:
        assessment = _assessment(
            rows=[universe_row(planned_delivery_days=30)],
            lines=[repair_line(due_date=date(2025, 4, 24))],  # 463 days late
        )
        assert assessment.waiting_beats_buying(TODAY) is None

    def test_it_never_claims_waiting_is_faster_for_an_overdue_repair(self) -> None:
        assessment = _assessment(
            rows=[universe_row(planned_delivery_days=30)],
            lines=[repair_line(due_date=date(2025, 4, 24))],
        )
        assert "waiting looks faster" not in assessment.headline(TODAY)

    def test_it_says_the_date_is_no_longer_a_forecast(self) -> None:
        assessment = _assessment(
            rows=[universe_row(planned_delivery_days=30)],
            lines=[repair_line(due_date=date(2025, 4, 24))],
        )
        headline = assessment.headline(TODAY)
        assert "no longer a forecast" in headline
        assert "chase it" in headline
        assert "463 days ago" in headline

    def test_the_due_date_is_flagged_unreliable_when_every_repair_is_late(self) -> None:
        assessment = _assessment(lines=[repair_line(due_date=date(2025, 4, 24))])
        assert assessment.repair_due_date_is_reliable is False

    def test_a_due_date_still_in_the_future_is_reliable(self) -> None:
        assessment = _assessment(lines=[repair_line(due_date=date(2026, 8, 10))])
        assert assessment.repair_due_date_is_reliable is True

    def test_reliability_is_unknown_when_there_is_no_date_at_all(self) -> None:
        assessment = _assessment(lines=[repair_line(due_date=None)])
        assert assessment.repair_due_date_is_reliable is None


class TestMissingLeadTime:
    def test_no_marc_lead_time_means_no_comparison(self) -> None:
        """MARC covers roughly two thirds of the universe and holds no Gamsberg
        rows at all, so this is common rather than exceptional."""
        assessment = _assessment(
            rows=[universe_row(planned_delivery_days=None)],
            lines=[repair_line(due_date=date(2026, 8, 10))],
        )
        assert assessment.new_unit_lead_time_days is None
        assert assessment.waiting_beats_buying(TODAY) is None
        assert "cannot say how a new order would compare" in assessment.headline(TODAY)

    def test_a_missing_lead_time_is_never_defaulted_to_zero(self) -> None:
        """A defaulted zero would make waiting look faster every time."""
        assessment = _assessment(rows=[universe_row(planned_delivery_days=None)])
        assert assessment.new_unit_lead_time_days is None


class TestNothingAvailable:
    def test_no_repairable_unit_still_reports_the_buying_lead_time(self) -> None:
        assessment = _assessment(
            rows=[universe_row(stock_on_hand=Decimal("0"), planned_delivery_days=30)],
            lines=[],
        )
        assert assessment.verdict.exists is False
        assert "takes about 30 days" in assessment.headline(TODAY)


class TestFieldsAreTakenFromRowsThatKnow:
    def test_criticality_comes_from_the_first_row_that_has_one(self) -> None:
        """A universe row exists per material+plant but its sources cover
        different subsets, so the first row is often the one that knows least --
        and None must mean "no source knows", not "we picked badly"."""
        assessment = build(
            material_id="8000005632",
            plant="1300",
            universe_rows=[
                universe_row(criticality=None, description=None, planned_delivery_days=None),
                universe_row(criticality="CRITICAL", description="Pump", planned_delivery_days=45),
            ],
            repair_lines=[],
            today=TODAY,
        )
        assert assessment.criticality == "CRITICAL"
        assert assessment.description == "Pump"
        assert assessment.new_unit_lead_time_days == 45

    def test_criticality_stays_none_when_no_row_knows(self) -> None:
        """Never defaulted -- an unknown criticality shown as NORMAL is worse
        than one shown as unknown."""
        assessment = _assessment(rows=[universe_row(criticality=None)])
        assert assessment.criticality is None


class TestTheStoredRecord:
    def test_it_is_json_serialisable(self) -> None:
        import json

        record = _assessment().as_record(TODAY)
        assert json.loads(json.dumps(record))["flow"] == "i08"

    def test_quantities_are_strings_never_floats(self) -> None:
        """A quantity that round-trips through a float comes back as
        2.9999999999999996, and this record is evidence."""
        record = _assessment(rows=[universe_row(stock_on_hand=Decimal("2.5"))]).as_record(TODAY)
        assert record["stockOnHand"] == "2.5"
        assert isinstance(record["stockOnHand"], str)

    def test_unknown_stock_is_null_not_zero_in_the_record(self) -> None:
        record = _assessment(rows=[universe_row(stock_on_hand=None)]).as_record(TODAY)
        assert record["stockOnHand"] is None
        assert record["stockIsUnknown"] is True

    def test_the_record_carries_the_comparison_it_could_not_make(self) -> None:
        record = _assessment(lines=[repair_line(due_date=date(2025, 4, 24))]).as_record(TODAY)
        assert record["waitingBeatsBuying"] is None
        assert record["repairDueDateIsReliable"] is False
