"""W7.4 unit tests: the quantity-suggestion engine (FR-3).

Pure-function tests -- no database, no provider, no clock. Boundary-first,
the same discipline W3.5's aging tests follow (365/366/730/731): every
threshold is exercised at one below, exactly on, and one above, because
"exceeds the ceiling" and "reaches the ceiling" are one comparison apart and
that comparison decides a purchase quantity.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.initiatives.i13.config import QuantitySuggestionConfig
from app.initiatives.i13.quantity_suggestion import (
    QuantitySuggestionInputs,
    SuggestionDirection,
    SuggestionReason,
    compute_quantity_suggestion,
)

AS_OF = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def _config(
    *,
    enabled: bool = True,
    ceiling: str | None = "6",
    minimum_history: int | None = 4,
    lookback: int = 12,
) -> QuantitySuggestionConfig:
    return QuantitySuggestionConfig(
        enabled=enabled,
        cover_ceiling_months=Decimal(ceiling) if ceiling is not None else None,
        minimum_history_count=minimum_history,
        lookback_months=lookback,
    )


def _inputs(
    *,
    requested: str = "10",
    window: str = "3",
    amc: str = "10",
    soh: str = "0",
    open_po: str = "0",
    history: int = 6,
) -> QuantitySuggestionInputs:
    return QuantitySuggestionInputs(
        material="MAT-1",
        plant="1300",
        requested_quantity=Decimal(requested),
        plan_window_months=Decimal(window),
        average_monthly_consumption=Decimal(amc),
        stock_on_hand=Decimal(soh),
        open_po_quantity=Decimal(open_po),
        consumption_count_12m=history,
    )


def _compute(inputs=None, config=None):
    return compute_quantity_suggestion(inputs or _inputs(), config or _config(), as_of=AS_OF)


# --- The gate -------------------------------------------------------------


def test_no_consumption_declines_rather_than_guessing() -> None:
    """FRS §3.1: "No suggestion is made where there is no consumption
    history." Zero AMC has no months-of-cover basis at all."""
    result = _compute(_inputs(amc="0", history=0))

    assert result.direction is SuggestionDirection.NO_SUGGESTION
    assert result.reason_code is SuggestionReason.INSUFFICIENT_HISTORY
    assert result.suggested_quantity is None
    assert result.variance is None


def test_suggested_quantity_is_never_the_request_echoed_back_when_declining() -> None:
    """A consumer reading suggested == requested would take it as agreement.
    Declining must be unmistakable."""
    result = _compute(_inputs(requested="25", amc="0"))
    assert result.suggested_quantity is None


@pytest.mark.parametrize(
    "history,expected_reason",
    [
        (3, SuggestionReason.INSUFFICIENT_HISTORY),  # MIN_HIST - 1
        (4, SuggestionReason.ALIGNED),  # exactly MIN_HIST -- speaks
        (5, SuggestionReason.ALIGNED),  # MIN_HIST + 1
    ],
)
def test_minimum_history_boundary(history: int, expected_reason: SuggestionReason) -> None:
    """The minimum is inclusive: exactly MIN_HIST consumptions is enough."""
    result = _compute(_inputs(history=history, requested="30", window="3", amc="10"))
    assert result.reason_code is expected_reason


def test_unconfigured_ceiling_declines_and_does_not_invent_one() -> None:
    result = _compute(config=_config(ceiling=None))

    assert result.direction is SuggestionDirection.NO_SUGGESTION
    assert result.reason_code is SuggestionReason.NOT_CONFIGURED
    assert result.cover_ceiling_months is None
    assert "cover ceiling" in result.reason_text


def test_unconfigured_minimum_history_declines() -> None:
    result = _compute(config=_config(minimum_history=None))
    assert result.reason_code is SuggestionReason.NOT_CONFIGURED
    assert "minimum history" in result.reason_text


def test_disabled_is_distinguishable_from_unconfigured() -> None:
    """Switched off here and "the business has not given us the numbers" are
    different states and must not collapse into one reason code."""
    result = _compute(config=_config(enabled=False))
    assert result.reason_code is SuggestionReason.DISABLED


def test_declined_result_still_records_inputs_and_config() -> None:
    """"The engine declined, and here is what it was looking at" is the
    auditable answer, and it is what the FRS §10 coverage question needs."""
    result = _compute(_inputs(amc="0", soh="7", open_po="2", history=1))

    assert result.average_monthly_consumption == Decimal("0")
    assert result.stock_on_hand == Decimal("7")
    assert result.open_po_quantity == Decimal("2")
    assert result.consumption_count_12m == 1
    assert result.lookback_months == 12


# --- Ceiling nudge --------------------------------------------------------
#
# AMC 10, ceiling 6 months, nothing on hand or on order -> ceiling_qty = 60.


@pytest.mark.parametrize(
    "requested,expected_direction,expected_reason",
    [
        ("59", SuggestionDirection.NONE, SuggestionReason.ALIGNED),  # ceiling - 1
        ("60", SuggestionDirection.NONE, SuggestionReason.ALIGNED),  # exactly the ceiling
        ("61", SuggestionDirection.DOWN, SuggestionReason.EXCEEDS_COVER_CEILING),  # ceiling + 1
    ],
)
def test_ceiling_boundary(requested: str, expected_direction, expected_reason) -> None:
    """The ceiling is inclusive: a request that lands exactly on it is not
    nudged. "Exceeds" means strictly greater."""
    result = _compute(_inputs(requested=requested, window="3", amc="10"))

    assert result.direction is expected_direction
    assert result.reason_code is expected_reason
    assert result.ceiling_quantity == Decimal("60")


def test_ceiling_nudge_suggests_the_ceiling_quantity() -> None:
    result = _compute(_inputs(requested="100", window="3", amc="10"))

    assert result.direction is SuggestionDirection.DOWN
    assert result.suggested_quantity == Decimal("60")
    assert result.variance == Decimal("40")


def test_stock_already_above_the_ceiling_gives_a_zero_ceiling_quantity() -> None:
    """Never a negative suggestion: 80 on hand against a 60-unit ceiling
    means buy nothing, not buy -20."""
    result = _compute(_inputs(requested="10", window="1", amc="10", soh="80"))

    assert result.ceiling_quantity == Decimal("0")
    assert result.suggested_quantity == Decimal("0")
    assert result.direction is SuggestionDirection.DOWN
    assert result.reason_code is SuggestionReason.EXCEEDS_COVER_CEILING


def test_open_pos_count_against_the_ceiling() -> None:
    """FR-3 nets the request against stock on hand AND open POs -- 20 already
    on order lowers what is left to buy under the ceiling."""
    result = _compute(_inputs(requested="60", window="3", amc="10", soh="10", open_po="20"))

    assert result.ceiling_quantity == Decimal("30")  # 6 * 10 - (10 + 20)
    assert result.direction is SuggestionDirection.DOWN
    assert result.suggested_quantity == Decimal("30")


# --- Plan-need nudge ------------------------------------------------------
#
# AMC 10, window 3 -> plan_need = 30. Ceiling 6 months keeps it under 60.


@pytest.mark.parametrize(
    "requested,expected_direction,expected_reason",
    [
        ("29", SuggestionDirection.UP, SuggestionReason.BELOW_PLAN_NEED),  # net_need - 1
        ("30", SuggestionDirection.NONE, SuggestionReason.ALIGNED),  # exactly net_need
        ("31", SuggestionDirection.NONE, SuggestionReason.ALIGNED),  # net_need + 1
    ],
)
def test_plan_need_boundary(requested: str, expected_direction, expected_reason) -> None:
    """Meeting the plan need exactly is enough -- "falls short" means
    strictly less."""
    result = _compute(_inputs(requested=requested, window="3", amc="10"))

    assert result.direction is expected_direction
    assert result.reason_code is expected_reason
    assert result.net_need_quantity == Decimal("30")


def test_shortfall_nudge_suggests_the_net_need() -> None:
    result = _compute(_inputs(requested="5", window="3", amc="10"))

    assert result.direction is SuggestionDirection.UP
    assert result.suggested_quantity == Decimal("30")
    assert result.variance == Decimal("-25")


def test_stock_already_covering_the_plan_gives_a_zero_net_need() -> None:
    """35 on hand against a 30-unit plan need: no shortfall, so no upward
    nudge, and never a negative need."""
    result = _compute(_inputs(requested="0", window="3", amc="10", soh="35"))

    assert result.plan_need_quantity == Decimal("30")
    assert result.net_need_quantity == Decimal("0")
    assert result.direction is SuggestionDirection.NONE
    assert result.reason_code is SuggestionReason.ALIGNED


# --- The conflict case ----------------------------------------------------


def test_ceiling_wins_when_the_plan_window_needs_more_than_it_allows() -> None:
    """A 9-month plan against a 6-month ceiling. The ceiling is the guard
    rail against over-ordering -- the initiative's whole purpose -- so it
    holds, and both figures are surfaced so the requester can justify the
    gap. Pending VZI confirmation (plan §9 Q4)."""
    result = _compute(_inputs(requested="90", window="9", amc="10"))

    assert result.reason_code is SuggestionReason.CEILING_BELOW_PLAN_NEED
    assert result.suggested_quantity == Decimal("60")  # the ceiling, not the 90 the plan wants
    assert result.direction is SuggestionDirection.DOWN

    # Both numbers present, so the gap is visible rather than implied.
    assert result.net_need_quantity == Decimal("90")
    assert result.ceiling_quantity == Decimal("60")
    assert "90" in result.reason_text and "60" in result.reason_text


def test_conflict_case_nudges_up_when_the_request_is_below_even_the_ceiling() -> None:
    """The conflict is between the plan and the ceiling, not with the
    requester -- a request of 10 against a 60-unit ceiling still moves up."""
    result = _compute(_inputs(requested="10", window="9", amc="10"))

    assert result.reason_code is SuggestionReason.CEILING_BELOW_PLAN_NEED
    assert result.direction is SuggestionDirection.UP
    assert result.suggested_quantity == Decimal("60")


# --- Months of cover ------------------------------------------------------


def test_resulting_cover_expresses_the_request_in_months() -> None:
    """FR-3's first sentence: (SOH + OPO + requested) / AMC."""
    result = _compute(_inputs(requested="20", window="3", amc="10", soh="5", open_po="5"))
    assert result.resulting_cover_months == Decimal("3.000000")


def test_resulting_cover_is_none_rather_than_zero_when_there_is_no_consumption() -> None:
    """The division is undefined at AMC 0; 0 would read as "no cover"."""
    result = _compute(_inputs(amc="0"))
    assert result.resulting_cover_months is None


# --- Discipline -----------------------------------------------------------


def test_every_figure_is_decimal_never_float() -> None:
    result = _compute(_inputs(requested="10.5", window="2.5", amc="3.3", soh="1.1", open_po="0.7"))

    for value in (
        result.requested_quantity,
        result.suggested_quantity,
        result.average_monthly_consumption,
        result.stock_on_hand,
        result.open_po_quantity,
        result.plan_window_months,
        result.resulting_cover_months,
        result.plan_need_quantity,
        result.net_need_quantity,
        result.ceiling_quantity,
    ):
        assert isinstance(value, Decimal)


def test_repeating_decimals_are_quantised_to_the_persisted_scale() -> None:
    """An unrounded Decimal division carries 28 significant digits and would
    be silently truncated by the Numeric(18, 6) column -- leaving the stored
    figure different from the computed one."""
    result = _compute(_inputs(requested="10", window="1", amc="3", history=6))

    assert result.resulting_cover_months == Decimal("3.333333")
    assert result.resulting_cover_months.as_tuple().exponent == -6


def test_as_of_is_stamped_and_never_read_from_the_clock() -> None:
    result = _compute()
    assert result.calculated_at == AS_OF


def test_the_config_used_is_snapshotted_onto_the_result() -> None:
    """Retuning the ceiling later must not rewrite the reasoning behind a
    suggestion already made -- FRS §8 has to prove what was suggested and on
    what basis."""
    result = _compute(config=_config(ceiling="8", minimum_history=2, lookback=18))

    assert result.cover_ceiling_months == Decimal("8")
    assert result.minimum_history_count == 2
    assert result.lookback_months == 18


def test_identical_inputs_always_produce_an_identical_result() -> None:
    """Reproducibility is the reason the number is arithmetic and not the
    model's -- a suggestion must be recomputable and checkable against what
    was persisted months later."""
    inputs = _inputs(requested="77", window="4", amc="12.5", soh="3", open_po="9")
    assert _compute(inputs) == _compute(inputs)


def test_negative_requested_quantity_is_refused() -> None:
    with pytest.raises(ValueError, match="requested_quantity"):
        _inputs(requested="-1")


def test_negative_plan_window_is_refused() -> None:
    with pytest.raises(ValueError, match="plan_window_months"):
        _inputs(window="-1")


def test_zero_plan_window_means_no_upward_nudge() -> None:
    """A requester who states no plan window gets the ceiling guard rail and
    nothing else -- the engine has no stated need to nudge them up to."""
    result = _compute(_inputs(requested="1", window="0", amc="10"))

    assert result.net_need_quantity == Decimal("0")
    assert result.direction is SuggestionDirection.NONE
    assert result.reason_code is SuggestionReason.ALIGNED


# --- Config validation ----------------------------------------------------


def test_a_non_positive_ceiling_is_refused_at_config_time() -> None:
    with pytest.raises(ValueError, match="i13_qty_cover_ceiling_months"):
        _config(ceiling="0")


def test_a_negative_minimum_history_is_refused_at_config_time() -> None:
    with pytest.raises(ValueError, match="i13_qty_minimum_history_count"):
        _config(minimum_history=-1)


def test_thresholds_configured_is_independent_of_the_master_gate() -> None:
    assert _config(enabled=False).thresholds_configured is True
    assert _config(ceiling=None).thresholds_configured is False
    assert _config(minimum_history=None).thresholds_configured is False
