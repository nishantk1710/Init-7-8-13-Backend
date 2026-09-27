"""Parse raw text values by their source's *declared* format.

Every ``raw_*`` column is text, on purpose (see ``app.seed.reader``). But the
same text means different things depending on which route loaded the table:

    route               "1,000"         "0,989"       dates
    July workbooks      one thousand    (not seen)    2026-03-17
    SAP CSV extract     one             0.989         17.03.2026, 00.00.0000 = none

The CSV extract is written in SAP's German user settings: ``.`` groups
thousands, ``,`` is the decimal separator, and a quantity can carry SAP's
trailing minus (``5,000-``). The workbooks were the other way round.

**No rule applied to the value can tell those apart.** ``1,000`` is a valid
number in both formats, 1000 apart. So the format is declared by whoever
knows where the table came from and passed in; it is never inferred from what
the value looks like. That is the same rule the SAP client applies to Edm
types: decode by declared type, never by value shape.

**A value that does not fit its declared format is rejected, never
reinterpreted.** The parsers return ``None`` and the caller records a
rejection, so a changed extract format surfaces as a rejection count in the
staging report rather than as a plausible, wrong number. One change is
undetectable by construction and worth knowing about: if SAP switched to
``.`` decimals, ``1.000`` would still read as one thousand here, because it is
also a well-formed German-format thousand. ``1.5`` or ``0.989`` would be
rejected -- a grouped number never has a leading ``0`` group -- which is what
makes such a switch visible at all.

Values that are already typed -- an aggregate such as ``SUM(...)`` arrives as a
``Decimal``, a date column as a ``date`` -- are returned as they are. The format
describes *text*; applying it to ``str(Decimal("12.5"))`` would misread the
database's own ``.`` as a thousands separator.

Shared infrastructure: I07, I08 and I13 all read the same raw tables, and all
of them need the same answer to "what number is this".
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from datetime import date, datetime
from decimal import Decimal, InvalidOperation


def _iso_date(text: str) -> date:
    return date.fromisoformat(text)


def _german_date(text: str) -> date:
    return datetime.strptime(text, "%d.%m.%Y").date()


def _number_pattern(decimal: str, thousands: str, trailing_minus: bool) -> re.Pattern[str]:
    """A whole-string pattern for one format's numbers.

    Grouping is checked, not just stripped: ``12,34`` is not a workbook number
    and ``1.5`` is not a German one. Both would otherwise parse, to the wrong
    value. A grouped number's leading group is never ``0`` either, which is
    what rejects ``0.989`` in the German format instead of reading it as 989.
    """
    d, t = re.escape(decimal), re.escape(thousands)
    integer = rf"(?:[1-9]\d{{0,2}}(?:{t}\d{{3}})+|\d+)"
    body = rf"(?:{integer}(?:{d}\d*)?|{d}\d+)(?:[eE][+-]?\d+)?"
    if trailing_minus:
        return re.compile(rf"(?:[+-]?{body}|{body}-)")
    return re.compile(rf"[+-]?{body}")


@dataclass(frozen=True)
class ValueFormat:
    """How one load route writes numbers and dates into raw text columns."""

    name: str
    decimal_separator: str
    thousands_separator: str
    trailing_minus: bool
    """SAP writes negatives as ``5,000-``; the workbooks never did."""
    parse_date_text: Callable[[str], date]
    empty_dates: frozenset[str]
    """Spellings that mean "no date" rather than a malformed one."""

    @cached_property
    def number_pattern(self) -> re.Pattern[str]:
        return _number_pattern(
            self.decimal_separator, self.thousands_separator, self.trailing_minus
        )


WORKBOOK = ValueFormat(
    name="workbook",
    decimal_separator=".",
    thousands_separator=",",
    trailing_minus=False,
    parse_date_text=_iso_date,
    empty_dates=frozenset({"00000000", "0000-00-00"}),
)
"""The July/August workbook extracts loaded by ``app.seed``."""

SAP_CSV = ValueFormat(
    name="sap_csv",
    decimal_separator=",",
    thousands_separator=".",
    trailing_minus=True,
    parse_date_text=_german_date,
    empty_dates=frozenset({"00.00.0000", "00000000"}),
)
"""The live SAP CSV extract loaded by ``app.ingest`` (``csv:<TABLE>:<id>``)."""


def _text(value: object) -> str | None:
    stripped = value.strip() if isinstance(value, str) else str(value).strip()
    return stripped or None


def parse_decimal(value: object | None, fmt: ValueFormat) -> Decimal | None:
    """A number in ``fmt``, or ``None`` if absent or not well-formed in it."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value)) if value == value and abs(value) != float("inf") else None

    text = _text(value)
    if text is None or not fmt.number_pattern.fullmatch(text):
        return None

    negative = False
    if fmt.trailing_minus and text.endswith("-"):
        negative, text = True, text[:-1]
    normalised = text.replace(fmt.thousands_separator, "").replace(fmt.decimal_separator, ".")
    try:
        number = Decimal(normalised)
    except InvalidOperation:  # pragma: no cover - the pattern already excludes it
        return None
    return -number if negative else number


def parse_date(value: object | None, fmt: ValueFormat) -> date | None:
    """A date in ``fmt``, or ``None`` if absent, SAP's zero date, or malformed."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = _text(value)
    if text is None or text in fmt.empty_dates:
        return None
    try:
        return fmt.parse_date_text(text)
    except ValueError:
        return None
