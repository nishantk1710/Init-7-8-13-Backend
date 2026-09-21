"""Formatting a number for a sentence a person reads.

Why this is shared rather than local
-------------------------------------
Three modules had written their own copy of this, and all three had the same
bug: they stripped trailing zeros but never rounded, so a computed ratio reached
the screen in full. The assistant told a requester they had

    5.29498153846153846153846154 months of cover

which is not a number anybody can act on, and which makes the rest of the
sentence look untrustworthy by association. One shared function means fixing it
once rather than three times and missing the fourth.

Display only, and the distinction matters
------------------------------------------
Nothing here is ever used to compute, compare or store. The stored records keep
full-precision Decimals as strings -- that is evidence, and rounding evidence
loses information that cannot be recovered from an append-only table. This is
the last step before a value becomes part of an English sentence, and nothing
else.

So ``2.5`` stays ``2.5``, ``3.000`` becomes ``3``, and
``5.29498153846...`` becomes ``5.29``.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

#: Two decimal places is enough for every number the assistant states: months of
#: cover, quantities, and consumption rates. More precision would be noise a
#: requester cannot act on; less would round a genuine half-unit away.
_DISPLAY_EXPONENT = Decimal("0.01")


def plain(value: Decimal | None, *, unknown: str = "unknown") -> str:
    """A number as a person would say it out loud.

    ``None`` renders as ``unknown`` rather than as ``0`` or an empty string.
    That default is load-bearing throughout this platform: "no source told us"
    and "the answer is zero" are different facts, and only one of them should
    change somebody's mind about buying a part.
    """
    if value is None:
        return unknown

    rounded = value.quantize(_DISPLAY_EXPONENT, rounding=ROUND_HALF_UP)

    # Drop the decimal part when there is nothing after it: "3 in stock", not
    # "3.00 in stock".
    normalised = rounded.normalize()

    # normalize() turns 30 into 3E+1. Put it back into plain notation, which is
    # the only form that belongs in a sentence.
    if normalised.as_tuple().exponent > 0:
        normalised = normalised.quantize(Decimal(1))

    return str(normalised)
