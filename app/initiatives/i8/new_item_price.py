"""New Item Price -- what a part costs to buy new, read from MBEW.

Why the coding screen shows it
------------------------------
A coding candidate is a material somebody should perhaps have coded
repairable. Whether recoding it is worth the effort depends on what the part
costs new: a R5 patch kit is not worth a repair process, a R5m scoop is. The
price is shown per plant, beside the verdict, so a reviewer can weigh the two.

Where it comes from
-------------------
MBEW VERPR, the moving average price -- confirmed as the source by the SAP team
on 16-Oct-2026. It is SAP's own running average of what was paid at goods
receipt, which is why it agreed with the last received PO price to within 0.5%
on 81 of 91 candidate material-plants where both exist (15-Oct-2026 export).

Nothing else is a fallback. A PO price was looked at and ruled out on
16-Oct-2026: contracts and quotations carry prices nobody paid, and received
POs would have filled 7 of 21 gaps with 2013-2018 prices.

Which MBEW row
--------------
Per material AND plant (BWKEY is the plant here):

* **split-valuated** at that plant -- any row carries a BWTAR, as every
  80-series material does: the ``NEW`` row (``I8_NEW_ITEM_VALUATION_TYPE``).
  The blank-BWTAR row is an aggregate across conditions there, and can carry
  the refurbished value -- ``43253306`` reads NEW R38,122 against R34,840.62
  on both the blank row and REFURBISHD.
* **not split**: the one blank-BWTAR row. Most coding candidates are like
  this -- they are not 80-series by definition.

The price is VERPR / PEINH. Three ways to have none, each named rather than
sent as zero:

* ``TOKEN_VALUE`` -- the row holds a placeholder at or below
  ``I8_NEW_ITEM_PRICE_TOKEN_MAX`` (R1.00 / R0.01). The value is kept so the
  screen can quote it.
* ``NO_NEW_VALUATION`` -- split-valuated, but no NEW row.
* ``NOT_IN_MBEW`` -- no row for that material at that plant.

An unreadable VERPR or PEINH counts as no row (blank or zero PEINH is refused,
not assumed to be 1 -- the same rule as I07's MBEW reader).

The rule (:func:`choose_price`) is pure, so every case is a unit test; the
lookup (:func:`fetch_new_item_prices`) only fetches rows for it.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.staged import reading, valuation_sources
from app.shared.sql_lists import fetch_in_chunks

logger = get_logger(__name__)


class MissingPrice(str, Enum):
    """Why a material-plant has no new-item price."""

    TOKEN_VALUE = "TOKEN_VALUE"
    """MBEW holds a placeholder valuation (R1.00, R0.01), not a price."""

    NO_NEW_VALUATION = "NO_NEW_VALUATION"
    """Split-valuated at the plant, with no NEW row to read."""

    NOT_IN_MBEW = "NOT_IN_MBEW"
    """No MBEW row for the material at the plant."""


#: The count key for a material-plant that has a price.
PRICED = "PRICED"


@dataclass(frozen=True)
class Valuation:
    """One MBEW row at one plant, as the rule needs it."""

    valuation_type: str | None
    """BWTAR, upper-cased; None on the blank (header or only) row."""

    price: Decimal | None
    """VERPR / PEINH; None when either was unreadable."""


@dataclass(frozen=True)
class NewItemPrice:
    """What a material costs new at one plant, or why that is not known."""

    plant: str
    price: Decimal | None
    """VERPR / PEINH, in ZAR (MBEW carries no currency; I08 is ZAR
    throughout). None whenever :attr:`missing` is set -- never zero."""

    valuation_type: str | None = None
    """The BWTAR read: ``NEW`` for a split-valuated material, None for one
    that is not split."""

    missing: str | None = None
    """A :class:`MissingPrice` value, or None when there is a price."""

    mbew_value: Decimal | None = None
    """The value MBEW holds on the row read. Equal to :attr:`price` when there
    is one; on ``TOKEN_VALUE`` it is the placeholder, so it can be quoted."""

    @property
    def state(self) -> str:
        return self.missing or PRICED


def unit_price(verpr: object, peinh: object) -> Decimal | None:
    """VERPR per one unit. None when either is unreadable or PEINH is not positive."""
    price, per = _decimal(verpr), _decimal(peinh)
    if price is None or per is None or per <= 0:
        return None
    return price / per


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except InvalidOperation:
        return None


def choose_price(
    rows: Sequence[Valuation], plant: str, cfg: I8Settings | None = None
) -> NewItemPrice:
    """The new-item price at ``plant``, from that plant's MBEW rows.

    See the module docstring for the rule. Several rows of the type read --
    which would be a data error, and was seen nowhere in scope on 15-Oct-2026
    -- resolve to the highest, as I07's MBEW reader does.
    """
    cfg = cfg or get_i8_settings()
    if not rows:
        return NewItemPrice(plant=plant, price=None, missing=MissingPrice.NOT_IN_MBEW.value)

    wanted = cfg.new_item_valuation_type.strip().upper()
    split = any(row.valuation_type for row in rows)
    if split:
        read = [row for row in rows if row.valuation_type == wanted]
        valuation_type: str | None = wanted
        absent = MissingPrice.NO_NEW_VALUATION
    else:
        read = list(rows)
        valuation_type = None
        absent = MissingPrice.NOT_IN_MBEW

    prices = [row.price for row in read if row.price is not None]
    if not prices:
        return NewItemPrice(plant=plant, price=None, missing=absent.value)

    value = max(prices)
    if value <= cfg.new_item_price_token_max:
        return NewItemPrice(
            plant=plant,
            price=None,
            valuation_type=valuation_type,
            missing=MissingPrice.TOKEN_VALUE.value,
            mbew_value=value,
        )
    return NewItemPrice(plant=plant, price=value, valuation_type=valuation_type, mbew_value=value)


_VALUATIONS_SQL = "select matnr, bwkey, bwtar, verpr, peinh from v_mbew where matnr in :materials"


def fetch_new_item_prices(
    db: Session,
    plants_by_material: Mapping[str, Iterable[str | None]],
    cfg: I8Settings | None = None,
    *,
    staged: bool = True,
) -> dict[str, tuple[NewItemPrice, ...]]:
    """Material -> its new-item price at each of its plants, in plant order.

    ``plants_by_material`` is the plants each material was found at -- on the
    coding screen, the plants of its evidence lines. A material with no plant
    gets no entry. One read of ``v_mbew`` for every material at once, staged
    on SQL Server (see :func:`app.initiatives.i8.staged.valuation_sources`).
    """
    cfg = cfg or get_i8_settings()
    wanted = {
        material: sorted({p.strip() for p in plants if p and p.strip()})
        for material, plants in plants_by_material.items()
        if material
    }
    wanted = {material: plants for material, plants in wanted.items() if plants}
    if not wanted:
        return {}

    started = time.monotonic()
    materials = sorted(wanted)
    rows: dict[tuple[str, str], list[Valuation]] = {}
    unreadable = 0
    with valuation_sources(db, materials, enabled=staged) as src:
        for row in fetch_in_chunks(db, reading(_VALUATIONS_SQL, src), "materials", materials):
            price = unit_price(row["verpr"], row["peinh"])
            if price is None:
                unreadable += 1
            bwtar = (row["bwtar"] or "").strip().upper() or None
            rows.setdefault((row["matnr"], (row["bwkey"] or "").strip()), []).append(Valuation(bwtar, price))

    prices = {
        material: tuple(choose_price(rows.get((material, plant), ()), plant, cfg) for plant in plants)
        for material, plants in wanted.items()
    }
    states = Counter(price.state for found in prices.values() for price in found)
    logger.info(
        "I08 new item price: %d material-plants in %.1fs (%s)%s",
        sum(states.values()),
        time.monotonic() - started,
        ", ".join(f"{state} {n}" for state, n in sorted(states.items())),
        f"; {unreadable} MBEW row(s) had no readable VERPR/PEINH" if unreadable else "",
    )
    return prices
