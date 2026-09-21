"""Text out of the raw layer, into the types the serving layer declares.

Everything in ``odata_*`` is text, deliberately: SAP keys are digit strings and
any numeric coercion at load time turns ``000000008000000000`` into ``8e+15``
irreversibly. That decision is paid for here instead, once, against a reviewed
mapping.

Two shapes cause most of the trouble.

**Space-padded decimals.** MARC's safety stock arrives as
``'              0.000'`` -- right-aligned in a 20-character field, because the
property drifted ``Edm.Decimal`` -> ``Edm.String`` and SAP now sends the
display form. ``float()`` happens to cope; ``Decimal()`` does not, and neither
does anything comparing it to a number.

**Three date forms.** ABAP DATS (``20260916``), ISO (``2026-09-16``), and
OData v2's epoch wrapper (``/Date(1757462400000)/``). The envelope decodes the
third when it knows the declared type, but a value that reached the raw layer
as text has lost that, so all three are handled.

Every function returns ``None`` rather than raising. A single unparseable cell
must not cost the load of two million rows, and a ``None`` in one column is
visible downstream in a way an aborted build is not.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

# OData v2 serialises Edm.DateTime as /Date(<epoch millis>)/, optionally with a
# trailing offset. Same pattern the envelope uses; repeated rather than imported
# because this module must work on text that never went through the envelope.
_SAP_EPOCH = re.compile(r"^/Date\((?P<millis>-?\d+)(?P<offset>[+-]\d{4})?\)/$")

# ABAP DATS. 00000000 is SAP's "no date", which is not the same as an unparseable
# one and must not become 0000-01-01.
_DATS = re.compile(r"^\d{8}$")
_DATS_NULL = "00000000"


def text(value: object) -> str | None:
    """Trimmed text, or None for blank.

    SAP pads fixed-width character fields, so trailing spaces are formatting
    rather than content. An empty string after trimming is not a value.
    """
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def number(value: object) -> Decimal | None:
    """A decimal, tolerating the space padding SAP sends.

    ``Decimal`` and not ``float``: these are quantities and money. A float
    cannot hold 0.1 exactly, and a safety stock that reads 2.0000000000000004
    in a report is a support ticket.
    """
    raw = text(value)
    if raw is None:
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def integer(value: object) -> int | None:
    """A whole number. Accepts a decimal form with a zero fraction."""
    parsed = number(value)
    if parsed is None:
        return None
    try:
        if parsed != parsed.to_integral_value():
            return None
        return int(parsed)
    except (InvalidOperation, ValueError):
        return None


def day(value: object) -> date | None:
    """A date from any of the three forms SAP uses. None if it is not one."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = text(value)
    if raw is None:
        return None

    match = _SAP_EPOCH.match(raw)
    if match:
        seconds = int(match.group("millis")) / 1000
        # Explicitly UTC. The offset group, where SAP sends one, has always
        # been +0000 on this service, and taking the local date of a UTC
        # instant would move a date by a day for anyone west of Greenwich.
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()

    if _DATS.match(raw):
        if raw == _DATS_NULL:
            # SAP's "no date". Distinct from an unparseable one, and both are
            # correctly None here -- but only this branch is expected.
            return None
        try:
            return datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            return None

    try:
        # fromisoformat handles both a plain date and a full timestamp.
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def flag(value: object, *, true_when: str = "X") -> bool:
    """An ABAP checkbox. Blank is false, 'X' is true.

    ``true_when`` because not every boolean-ish SAP field uses X, and guessing
    at the call site is how a deletion flag gets read backwards.
    """
    raw = text(value)
    return raw is not None and raw.upper() == true_when.upper()
