"""W7.4: the reservation-time quantity-suggestion engine (FRS FR-3).

Months-of-cover arithmetic, and nothing else. This module is pure: no
database, no HTTP, no LLM, and no wall-clock read -- ``as_of`` is passed in,
the same discipline ``act/detection.py`` and ``watch.py`` already follow. It
takes the W6.3 figures a caller has already fetched and returns a decision;
fetching them is ``quantity_suggestion_store.py``'s job.

**Every input already exists.** W6.3's ``i13_watch_metric_mart`` computes the
trailing-twelve-month consumption rate, stock on hand, open-PO quantity and
the consumption count this engine needs, so nothing here re-derives a
consumption rate, re-reads MARD or re-nets an open PO -- it reuses W6.3's
figures exactly as ACT reuses its GRNI flag.

The decision, per FR-3:

1. **Gate.** No configured ceiling/minimum history -> NOT_CONFIGURED. No
   consumption rate, or history below the minimum -> INSUFFICIENT_HISTORY.
   The FRS is explicit ("No suggestion is made where there is no consumption
   history"), so the engine declines rather than guessing.
2. **Express the request as cover.** ``(SOH + OPO + requested) / AMC``.
3. **Plan need.** ``AMC x plan window``, net of what is already on hand and
   on order.
4. **Nudge down** where the request exceeds the cover ceiling.
5. **Nudge up** where it falls short of the plan need.

**Where the plan window needs more than the ceiling allows, the ceiling
wins** (CEILING_BELOW_PLAN_NEED), and both figures are carried on the result
so the requester can see the gap and justify it. FR-3 does not rule on this
case; the ceiling is the guard rail against over-ordering, which is the
initiative's whole purpose, so silently exceeding it would defeat the
feature. **This is the code's ruling pending VZI confirmation** -- it is one
branch in ``compute_quantity_suggestion`` below if the answer comes back the
other way.

The LLM has no part in any of the above. The number is deterministic,
reproducible arithmetic that has to survive an audit; the model is used only
to phrase the reason for a human reader, in
``quantity_suggestion_reason.py``, and the deterministic sentence built here
is what persists when no model answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from app.initiatives.i13.config import QuantitySuggestionConfig

# Every quantity and month figure is rounded to this scale before it leaves
# the engine. Matches the Numeric(18, 6) columns the W6.3 mart and the W7.4
# tables both use, so what the engine computes is exactly what persists --
# an unrounded Decimal division carries 28 significant digits and would be
# silently truncated on its way into the database, leaving the stored figure
# and the computed one different.
_SCALE = Decimal("0.000001")

ZERO = Decimal("0")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_SCALE, rounding=ROUND_HALF_UP)


class SuggestionDirection(str, Enum):
    """Which way the engine moved the requested quantity, if at all."""

    UP = "UP"
    DOWN = "DOWN"
    NONE = "NONE"
    NO_SUGGESTION = "NO_SUGGESTION"
    """The engine declined. ``suggested_quantity`` is ``None`` -- never the
    requested quantity echoed back, which a consumer would read as
    agreement."""


class SuggestionReason(str, Enum):
    """Why the engine said what it said. Structured, so W6.6's exception
    evidence and FRS §8 savings attribution never have to parse prose."""

    DISABLED = "DISABLED"
    """The master gate (``I13_QTY_SUGGESTION_ENABLED``) is off."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    """Cover ceiling and/or minimum history are unset -- VZI open items. The
    engine declines rather than defaulting to an invented ceiling."""

    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    EXCEEDS_COVER_CEILING = "EXCEEDS_COVER_CEILING"
    BELOW_PLAN_NEED = "BELOW_PLAN_NEED"
    CEILING_BELOW_PLAN_NEED = "CEILING_BELOW_PLAN_NEED"
    """The stated plan window needs more than the ceiling permits. The
    ceiling wins; see the module docstring."""

    ALIGNED = "ALIGNED"
    """The request already sits between the plan need and the ceiling."""


@dataclass(frozen=True)
class QuantitySuggestionInputs:
    """One quantity decision to evaluate.

    ``average_monthly_consumption``, ``stock_on_hand``, ``open_po_quantity``
    and ``consumption_count_12m`` come straight from W6.3's mart row and are
    never recomputed here.

    ``plan_window_months`` is the months of use the requester stated. It is
    an explicit parameter rather than a lookup on purpose: that is the only
    value W7.4 needs from W7.3's ``ConsumptionPlan``, and keeping it a
    parameter makes this engine buildable and testable standalone, ahead of
    the chat flow, and wires to W7.3 later without a change here.
    """

    material: str
    plant: str
    requested_quantity: Decimal
    plan_window_months: Decimal
    average_monthly_consumption: Decimal
    stock_on_hand: Decimal
    open_po_quantity: Decimal
    consumption_count_12m: int

    def __post_init__(self) -> None:
        if self.requested_quantity < 0:
            raise ValueError(f"requested_quantity cannot be negative, got {self.requested_quantity}")
        if self.plan_window_months < 0:
            raise ValueError(f"plan_window_months cannot be negative, got {self.plan_window_months}")


@dataclass(frozen=True)
class QuantitySuggestion:
    """What the engine decided, and everything needed to defend it later.

    Carries the *config values used* alongside the inputs, not just a
    reference to them: FRS §8 counts a benefit only where the requester
    accepted the suggestion, and proving that months later means proving what
    was suggested and on what basis. A ceiling that has since been retuned
    must not silently rewrite the reasoning behind a suggestion made under
    the old one.
    """

    material: str
    plant: str

    requested_quantity: Decimal
    suggested_quantity: Decimal | None
    direction: SuggestionDirection
    reason_code: SuggestionReason
    reason_text: str
    """Deterministic, built here. ``quantity_suggestion_reason.py`` may
    replace it with model-phrased prose; the figures never change."""

    # --- Input snapshot (W6.3) ---
    average_monthly_consumption: Decimal
    stock_on_hand: Decimal
    open_po_quantity: Decimal
    consumption_count_12m: int

    # --- Basis ---
    plan_window_months: Decimal
    resulting_cover_months: Decimal | None
    """Months of cover the request would leave, ``None`` when AMC is zero --
    the division is undefined, and 0 would read as "no cover"."""
    plan_need_quantity: Decimal | None
    net_need_quantity: Decimal | None
    ceiling_quantity: Decimal | None

    # --- Config snapshot ---
    cover_ceiling_months: Decimal | None
    minimum_history_count: int | None
    lookback_months: int

    calculated_at: datetime

    @property
    def has_suggestion(self) -> bool:
        return self.suggested_quantity is not None

    @property
    def variance(self) -> Decimal | None:
        """Requested minus suggested. ``None`` when no suggestion was made --
        never 0, which would read as "the requester was already right"."""
        if self.suggested_quantity is None:
            return None
        return self.requested_quantity - self.suggested_quantity


def _declined(
    inputs: QuantitySuggestionInputs,
    config: QuantitySuggestionConfig,
    *,
    reason_code: SuggestionReason,
    reason_text: str,
    as_of: datetime,
    resulting_cover_months: Decimal | None = None,
) -> QuantitySuggestion:
    """A no-suggestion result. The inputs and config snapshot are still
    recorded: "the engine declined, and here is what it was looking at" is
    the auditable answer, and it is what drives the coverage question in
    FRS §10."""
    return QuantitySuggestion(
        material=inputs.material,
        plant=inputs.plant,
        requested_quantity=_q(inputs.requested_quantity),
        suggested_quantity=None,
        direction=SuggestionDirection.NO_SUGGESTION,
        reason_code=reason_code,
        reason_text=reason_text,
        average_monthly_consumption=_q(inputs.average_monthly_consumption),
        stock_on_hand=_q(inputs.stock_on_hand),
        open_po_quantity=_q(inputs.open_po_quantity),
        consumption_count_12m=inputs.consumption_count_12m,
        plan_window_months=_q(inputs.plan_window_months),
        resulting_cover_months=resulting_cover_months,
        plan_need_quantity=None,
        net_need_quantity=None,
        ceiling_quantity=None,
        cover_ceiling_months=config.cover_ceiling_months,
        minimum_history_count=config.minimum_history_count,
        lookback_months=config.lookback_months,
        calculated_at=as_of,
    )


def _fmt(value: Decimal) -> str:
    """Trim a fixed-point figure for the deterministic sentence -- 12 reads
    better than 12.000000 to the person deciding on a purchase."""
    trimmed = value.normalize()
    # normalize() turns 100 into 1E+2; expand it back.
    return f"{trimmed:f}"


def compute_quantity_suggestion(
    inputs: QuantitySuggestionInputs,
    config: QuantitySuggestionConfig,
    *,
    as_of: datetime,
) -> QuantitySuggestion:
    """The FR-3 decision for one (material, plant) request.

    Pure: the same inputs always produce the same result, which is what lets
    a suggestion be recomputed and checked against what was persisted months
    later. ``as_of`` is stamped onto the result, never read from the clock.
    """
    if not config.enabled:
        return _declined(
            inputs,
            config,
            reason_code=SuggestionReason.DISABLED,
            reason_text=(
                "No quantity suggestion: the suggestion engine is switched off in this environment "
                "(I13_QTY_SUGGESTION_ENABLED)."
            ),
            as_of=as_of,
        )

    if not config.thresholds_configured:
        missing = []
        if config.cover_ceiling_months is None:
            missing.append("cover ceiling")
        if config.minimum_history_count is None:
            missing.append("minimum history")
        return _declined(
            inputs,
            config,
            reason_code=SuggestionReason.NOT_CONFIGURED,
            reason_text=(
                f"No quantity suggestion: {' and '.join(missing)} not configured. "
                "These are business values, not defaults this engine may invent."
            ),
            as_of=as_of,
        )

    amc = inputs.average_monthly_consumption
    on_hand_and_ordered = inputs.stock_on_hand + inputs.open_po_quantity

    # Minimum-history gate. Two independent ways to fail it: no consumption
    # rate at all, and a rate built on too few events to trust.
    if amc <= ZERO:
        return _declined(
            inputs,
            config,
            reason_code=SuggestionReason.INSUFFICIENT_HISTORY,
            reason_text=(
                f"No quantity suggestion for {inputs.material} at {inputs.plant}: no consumption recorded in the "
                f"last {config.lookback_months} months, so there is no basis for a months-of-cover figure."
            ),
            as_of=as_of,
        )

    # Both are non-None past the thresholds_configured gate above; bound to
    # locals so that stays true for the rest of this function.
    minimum_history = config.minimum_history_count or 0
    cover_ceiling = config.cover_ceiling_months or ZERO

    if inputs.consumption_count_12m < minimum_history:
        return _declined(
            inputs,
            config,
            reason_code=SuggestionReason.INSUFFICIENT_HISTORY,
            reason_text=(
                f"No quantity suggestion for {inputs.material} at {inputs.plant}: "
                f"{inputs.consumption_count_12m} consumption(s) in the last {config.lookback_months} months is "
                f"below the minimum of {minimum_history} this engine needs before it will advise."
            ),
            as_of=as_of,
            resulting_cover_months=_q((on_hand_and_ordered + inputs.requested_quantity) / amc),
        )

    resulting_cover = _q((on_hand_and_ordered + inputs.requested_quantity) / amc)

    plan_need = _q(amc * inputs.plan_window_months)
    net_need = _q(max(ZERO, plan_need - on_hand_and_ordered))
    ceiling_qty = _q(max(ZERO, (cover_ceiling * amc) - on_hand_and_ordered))

    requested = _q(inputs.requested_quantity)
    basis = (
        f"{_fmt(_q(amc))}/month over {config.lookback_months} months, with {_fmt(_q(inputs.stock_on_hand))} on hand "
        f"and {_fmt(_q(inputs.open_po_quantity))} on open order"
    )

    if net_need > ceiling_qty:
        # The conflict case. The ceiling wins -- see the module docstring.
        direction = (
            SuggestionDirection.DOWN
            if requested > ceiling_qty
            else SuggestionDirection.UP
            if requested < ceiling_qty
            else SuggestionDirection.NONE
        )
        reason_code = SuggestionReason.CEILING_BELOW_PLAN_NEED
        reason_text = (
            f"Suggested {_fmt(ceiling_qty)} against {_fmt(requested)} requested. The stated "
            f"{_fmt(_q(inputs.plan_window_months))}-month plan needs {_fmt(net_need)}, which is more than the "
            f"{_fmt(cover_ceiling)}-month cover ceiling allows ({_fmt(ceiling_qty)}); the ceiling "
            f"holds, so the shortfall of {_fmt(net_need - ceiling_qty)} needs a justification. Based on {basis}."
        )
        suggested = ceiling_qty
    elif requested > ceiling_qty:
        direction = SuggestionDirection.DOWN
        reason_code = SuggestionReason.EXCEEDS_COVER_CEILING
        reason_text = (
            f"Suggested {_fmt(ceiling_qty)} against {_fmt(requested)} requested. The requested quantity would "
            f"leave {_fmt(resulting_cover)} months of cover, above the {_fmt(cover_ceiling)}-month "
            f"ceiling. Based on {basis}."
        )
        suggested = ceiling_qty
    elif requested < net_need:
        direction = SuggestionDirection.UP
        reason_code = SuggestionReason.BELOW_PLAN_NEED
        reason_text = (
            f"Suggested {_fmt(net_need)} against {_fmt(requested)} requested. The stated "
            f"{_fmt(_q(inputs.plan_window_months))}-month plan needs {_fmt(plan_need)}, of which "
            f"{_fmt(net_need)} is not already covered. Based on {basis}."
        )
        suggested = net_need
    else:
        direction = SuggestionDirection.NONE
        reason_code = SuggestionReason.ALIGNED
        reason_text = (
            f"No change suggested to {_fmt(requested)}. It covers the stated "
            f"{_fmt(_q(inputs.plan_window_months))}-month plan and stays within the "
            f"{_fmt(cover_ceiling)}-month cover ceiling, leaving {_fmt(resulting_cover)} months of "
            f"cover. Based on {basis}."
        )
        suggested = requested

    return QuantitySuggestion(
        material=inputs.material,
        plant=inputs.plant,
        requested_quantity=requested,
        suggested_quantity=suggested,
        direction=direction,
        reason_code=reason_code,
        reason_text=reason_text,
        average_monthly_consumption=_q(amc),
        stock_on_hand=_q(inputs.stock_on_hand),
        open_po_quantity=_q(inputs.open_po_quantity),
        consumption_count_12m=inputs.consumption_count_12m,
        plan_window_months=_q(inputs.plan_window_months),
        resulting_cover_months=resulting_cover,
        plan_need_quantity=plan_need,
        net_need_quantity=net_need,
        ceiling_quantity=ceiling_qty,
        cover_ceiling_months=config.cover_ceiling_months,
        minimum_history_count=config.minimum_history_count,
        lookback_months=config.lookback_months,
        calculated_at=as_of,
    )
