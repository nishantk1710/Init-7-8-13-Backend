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
from tests.assistant.conftest import repair_line, repair_request, universe_row

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


class TestRequestsAndStockConditionInTheRecord:
    """The 27-Oct-2026 keys (section 2.4). Added only: an older stored record
    lacks them and is replayed as it was."""

    _OLD_KEYS = {
        "flow", "materialId", "plant", "description", "criticality", "headline",
        "caveats", "repairableUnitExists", "sources", "stockOnHand", "stockIsUnknown",
        "openRepairLines", "quantityUnderRepair", "soonestDueDate", "overdueLines",
        "repairDueDateIsReliable", "newUnitLeadTimeDays", "waitingBeatsBuying",
        "referenceDate", "openRepairs",
    }

    def _record(self, rows=None, lines=None, requests=None):
        import json

        assessment = build(
            material_id="8000005632",
            plant="1300",
            universe_rows=rows if rows is not None else [universe_row()],
            repair_lines=lines if lines is not None else [],
            today=TODAY,
            repair_requests=requests,
            department_name={"10305": "Engineering"}.get,
        )
        record = assessment.as_record(TODAY)
        assert json.loads(json.dumps(record)) == record
        return record

    def test_no_key_was_renamed_or_removed(self) -> None:
        assert self._OLD_KEYS <= set(self._record())

    def test_the_request_keys_and_their_types(self) -> None:
        record = self._record(requests=[repair_request()])
        assert record["repairableUnitExists"] is True
        assert record["sources"] == ["REPAIR_REQUESTED"]
        assert record["openRepairLines"] == 0
        assert record["openRepairs"] == []
        assert record["openRepairRequestLines"] == 1
        assert record["openRepairRequests"] == [
            {
                "purchaseRequisition": "2000029113",
                "item": "10",
                "quantity": "1",
                "requestedAt": "2025-12-04",
                "daysSinceRequested": 239,
                "department": "10305",
                "departmentName": "Engineering",
                "repairOrder": "940000004981",
            }
        ]
        assert "requested, not yet ordered (requested 239 days ago)" in record["headline"]

    def test_switched_off_counts_no_requests(self) -> None:
        record = self._record(requests=None)
        assert record["openRepairRequestLines"] == 0
        assert record["openRepairRequests"] == []
        assert record["repairableUnitExists"] is False

    def test_known_condition_stock_keys(self) -> None:
        row = universe_row(
            stock_on_hand=Decimal("3"),
            usable_stock=Decimal("2"),
            damaged_stock=Decimal("1"),
            stock_by_condition=(("DAMAGED", Decimal("1")), ("NEW", Decimal("2"))),
            stock_condition_source="MCHB",
        )
        record = self._record(rows=[row])
        assert record["stockOnHand"] == "3"
        assert record["usableStock"] == "2"
        assert record["damagedStock"] == "1"
        assert record["stockConditionKnown"] is True
        assert record["stockSource"] == "MCHB"

    def test_damaged_only_is_no_unit_in_the_record(self) -> None:
        row = universe_row(
            stock_on_hand=Decimal("2"),
            usable_stock=Decimal("0"),
            damaged_stock=Decimal("2"),
            stock_by_condition=(("DAMAGED", Decimal("2")),),
            stock_condition_source="MCHB",
        )
        record = self._record(rows=[row])
        assert record["repairableUnitExists"] is False
        assert record["usableStock"] == "0"
        assert record["stockOnHand"] == "2"

    def test_unknown_condition_falls_back_to_the_total(self) -> None:
        record = self._record(rows=[universe_row(stock_on_hand=Decimal("3"))])
        assert record["usableStock"] is None
        assert record["damagedStock"] is None
        assert record["stockConditionKnown"] is False
        assert record["stockSource"] == "MARD"
        assert record["repairableUnitExists"] is True

    def test_no_stock_record_has_no_source(self) -> None:
        record = self._record(rows=[universe_row(stock_on_hand=None)])
        assert record["stockSource"] is None
        assert record["stockIsUnknown"] is True


class TestNeedDateAdvice:
    """The need-date line (section 2.4, 27-Oct-2026). Off by default until SAP
    confirms MRP counts an open repair PO as stock on the way."""

    def _assessment(self, *, lines, enabled=True, requests=None):
        return build(
            material_id="8000005632",
            plant="1300",
            universe_rows=[universe_row()],
            repair_lines=lines,
            today=TODAY,
            repair_requests=requests,
            need_date_advice_enabled=enabled,
        )

    def test_off_by_default_says_nothing(self) -> None:
        assessment = _assessment(lines=[repair_line(due_date=date(2026, 8, 10))])
        assert assessment.need_date_advice(TODAY) is None
        record = assessment.as_record(TODAY)
        assert "needDateAdvice" in record
        assert record["needDateAdvice"] is None

    def test_switched_off_says_nothing_even_with_a_future_date(self) -> None:
        assessment = self._assessment(
            lines=[repair_line(due_date=date(2026, 8, 10))], enabled=False
        )
        assert assessment.as_record(TODAY)["needDateAdvice"] is None

    def test_a_future_due_date_gives_the_sentence(self) -> None:
        assessment = self._assessment(lines=[repair_line(due_date=date(2026, 8, 10))])
        assert assessment.as_record(TODAY)["needDateAdvice"] == (
            "A repaired unit is due back on 2026-08-10. If your job can wait, set "
            "the need date after that, and no new unit is bought."
        )

    def test_a_date_due_today_still_counts(self) -> None:
        assessment = self._assessment(lines=[repair_line(due_date=TODAY)])
        assert "due back on 2026-07-31." in assessment.need_date_advice(TODAY)

    def test_only_past_due_dates_give_nothing(self) -> None:
        """An overdue date is a plan already missed, not a forecast."""
        assessment = self._assessment(lines=[repair_line(due_date=date(2025, 4, 24))])
        assert assessment.need_date_advice(TODAY) is None

    def test_the_soonest_date_not_yet_passed_is_used(self) -> None:
        """Not the verdict's soonest due date, which here is the overdue one."""
        assessment = self._assessment(
            lines=[
                repair_line(item="00010", due_date=date(2025, 4, 24)),
                repair_line(item="00020", due_date=date(2026, 9, 15)),
                repair_line(item="00030", due_date=date(2026, 8, 20)),
            ]
        )
        assert assessment.repair_due_date == date(2025, 4, 24)
        assert "due back on 2026-08-20." in assessment.need_date_advice(TODAY)

    def test_a_repair_with_no_due_date_gives_nothing(self) -> None:
        assessment = self._assessment(lines=[repair_line(due_date=None)])
        assert assessment.need_date_advice(TODAY) is None

    def test_a_requested_repair_alone_gives_nothing(self) -> None:
        """A repair PR has no PO, so no date for its return."""
        assessment = self._assessment(lines=[], requests=[repair_request()])
        assert assessment.verdict.exists is True
        assert assessment.as_record(TODAY)["needDateAdvice"] is None


def test_the_rules_echo_the_need_date_switch(monkeypatch) -> None:
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.api.i8 import router
    from app.initiatives.i8.config import I8Settings
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
    for enabled in (False, True):
        cfg = I8Settings(_env_file=None, need_date_advice_enabled=enabled)
        rules = router.get_snapshot_info(snapshot, cfg).model_dump(by_alias=True)["rules"]
        assert rules["needDateAdviceEnabled"] is enabled


def test_the_need_date_switch_is_off_by_default() -> None:
    from app.initiatives.i8.config import I8Settings

    assert I8Settings(_env_file=None).need_date_advice_enabled is False
