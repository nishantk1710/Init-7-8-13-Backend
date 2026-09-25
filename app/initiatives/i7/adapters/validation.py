"""Parsing and validation for raw extract values.

Every raw column is ``text`` -- deliberately, since coercing
``000000008000000000`` to a number is irreversible -- so the adapter parses, and
parsing is where bad data surfaces.

Two rules:

* **A rejection reason is a stable code, not prose.** They are counted and
  grouped, and free text cannot be.
* **Nothing is silently dropped.** A parse failure returns ``None`` and the
  caller records a rejection, so every excluded row is accounted for.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from app.initiatives.i7.adapters.field_map import (
    DELETION_FLAG_TRUE,
    PURCHASING_DELETION_INDICATORS,
)


class RejectionReason:
    """Stable rejection codes.

    A class of constants rather than an enum: these are written to a ``String``
    column and grouped in SQL, and the string is the value.
    """

    MISSING_MATERIAL = "missing_material"
    MISSING_PLANT = "missing_plant"
    MISSING_PERIOD = "missing_period"
    INVALID_DATE = "invalid_date"
    INVALID_QUANTITY = "invalid_quantity"
    NEGATIVE_QUANTITY = "negative_quantity"
    MISSING_PURCHASING_DOCUMENT = "missing_purchasing_document"
    RECEIPT_BEFORE_CREATION = "receipt_before_creation"
    UNKNOWN_MOVEMENT_TYPE = "unknown_movement_type"
    DUPLICATE_KEY = "duplicate_key"


def clean(value: object | None) -> str | None:
    """Trim, and treat blank as absent.

    The extract writes an empty cell as an empty string. Blank and NULL mean the
    same thing here -- not maintained -- and collapsing them early stops every
    caller having to check both.

    Accepts non-strings because not every value reaching the adapter came
    straight from a text column: an aggregate such as ``SUM(quantity::numeric)``
    arrives as a ``Decimal``, and a parser that assumed ``str`` would fail on it.
    """
    if value is None:
        return None
    stripped = value.strip() if isinstance(value, str) else str(value)
    return stripped or None


def parse_date(value: object | None) -> date | None:
    """ISO ``YYYY-MM-DD`` as the extract writes it. ``None`` if unparseable.

    SAP's own zero-date ``00000000`` and its ISO spelling ``0000-00-00`` both
    mean "no date", and ``date.fromisoformat`` rejects them, so they fall out
    here as ``None`` rather than raising.
    """
    text = clean(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def parse_decimal(value: object | None) -> Decimal | None:
    """Parse a quantity. ``None`` if absent or not a number.

    Handles the thousands separators Excel sometimes leaves in an export.
    ``Decimal``, not ``float``: these feed stock and money arithmetic.
    """
    text = clean(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def parse_int(value: object | None) -> int | None:
    """Parse a whole number, tolerating a decimal point.

    PLIFZ arrives as ``"14"`` but sometimes ``"14.0"``; ``int()`` rejects the
    second, so route through Decimal.
    """
    parsed = parse_decimal(value)
    if parsed is None:
        return None
    try:
        return int(parsed)
    except (InvalidOperation, ValueError):
        return None


def parse_flag(value: object | None) -> bool | None:
    """SAP's boolean: ``X`` is true, blank is false.

    Blank returns ``False`` rather than ``None`` -- for LVORM the absence of a
    deletion flag genuinely means "not deleted", unlike MSTAE where absence
    means "not classified".
    """
    text = clean(value)
    if text is None:
        return False
    return text.upper() == DELETION_FLAG_TRUE


def parse_purchasing_deletion(value: object | None) -> bool:
    """EKPO/EKKO ``LOEKZ`` -- a code, not a boolean.

    ``L`` (deleted) and ``S`` (blocked) both mean the line is not a real
    replenishment. Deliberately separate from :func:`parse_flag`: LOEKZ never
    holds ``X``, so routing it through the boolean parser reports every
    cancelled line as active.
    """
    text = clean(value)
    if text is None:
        return False
    return text.strip().upper() in PURCHASING_DELETION_INDICATORS


def first_of_month(value: date) -> date:
    return value.replace(day=1)
