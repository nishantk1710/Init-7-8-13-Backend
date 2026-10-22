"""Which MBEW valuation row is a material's replacement unit price at a plant.

Pure: no database, no settings. :func:`choose_unit_price` takes the MBEW rows of
ONE material at ONE plant and says which price to use, or why there is none.
Every case is a unit test (``tests/test_valuation_price.py``); the caller only
fetches rows and records what this returns.

**One plant, one price.** MBEW holds a row per valuation area -- here the area
is the plant (BWKEY) -- and per valuation type (BWTAR) within it. A plant's
price is read from that plant's own rows only. It is never borrowed from another
plant, and never taken as "the highest of everything we found": the highest
price across areas picked ``7,689.26`` for sodium cyanide at plants where the
material is valued at ``14.58`` and ``15.45``, because two dormant plants held a
per-tonne price in a per-KG field. That was a 500x error in every money figure
built on it.

**Which row, at the plant.**

* *Not split-valuated* (no row carries a BWTAR): the blank-BWTAR row is the
  price.
* *Split-valuated* (any row carries a BWTAR -- every 80-series material does):
  the price is the ``NEW`` row; when there is no ``NEW`` row, the ``NEW-INS``
  row. ``NEW`` wins whenever both exist -- by precedence, not by price. The
  blank-BWTAR row is ignored: for a split-valuated material it is an aggregate
  across conditions and can carry the refurbished value (I08 measured material
  43253306 at NEW R38,122 against R34,840.62 on the blank row). Refurbished,
  damaged and scrap rows are not what buying a replacement costs, so they are
  never read.

The rule is decided by whether the row *exists*, not by whether its price is
usable. A ``NEW`` row holding a placeholder does not hand the decision to
``NEW-INS``: that would be a fallback in all but name, and the gap is the honest
answer. It is reported, not papered over.

**No price is a named outcome, never a zero.** See :class:`PriceMissing`.
Several rows of the read type with *different* prices have no defensible winner
(highest would be a guess), so they are a gap too; identical duplicates are not.

This is the same row choice I08 makes for its coding screen (``NEW``-first,
blank row only without split valuation, placeholder floor). It lives here, in
shared code, because I07 must not import I08's internals; I08's own copy is
untouched.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

NEW_TYPE_PRECEDENCE: tuple[str, ...] = ("NEW", "NEW-INS")
"""Valuation types that are a replacement price, most preferred first. Fixed
order: when both exist the first wins, whatever the prices."""

DEFAULT_TOKEN_MAX = Decimal("1.00")
"""A price at or below this is a placeholder, not a price. SAP stores 0.01 and
1.00 on valuation rows that are not really valued; read as a price they make
working capital zero and an EOQ-based maximum stock ~700x too large."""


class PriceMissing(str, Enum):
    """Why a material-plant has no unit price. The value is the stored code."""

    NOT_IN_MBEW = "price_not_in_mbew"
    """No MBEW row for this material at this plant."""

    NO_NEW_VALUATION = "price_no_new_valuation"
    """Split-valuated at this plant, with neither a NEW nor a NEW-INS row."""

    UNREADABLE_PRICE = "price_unreadable"
    """The row to read has no parseable VERPR."""

    BAD_PRICE_UNIT = "price_bad_price_unit"
    """PEINH is present but blank, zero or negative. Not assumed to be 1."""

    MISSING_CURRENCY = "price_missing_currency"
    """The source carries a currency column and this row's is blank."""

    TOKEN_VALUE = "price_token_value"
    """A placeholder at or below the floor, not a price."""

    AMBIGUOUS_PRICE = "price_ambiguous"
    """Several rows of the read type disagree; there is no defensible winner."""

    DELETED_MATERIAL_PLANT = "price_deleted_material_plant"
    """SAP flags this material-plant for deletion; its valuation is not read."""


@dataclass(frozen=True)
class Valuation:
    """One MBEW row, already reduced to what the rule needs."""

    valuation_type: str | None
    """BWTAR, trimmed and upper-cased; ``None`` for the blank (header or only) row."""

    price: Decimal | None
    """VERPR / PEINH. ``None`` when the row's own data was unusable -- see ``problem``."""

    currency: str | None = None
    problem: PriceMissing | None = None
    """Why ``price`` is ``None``, when the reader knows. A row with a problem is
    never read as a price."""


@dataclass(frozen=True)
class PriceChoice:
    """The outcome for one material-plant. Exactly one of ``price`` / ``missing`` is set."""

    price: Decimal | None = None
    currency: str | None = None
    valuation_type: str | None = None
    """The BWTAR read (``NEW`` / ``NEW-INS``), or ``None`` for the blank row."""

    missing: PriceMissing | None = None
    detail: str | None = None
    """Free text for a reviewer; the code in ``missing`` is what is counted."""

    mbew_value: Decimal | None = None
    """The value MBEW held when it was rejected as a placeholder, so a review can see it."""


def normalise_valuation_type(raw: object | None) -> str | None:
    """BWTAR as the rule compares it: trimmed, upper-cased, blank -> ``None``."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    return text or None


def choose_unit_price(
    rows: Sequence[Valuation], token_max: Decimal = DEFAULT_TOKEN_MAX
) -> PriceChoice:
    """The unit price at one plant from that plant's MBEW rows. See the module docstring."""
    if not rows:
        return PriceChoice(missing=PriceMissing.NOT_IN_MBEW)

    if any(row.valuation_type for row in rows):
        for wanted in NEW_TYPE_PRECEDENCE:
            read = [row for row in rows if row.valuation_type == wanted]
            if read:
                valuation_type: str | None = wanted
                break
        else:
            return PriceChoice(
                missing=PriceMissing.NO_NEW_VALUATION,
                detail="split-valuated with no NEW or NEW-INS row: "
                + ", ".join(sorted({row.valuation_type for row in rows if row.valuation_type})),
            )
    else:
        read = list(rows)
        valuation_type = None

    usable = [row for row in read if row.price is not None and row.problem is None]
    if not usable:
        problem = next((row.problem for row in read if row.problem is not None), None)
        return PriceChoice(
            valuation_type=valuation_type,
            missing=problem or PriceMissing.UNREADABLE_PRICE,
        )

    distinct = {(row.price, row.currency) for row in usable}
    if len(distinct) > 1:
        shown = ", ".join(f"{price}{f' {cur}' if cur else ''}" for price, cur in sorted(distinct, key=str))
        return PriceChoice(
            valuation_type=valuation_type,
            missing=PriceMissing.AMBIGUOUS_PRICE,
            detail=f"{len(usable)} rows disagree: {shown}",
        )

    price, currency = next(iter(distinct))
    assert price is not None
    if price <= token_max:
        return PriceChoice(
            valuation_type=valuation_type,
            missing=PriceMissing.TOKEN_VALUE,
            mbew_value=price,
            detail=f"{price} is at or below the {token_max} placeholder floor",
        )
    return PriceChoice(price=price, currency=currency, valuation_type=valuation_type)


def group_by_plant(
    rows: Iterable[tuple[str, str, Valuation]],
) -> dict[tuple[str, str], list[Valuation]]:
    """``(material, plant) -> its valuations``, from ``(material, plant, valuation)`` triples."""
    grouped: dict[tuple[str, str], list[Valuation]] = {}
    for material, plant, valuation in rows:
        grouped.setdefault((material, plant), []).append(valuation)
    return grouped
