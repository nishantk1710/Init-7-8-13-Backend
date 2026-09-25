"""W7.4 / I13 FR-3 -- how many should they actually reserve?

The arithmetic is not the model's job
--------------------------------------
``model_registry.py`` took this position before WS7 started. Its entry for
``i13_quantity_suggestion`` reads: *"Explains an arithmetic result. The
arithmetic is not the model's job."* This module is that arithmetic. A language
model may later be asked to phrase the answer (see ``app.assistant.narrative``),
and it is never asked to compute it -- every input here is a field already held
on ``WatchMetric``, so there is nothing to infer and a generated number would be
a fabrication wearing a decimal point.

What it computes
----------------
One idea: **do not buy past the cover ceiling.**

    target        = average monthly consumption x cover ceiling (months)
    already have  = stock on hand + quantity already on open purchase orders
    headroom      = target - already have

Open PO quantity is netted off deliberately. A requester who cannot see that
three are already on order is exactly the person who orders a fourth, and that
is the over-ordering FR-3 exists to prevent.

The suggestion is the headroom, capped at what they asked for. Capping matters:
a requester who wants one part for a breakdown must never be told to order six
because the shelf happens to be empty. This is an assistant, not a planner --
Initiative 07 owns reorder points, and this must not quietly become a second,
disagreeing implementation of one.

When it refuses to answer
--------------------------
**Below ``min_history_consumptions`` movements in the look-back window, no
suggestion is made at all.** An average built from one issue is not an average,
and a confident number derived from it is worse than no number: the requester
cannot tell the difference, and over-trusting a fabricated average is how a
platform loses the argument the first time somebody checks it.

``suggested_quantity`` is then ``None`` -- which is **not zero**. "We suggest
nothing" and "we suggest none" are opposite instructions, and the record keeps
them apart (see ``QuantitySuggestionRecord.suggested_quantity``).

Rounding, and which way
------------------------
Down, to whole units. The ceiling is a *ceiling*: rounding up would suggest a
quantity that breaches the thing the suggestion exists to respect. The cost is
that a headroom of 0.8 suggests 0, which reads as "you already have enough" --
and on a cover basis that is exactly what it means.

Whole units regardless of the unit of measure, and that is a known
simplification: a material issued in metres or litres can legitimately be
reserved fractionally. No UoM-aware rounding rule exists in this codebase to
reuse and inventing one here would put a second opinion about units in a module
that has no business holding one.

It advises, it never blocks
----------------------------
The platform cannot write to SAP, so a requester can close the assistant and
reserve whatever they like. Keeping more than was suggested is recorded as an
override and asks for a justification -- which is precisely what ACT's
``QUANTITY_OVERRIDE`` exception and its ``QuantityDecisionRecord`` were built
for. That record's ``suggested_quantity`` field has been ``None`` for every
caller because this engine did not exist; it does now.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import Enum

from app.core.config import Settings, get_settings
from app.initiatives.i13.models import WatchMetric
from app.shared.numbers import plain

ZERO = Decimal("0")


class NoSuggestionReason(str, Enum):
    """Why no number was produced. Never collapsed into a zero."""

    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    """Fewer consumption events in the look-back window than
    ``min_history_consumptions``. The commonest case, and the honest one."""

    NO_CONSUMPTION_RATE = "NO_CONSUMPTION_RATE"
    """History exists but the average monthly consumption is zero or unknown,
    so there is no rate to divide a cover target by."""


@dataclass(frozen=True)
class QuantitySuggestionConfig:
    """FR-3's three configured values.

    The FRS names all three and gives numbers for none of them, so these are
    OUR defaults until VZI confirms them (open question 10). They travel on
    every suggestion for exactly that reason -- a number whose basis cannot be
    recovered is one nobody can argue with.
    """

    cover_ceiling_months: Decimal
    lookback_months: int
    min_history_consumptions: int

    def __post_init__(self) -> None:
        if self.cover_ceiling_months <= 0:
            raise ValueError(
                f"i13_quantity_cover_ceiling_months must be positive, got "
                f"{self.cover_ceiling_months}"
            )
        if self.lookback_months <= 0:
            raise ValueError(
                f"i13_quantity_lookback_months must be positive, got {self.lookback_months}"
            )
        if self.min_history_consumptions < 0:
            raise ValueError(
                f"i13_quantity_min_history_consumptions cannot be negative, got "
                f"{self.min_history_consumptions}"
            )


def build_quantity_config(settings: Settings | None = None) -> QuantitySuggestionConfig:
    settings = settings or get_settings()
    return QuantitySuggestionConfig(
        # str() first: Decimal(float) carries the float's binary error into a
        # number people read, and 12.0 should not become 11.999999999999998.
        cover_ceiling_months=Decimal(str(settings.i13_quantity_cover_ceiling_months)),
        lookback_months=settings.i13_quantity_lookback_months,
        min_history_consumptions=settings.i13_quantity_min_history_consumptions,
    )


@dataclass(frozen=True)
class QuantitySuggestion:
    """What we suggest, what it was computed from, and why.

    Carries its whole derivation. This is served to somebody about to spend
    money and is stored as evidence of benefit, and both readers need to be able
    to check the number rather than take it.
    """

    material: str
    plant: str

    requested_quantity: Decimal

    suggested_quantity: Decimal | None
    """``None`` means no suggestion was made -- see :attr:`no_suggestion_reason`.
    It is not zero."""

    no_suggestion_reason: NoSuggestionReason | None

    # --- the inputs, all from WatchMetric --------------------------------
    stock_on_hand: Decimal | None
    open_po_quantity: Decimal
    average_monthly_consumption: Decimal
    months_of_cover: Decimal | None
    consumption_count: int

    # --- the configured basis --------------------------------------------
    config: QuantitySuggestionConfig

    # --- what follows from each choice ------------------------------------
    projected_cover_if_suggested: Decimal | None
    projected_cover_if_requested: Decimal | None

    @property
    def available(self) -> bool:
        """Whether a suggestion could be made at all.

        Mirrors ``QuantityOverrideEvaluation.available`` in the ACT domain,
        where ``False`` means SOURCE_UNAVAILABLE and is never reported as "no
        override".
        """
        return self.suggested_quantity is not None

    @property
    def is_override(self) -> bool:
        """Whether the requester is keeping more than was suggested.

        ``False`` when no suggestion was made. Calling that an override would
        manufacture a compliance finding out of a data gap -- the requester
        cannot override advice that was never given.
        """
        if self.suggested_quantity is None:
            return False
        return self.requested_quantity > self.suggested_quantity

    @property
    def variance(self) -> Decimal | None:
        if self.suggested_quantity is None:
            return None
        return self.requested_quantity - self.suggested_quantity

    @property
    def reason(self) -> str:
        """The sentence of record. Deterministic, and the one that gets stored."""
        ceiling = plain(self.config.cover_ceiling_months)

        if self.no_suggestion_reason is NoSuggestionReason.INSUFFICIENT_HISTORY:
            return (
                f"No quantity is suggested: {self.material} has "
                f"{self.consumption_count} recorded consumption"
                f"{'' if self.consumption_count == 1 else 's'} in the last "
                f"{self.config.lookback_months} months, below the minimum of "
                f"{self.config.min_history_consumptions} this platform will "
                "average over. A rate built from that little history would look "
                "authoritative and not be."
            )

        if self.no_suggestion_reason is NoSuggestionReason.NO_CONSUMPTION_RATE:
            return (
                f"No quantity is suggested: {self.material} has no measurable "
                f"monthly consumption over the last {self.config.lookback_months} "
                "months, so there is no rate to size a cover target against."
            )

        assert self.suggested_quantity is not None  # narrowed by the branches above
        held = (self.stock_on_hand or ZERO) + self.open_po_quantity
        basis = (
            f"{plain(self.average_monthly_consumption)} a month over the last "
            f"{self.config.lookback_months} months, {plain(held)} already held "
            f"or on order, and a {ceiling}-month cover ceiling"
        )

        if self.suggested_quantity == ZERO:
            cover = (
                f" -- that is {plain(self.months_of_cover)} months of cover"
                if self.months_of_cover is not None
                else ""
            )
            return (
                f"No new units are suggested: {plain(held)} are already in stock "
                f"or on order{cover}, at or above the {ceiling}-month ceiling. "
                f"Based on {basis}."
            )

        return (
            f"{plain(self.suggested_quantity)} suggested, based on {basis}."
        )




def _months_of_cover(available: Decimal, rate: Decimal) -> Decimal | None:
    """Cover in months, or ``None`` when there is no rate to divide by.

    Not zero. A material nothing is consumed from has *infinite* cover, not
    none, and reporting zero would invert the meaning at exactly the moment
    somebody is deciding whether to buy more.
    """
    if rate <= ZERO:
        return None
    return (available / rate).quantize(Decimal("0.1"))


def suggest(
    metric: WatchMetric,
    requested_quantity: Decimal,
    config: QuantitySuggestionConfig | None = None,
) -> QuantitySuggestion:
    """The FR-3 suggestion for one material+plant and one requested quantity.

    Pure: every input is already on the ``WatchMetric`` the caller loaded, so
    this computes and never reads. That is what lets the assistant, the
    standalone endpoint and the unit tests share one implementation.
    """
    config = config or build_quantity_config()

    stock_on_hand = metric.stock_on_hand
    open_po_quantity = metric.open_po_quantity or ZERO
    rate = metric.average_monthly_consumption or ZERO
    available = (stock_on_hand or ZERO) + open_po_quantity

    def _refuse(reason: NoSuggestionReason) -> QuantitySuggestion:
        return QuantitySuggestion(
            material=metric.material,
            plant=metric.plant,
            requested_quantity=requested_quantity,
            suggested_quantity=None,
            no_suggestion_reason=reason,
            stock_on_hand=stock_on_hand,
            open_po_quantity=open_po_quantity,
            average_monthly_consumption=rate,
            months_of_cover=metric.months_of_cover,
            consumption_count=metric.consumption_count_12m,
            config=config,
            projected_cover_if_suggested=None,
            projected_cover_if_requested=_months_of_cover(
                available + requested_quantity, rate
            ),
        )

    # The refusals come first, and in this order. Too little history is the
    # honest reason; a zero rate computed FROM too little history would be a
    # misleading one, so the history test must not be reachable past it.
    if metric.consumption_count_12m < config.min_history_consumptions:
        return _refuse(NoSuggestionReason.INSUFFICIENT_HISTORY)

    if rate <= ZERO:
        return _refuse(NoSuggestionReason.NO_CONSUMPTION_RATE)

    target = rate * config.cover_ceiling_months
    headroom = target - available

    if headroom <= ZERO:
        suggested = ZERO
    else:
        # DOWN. The ceiling is a ceiling -- rounding up would suggest a quantity
        # that breaches the limit the suggestion exists to respect.
        suggested = headroom.to_integral_value(rounding=ROUND_FLOOR)
        # Never suggest more than was asked for. This advises a reservation; it
        # is not a reorder proposal, and Initiative 07 owns those.
        suggested = min(suggested, requested_quantity)

    return QuantitySuggestion(
        material=metric.material,
        plant=metric.plant,
        requested_quantity=requested_quantity,
        suggested_quantity=suggested,
        no_suggestion_reason=None,
        stock_on_hand=stock_on_hand,
        open_po_quantity=open_po_quantity,
        average_monthly_consumption=rate,
        months_of_cover=metric.months_of_cover,
        consumption_count=metric.consumption_count_12m,
        config=config,
        projected_cover_if_suggested=_months_of_cover(available + suggested, rate),
        projected_cover_if_requested=_months_of_cover(
            available + requested_quantity, rate
        ),
    )
