"""Which price currencies the Working Capital Impact can state in ZAR, and at what rate.

Kept free of models and SQL so the schemas, the calculation and the tests can all read one definition.
"""

from __future__ import annotations

from decimal import Decimal

REPORTING_CURRENCY = "ZAR"

TO_ZAR: dict[str, Decimal] = {"ZAR": Decimal(1), "NAD": Decimal(1)}
"""Currencies the figure includes, and what one unit is worth in ZAR (NAD is pegged to the rand). Anything
not here -- another currency, or none stated -- is left out, never converted or guessed."""


def zar_currency_for(price_currency: str | None) -> str | None:
    """``"ZAR"`` when a price in ``price_currency`` is counted in ZAR terms, else ``None``."""
    return REPORTING_CURRENCY if price_currency in TO_ZAR else None
