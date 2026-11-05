"""What the quantity suggestion saved on an OAR request line (3 Nov 2026).

The requester names the quantity they need first; only then is the suggestion
shown, and they lock one of the two. What they asked for less what they locked
is the saving -- in units, and in ZAR at the plant's replacement price.

The price is I07's, read from where I07 stages it
(``i7_staged_material_plant.unit_price``): MBEW VERPR / PEINH from the plant's
own valuation rows, ``NEW`` before ``NEW-INS`` for a split-valuated material
(:mod:`app.shared.valuation_price`). Reading the staged figure rather than
re-deriving it means a part is never valued two ways by two initiatives.

No price is a named gap, never a zero: the units still count, the value does
not.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.initiatives.i7.kpi_formulas.currency import REPORTING_CURRENCY, TO_ZAR
from app.initiatives.i8.material_number import normalise

CHOICE_SUGGESTED = "suggested"
CHOICE_OWN = "own"
QUANTITY_CHOICES = (CHOICE_SUGGESTED, CHOICE_OWN)

BELOW_SUGGESTION_KIND = "QUANTITY_BELOW_SUGGESTION"
"""Justification kind for keeping a quantity under the suggestion. Evidence
only: ACT raises nothing for it (it reads ``QUANTITY_OVERRIDE``)."""

PRICE_NOT_STAGED = "price_not_staged"
"""I07 has staged no row for this material at this plant."""

PRICE_NOT_ZAR = "price_not_zar"
"""The price is in a currency I07 does not state in ZAR, or in none."""

_MONEY = Decimal("0.01")

_MISSING_TEXT = {
    PRICE_NOT_STAGED: "not in the I07 price data for this plant",
    PRICE_NOT_ZAR: "not stated in ZAR",
    "price_not_in_mbew": "no MBEW row at this plant",
    "price_no_new_valuation": "no NEW or NEW-INS valuation",
    "price_unreadable": "MBEW price unreadable",
    "price_unit_column_missing": "MBEW extract has no price unit (PEINH) column",
    "price_unit_missing": "MBEW price unit (PEINH) is blank",
    "price_unit_invalid": "MBEW price unit (PEINH) is zero, negative or not a number",
    "price_bad_price_unit": "MBEW price unit (PEINH) is blank or zero",  # legacy code on older staged rows
    "price_missing_currency": "MBEW row has no currency",
    "price_token_value": "MBEW holds a placeholder price",
    "price_ambiguous": "MBEW rows disagree",
    "price_deleted_material_plant": "material is flagged for deletion at this plant",
}


@dataclass(frozen=True)
class UnitPrice:
    """One material's replacement price at one plant, or why there is none."""

    price: Decimal | None = None
    currency: str | None = None
    valuation_type: str | None = None
    missing: str | None = None

    @property
    def note(self) -> str | None:
        """Why there is no price, in words for the screen."""
        if self.missing is None:
            return None
        return "No price: " + _MISSING_TEXT.get(self.missing, self.missing.replace("_", " "))


def unit_price(db: DbSession, material: str, plant: str) -> UnitPrice:
    """The plant's replacement price as I07 staged it. See the module docstring."""
    from app.models.i7_staging import StagedMaterialPlant  # local: keeps the assistant import light

    canonical = normalise(material) or (material or "").strip()
    keys = {canonical, canonical.zfill(18)}
    row = db.execute(
        select(StagedMaterialPlant)
        .where(
            StagedMaterialPlant.sap_material_number.in_(keys),
            StagedMaterialPlant.sap_plant_code == (plant or "").strip(),
            StagedMaterialPlant.is_active,  # not .is_(True): SQL Server refuses "IS 1"
        )
        .order_by(StagedMaterialPlant.staging_run_id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return UnitPrice(missing=PRICE_NOT_STAGED)
    if row.unit_price is None:
        return UnitPrice(missing=row.price_missing_reason or "price_unreadable")
    if row.currency not in TO_ZAR:
        return UnitPrice(currency=row.currency, missing=PRICE_NOT_ZAR)
    return UnitPrice(
        price=row.unit_price * TO_ZAR[row.currency],
        currency=REPORTING_CURRENCY,
        valuation_type=row.price_valuation_type,
    )


def units_saved(
    required: Decimal, suggested: Decimal | None, final: Decimal, *, not_needed: bool = False
) -> Decimal | None:
    """Required less final, never below 0. ``None`` when no suggestion was made.

    A line marked not needed counts its whole quantity only when the suggestion
    was 0 -- stock already covered it. Against a positive suggestion, dropping
    the line was the requester's own call, not something the suggestion saved.
    """
    if suggested is None:
        return None
    if not_needed and suggested > 0:
        return Decimal("0")
    return max(required - final, Decimal("0"))


def value_saved(units: Decimal | None, price: UnitPrice) -> Decimal | None:
    """``units`` x the unit price, to the cent; ``None`` without either."""
    if units is None or price.price is None:
        return None
    return (units * price.price).quantize(_MONEY)


__all__ = [
    "BELOW_SUGGESTION_KIND",
    "CHOICE_OWN",
    "CHOICE_SUGGESTED",
    "PRICE_NOT_STAGED",
    "PRICE_NOT_ZAR",
    "QUANTITY_CHOICES",
    "UnitPrice",
    "unit_price",
    "units_saved",
    "value_saved",
]
