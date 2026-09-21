"""I13 FR-3 -- the quantity suggestion.

Pure arithmetic over a ``WatchMetric``, so these tests build metrics directly
and need no database, no CSV and no repositories.

The behaviour worth defending here is mostly about **refusing to answer**. The
easy half is dividing two numbers; the half that decides whether anybody trusts
this is knowing when not to.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.initiatives.i13.models import AcquiredVsPlanStatus, AgingBand, WatchMetric
from app.initiatives.i13.quantity import (
    NoSuggestionReason,
    QuantitySuggestionConfig,
    build_quantity_config,
    suggest,
)
from app.shared.material_scope import MaterialScope

CONFIG = QuantitySuggestionConfig(
    cover_ceiling_months=Decimal("12"),
    lookback_months=12,
    min_history_consumptions=3,
)


def _metric(
    *,
    stock_on_hand: Decimal | None = Decimal("0"),
    open_po_quantity: Decimal = Decimal("0"),
    average_monthly_consumption: Decimal = Decimal("1"),
    months_of_cover: Decimal | None = Decimal("0"),
    consumption_count_12m: int = 12,
) -> WatchMetric:
    """A WatchMetric with only the fields FR-3 reads set to something meaningful."""
    return WatchMetric(
        material="1000000123",
        plant="1300",
        material_scope=MaterialScope.OAR,
        stock_on_hand=stock_on_hand,
        open_po_quantity=open_po_quantity,
        average_monthly_consumption=average_monthly_consumption,
        months_of_cover=months_of_cover,
        projected_months_of_cover=None,
        months_of_cover_reason=None,
        last_movement_date=date(2026, 7, 1),
        days_since_last_movement=30,
        last_issue_date=date(2026, 7, 1),
        days_since_last_issue=30,
        consumption_count_12m=consumption_count_12m,
        consumed_qty_12m=Decimal("12"),
        inventory_turns=None,
        inventory_turns_reason=None,
        aging_band=AgingBand.FAST,
        gr_not_issued_flag=False,
        gr_not_issued_days_since_gr=None,
        gr_not_issued_relevant_gr_date=None,
        gr_not_issued_threshold_days=30,
        gr_not_issued_received_quantity=Decimal("0"),
        gr_not_issued_issued_quantity=Decimal("0"),
        gr_not_issued_outstanding_quantity=Decimal("0"),
        acquired_vs_plan_status=AcquiredVsPlanStatus.NO_PLAN,
        planned_quantity=None,
        received_quantity=Decimal("0"),
        issued_quantity=Decimal("0"),
        acquired_vs_plan_variance_quantity=None,
        acquired_vs_plan_variance_percentage=None,
        calculated_at=datetime.now(timezone.utc),
    )


class TestTheArithmetic:
    def test_an_empty_shelf_gets_the_full_cover_target_capped_at_the_request(self) -> None:
        """1/month x 12 months = 12 target, nothing held -- but they asked for 2."""
        result = suggest(_metric(), Decimal("2"), CONFIG)
        assert result.suggested_quantity == Decimal("2")

    def test_the_suggestion_never_exceeds_what_was_asked_for(self) -> None:
        """This advises a reservation. It is not a reorder proposal -- I07 owns
        those, and a second disagreeing implementation of one would be worse
        than none."""
        result = suggest(_metric(average_monthly_consumption=Decimal("10")), Decimal("1"), CONFIG)
        assert result.suggested_quantity == Decimal("1")

    def test_stock_already_held_reduces_the_suggestion(self) -> None:
        """1/month, 12-month ceiling, 10 on the shelf -> headroom of 2."""
        result = suggest(_metric(stock_on_hand=Decimal("10")), Decimal("5"), CONFIG)
        assert result.suggested_quantity == Decimal("2")

    def test_open_purchase_orders_are_netted_off(self) -> None:
        """The requester who cannot see that three are already on order is
        exactly the person who orders a fourth."""
        result = suggest(
            _metric(stock_on_hand=Decimal("4"), open_po_quantity=Decimal("6")),
            Decimal("5"),
            CONFIG,
        )
        assert result.suggested_quantity == Decimal("2")

    def test_full_cover_suggests_nothing_new(self) -> None:
        result = suggest(_metric(stock_on_hand=Decimal("12")), Decimal("3"), CONFIG)
        assert result.suggested_quantity == Decimal("0")
        assert result.available is True

    def test_cover_beyond_the_ceiling_still_suggests_zero_not_a_negative(self) -> None:
        result = suggest(_metric(stock_on_hand=Decimal("40")), Decimal("3"), CONFIG)
        assert result.suggested_quantity == Decimal("0")


class TestRounding:
    def test_it_rounds_down_so_the_ceiling_is_respected(self) -> None:
        """0.5/month x 12 = 6 target, 3 held -> headroom 3.0. Use a rate that
        produces a fraction: 0.7 x 12 = 8.4, minus 5 held = 3.4 -> 3."""
        result = suggest(
            _metric(average_monthly_consumption=Decimal("0.7"), stock_on_hand=Decimal("5")),
            Decimal("10"),
            CONFIG,
        )
        assert result.suggested_quantity == Decimal("3")

    def test_a_fractional_headroom_below_one_suggests_nothing(self) -> None:
        """Reads as "you already have enough", which on a cover basis is what
        0.8 of a unit of headroom means."""
        result = suggest(
            _metric(average_monthly_consumption=Decimal("0.4"), stock_on_hand=Decimal("4")),
            Decimal("5"),
            CONFIG,
        )
        assert result.suggested_quantity == Decimal("0")


class TestRefusingToAnswer:
    """The half that decides whether anybody trusts this."""

    def test_too_little_history_produces_no_suggestion(self) -> None:
        result = suggest(_metric(consumption_count_12m=2), Decimal("5"), CONFIG)
        assert result.suggested_quantity is None
        assert result.no_suggestion_reason is NoSuggestionReason.INSUFFICIENT_HISTORY
        assert result.available is False

    def test_no_suggestion_is_not_a_suggestion_of_zero(self) -> None:
        """The distinction the record is built around: "we suggest nothing" and
        "we suggest none" are opposite instructions."""
        refused = suggest(_metric(consumption_count_12m=1), Decimal("5"), CONFIG)
        zero = suggest(_metric(stock_on_hand=Decimal("99")), Decimal("5"), CONFIG)
        assert refused.suggested_quantity is None
        assert zero.suggested_quantity == Decimal("0")
        assert refused.available is False and zero.available is True

    def test_exactly_the_minimum_history_is_enough(self) -> None:
        result = suggest(_metric(consumption_count_12m=3), Decimal("5"), CONFIG)
        assert result.suggested_quantity is not None

    def test_a_zero_consumption_rate_produces_no_suggestion(self) -> None:
        result = suggest(
            _metric(average_monthly_consumption=Decimal("0"), consumption_count_12m=12),
            Decimal("5"),
            CONFIG,
        )
        assert result.no_suggestion_reason is NoSuggestionReason.NO_CONSUMPTION_RATE

    def test_insufficient_history_is_reported_ahead_of_a_zero_rate(self) -> None:
        """Both are true when there is no history at all, and the honest reason
        is the one about history -- a zero rate computed from no data would
        describe the symptom rather than the cause."""
        result = suggest(
            _metric(average_monthly_consumption=Decimal("0"), consumption_count_12m=0),
            Decimal("5"),
            CONFIG,
        )
        assert result.no_suggestion_reason is NoSuggestionReason.INSUFFICIENT_HISTORY


class TestOverride:
    def test_keeping_more_than_suggested_is_an_override(self) -> None:
        result = suggest(_metric(stock_on_hand=Decimal("10")), Decimal("5"), CONFIG)
        assert result.suggested_quantity == Decimal("2")
        assert result.is_override is True
        assert result.variance == Decimal("3")

    def test_accepting_the_suggestion_is_not_an_override(self) -> None:
        result = suggest(_metric(), Decimal("2"), CONFIG)
        assert result.is_override is False
        assert result.variance == Decimal("0")

    def test_no_suggestion_can_never_be_overridden(self) -> None:
        """A requester cannot override advice that was never given -- calling
        that an override manufactures a compliance finding out of a data gap."""
        result = suggest(_metric(consumption_count_12m=0), Decimal("99"), CONFIG)
        assert result.is_override is False
        assert result.variance is None


class TestProjectedCover:
    def test_it_shows_what_each_choice_leads_to(self) -> None:
        result = suggest(_metric(stock_on_hand=Decimal("10")), Decimal("5"), CONFIG)
        assert result.projected_cover_if_suggested == Decimal("12.0")
        assert result.projected_cover_if_requested == Decimal("15.0")

    def test_cover_with_no_consumption_rate_is_unknown_not_zero(self) -> None:
        """A material nothing is consumed from has infinite cover, not none.
        Reporting zero would invert the meaning exactly when somebody is
        deciding whether to buy more."""
        result = suggest(
            _metric(average_monthly_consumption=Decimal("0"), consumption_count_12m=12),
            Decimal("5"),
            CONFIG,
        )
        assert result.projected_cover_if_requested is None


class TestTheReasonIsReadable:
    def test_it_states_the_basis_including_the_configured_values(self) -> None:
        """These are our defaults until VZI confirms them, so a number whose
        basis cannot be recovered is one nobody can argue with."""
        result = suggest(_metric(stock_on_hand=Decimal("10")), Decimal("5"), CONFIG)
        assert "12-month cover ceiling" in result.reason
        assert "last 12 months" in result.reason

    def test_refusal_explains_itself(self) -> None:
        result = suggest(_metric(consumption_count_12m=1), Decimal("5"), CONFIG)
        assert "below the minimum of 3" in result.reason

    def test_quantities_read_as_people_say_them(self) -> None:
        result = suggest(_metric(stock_on_hand=Decimal("10.000")), Decimal("5"), CONFIG)
        assert "2 suggested" in result.reason
        assert "2.000" not in result.reason


class TestConfig:
    def test_the_defaults_come_from_settings(self) -> None:
        config = build_quantity_config()
        assert config.cover_ceiling_months == Decimal("12")
        assert config.lookback_months == 12
        assert config.min_history_consumptions == 3

    def test_the_ceiling_avoids_binary_float_error(self) -> None:
        """Decimal(12.0) direct from a float carries binary error into a number
        people read. Going through str() does not."""
        assert str(build_quantity_config().cover_ceiling_months) == "12.0"

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"cover_ceiling_months": Decimal("0")},
            {"cover_ceiling_months": Decimal("-1")},
            {"lookback_months": 0},
            {"min_history_consumptions": -1},
        ],
    )
    def test_nonsense_configuration_is_refused_at_construction(self, kwargs) -> None:
        base = dict(
            cover_ceiling_months=Decimal("12"),
            lookback_months=12,
            min_history_consumptions=3,
        )
        base.update(kwargs)
        with pytest.raises(ValueError):
            QuantitySuggestionConfig(**base)
