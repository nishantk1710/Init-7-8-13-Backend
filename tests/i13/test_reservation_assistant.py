"""I13 FR-2 -- the cross-check the assistant serves before capturing a plan.

Composition over built parts, so the tests are about what the assembly says
rather than about arithmetic -- WATCH's numbers have their own tests. What
matters here is that absences read as absences and that the sentence does not
say something the platform cannot do.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.initiatives.i13.act.domain import CrossPlantStockInfo
from app.initiatives.i13.models import AgingBand
from app.initiatives.i13.reservation_assistant import build
from tests.assistant.conftest import watch_metric

TODAY = date(2026, 7, 31)


def _assessment(metric=None, cross_plant=(), requested=Decimal("5")):
    return build(
        material="1000000123",
        plant="1300",
        metric=metric or watch_metric(),
        cross_plant_stock=cross_plant,
        requested_quantity=requested,
    )


class TestWhatItSays:
    def test_it_leads_with_what_you_have(self) -> None:
        assessment = _assessment(watch_metric(stock_on_hand=Decimal("4")))
        assert assessment.headline.startswith("You have 4 at plant 1300")

    def test_open_orders_are_stated_because_they_stop_a_second_order(self) -> None:
        assessment = _assessment(watch_metric(open_po_quantity=Decimal("3")))
        assert "3 already on order" in assessment.headline

    def test_no_open_orders_is_simply_not_mentioned(self) -> None:
        assessment = _assessment(watch_metric(open_po_quantity=Decimal("0")))
        assert "on order" not in assessment.headline

    def test_cover_is_rounded_for_reading(self) -> None:
        assessment = _assessment(
            watch_metric(months_of_cover=Decimal("5.29498153846153846"))
        )
        assert "about 5.29 months of cover" in assessment.headline


class TestDormantParts:
    def test_a_non_moving_part_is_called_out(self) -> None:
        """The case FR-2 exists for: a part untouched for 400 days, about to be
        bought again."""
        assessment = _assessment(
            watch_metric(aging_band=AgingBand.NON_MOVING, days_since_last_movement=400)
        )
        assert "has not moved in 400 days (non-moving)" in assessment.headline

    def test_a_fast_moving_part_is_not_editorialised(self) -> None:
        assessment = _assessment(watch_metric(aging_band=AgingBand.FAST))
        assert "has not moved" not in assessment.headline

    def test_the_band_comes_from_w3_5_rather_than_a_local_threshold(self) -> None:
        """So the assistant and the aging report never disagree about a part."""
        assessment = _assessment(
            watch_metric(aging_band=AgingBand.SLOW, days_since_last_movement=500)
        )
        assert assessment.is_dormant is True


class TestCrossPlantStock:
    def test_stock_elsewhere_is_reported(self) -> None:
        assessment = _assessment(
            cross_plant=[CrossPlantStockInfo("1000000123", "1500", Decimal("6"))]
        )
        assert "Other plants hold 6 at 1500" in assessment.headline

    def test_plants_holding_nothing_are_dropped(self) -> None:
        """"Other plants hold 0 at 1300, 0 at 1500" says there is stock
        elsewhere and then says there is not."""
        assessment = _assessment(
            cross_plant=[
                CrossPlantStockInfo("1000000123", "1300", Decimal("0")),
                CrossPlantStockInfo("1000000123", "1500", Decimal("0")),
            ]
        )
        assert assessment.cross_plant_stock == ()
        assert "Other plants" not in assessment.headline

    def test_a_mix_keeps_only_the_plants_with_stock(self) -> None:
        assessment = _assessment(
            cross_plant=[
                CrossPlantStockInfo("1000000123", "1300", Decimal("0")),
                CrossPlantStockInfo("1000000123", "1500", Decimal("6")),
            ]
        )
        assert [i.plant for i in assessment.cross_plant_stock] == ["1500"]

    def test_it_never_proposes_a_transfer(self) -> None:
        """Informational only. The platform does not write to SAP at all."""
        assessment = _assessment(
            cross_plant=[CrossPlantStockInfo("1000000123", "1500", Decimal("6"))]
        )
        assert any("does not create transfers" in note for note in assessment.caveats)


class TestAbsencesAreAnswers:
    def test_unknown_stock_is_not_zero(self) -> None:
        assessment = _assessment(watch_metric(stock_on_hand=None))
        assert "No stock record exists" in assessment.headline
        assert any("not that stock is zero" in note for note in assessment.caveats)

    def test_unknown_cover_is_unlimited_not_zero(self) -> None:
        """A material nothing consumes has infinite cover. Rendering it as zero
        would tell somebody to buy more of a part nothing ever uses."""
        assessment = _assessment(
            watch_metric(months_of_cover=None, months_of_cover_reason="NO_CONSUMPTION")
        )
        assert assessment.cover_is_unknown is True
        assert any("effectively" in note and "unlimited" in note for note in assessment.caveats)

    def test_the_cover_reason_is_carried_so_the_ui_can_explain_a_blank(self) -> None:
        assessment = _assessment(
            watch_metric(months_of_cover=None, months_of_cover_reason="NO_CONSUMPTION")
        )
        assert "NO_CONSUMPTION" in assessment.headline


class TestExistingExceptions:
    def test_an_open_grni_exception_is_surfaced(self) -> None:
        """Stock received and never issued, on the part they are about to order
        more of."""
        assessment = _assessment(
            watch_metric(gr_not_issued_flag=True, gr_not_issued_days_since_gr=45)
        )
        assert any("45 days" in note and "not been issued" in note for note in assessment.caveats)


class TestTheStoredRecord:
    def test_it_is_json_serialisable(self) -> None:
        import json

        record = _assessment().as_record(TODAY)
        assert json.loads(json.dumps(record))["flow"] == "i13"

    def test_quantities_are_strings_and_keep_full_precision(self) -> None:
        """Display rounds; the RECORD does not. Rounding evidence loses
        information that an append-only table cannot recover."""
        assessment = _assessment(
            watch_metric(months_of_cover=Decimal("5.29498153846153846"))
        )
        assert assessment.as_record(TODAY)["monthsOfCover"] == "5.29498153846153846"

    def test_unknown_stock_is_null_in_the_record(self) -> None:
        record = _assessment(watch_metric(stock_on_hand=None)).as_record(TODAY)
        assert record["stockOnHand"] is None
