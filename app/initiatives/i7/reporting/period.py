"""Calendar-quarter resolution for the I07 Quarterly Deep-Dive Report.

A pure function, deliberately kept separate from the aggregation service so
it is trivially unit-testable without a database session.
"""

from __future__ import annotations

import re
from datetime import date

_QUARTER_RE = re.compile(r"^Q([1-4])\s+(\d{4})$")

_QUARTER_START_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}
_QUARTER_END_MONTH_DAY = {
    1: (3, 31),
    2: (6, 30),
    3: (9, 30),
    4: (12, 31),
}


def latest_closed_quarter(today: date | None = None) -> str:
    """The most recently *completed* calendar quarter as of ``today``
    (defaults to :func:`date.today`), formatted the way :func:`resolve_quarter`
    accepts it, e.g. ``"Q3 2026"``.

    Lets a caller (API endpoint, CLI, external scheduler) ask for "whatever
    quarter just closed" without computing the quarter itself -- the one place
    this arithmetic lives. The current quarter is always in progress, so the
    latest *closed* one is always the previous one, wrapping the year at Q1::

        2026-10-01 (Q4 2026 in progress) -> "Q3 2026"
        2027-01-01 (Q1 2027 in progress) -> "Q4 2026"
    """
    today = today or date.today()
    current_q = (today.month - 1) // 3 + 1
    if current_q == 1:
        return f"Q4 {today.year - 1}"
    return f"Q{current_q - 1} {today.year}"


def resolve_quarter(quarter: str) -> tuple[date, date]:
    """Parse ``"Q3 2026"`` -> ``(date(2026, 7, 1), date(2026, 9, 30))``.

    ``period_end`` is the last calendar day of the quarter (inclusive), not
    the first day of the next quarter -- callers that need a half-open range
    for a ``< period_end_exclusive`` filter should add one day themselves
    rather than this function silently picking a convention for them.

    Raises ``ValueError`` for anything that does not match ``"Q<1-4> <year>"``
    exactly (e.g. lowercase ``"q3 2026"``, missing space, non-1..4 quarter).
    """
    match = _QUARTER_RE.match(quarter.strip())
    if not match:
        raise ValueError(
            f"'{quarter}' is not a valid quarter string; expected format 'Q<1-4> <year>', "
            "e.g. 'Q3 2026'."
        )
    q = int(match.group(1))
    year = int(match.group(2))

    start_month = _QUARTER_START_MONTH[q]
    end_month, end_day = _QUARTER_END_MONTH_DAY[q]

    period_start = date(year, start_month, 1)
    period_end = date(year, end_month, end_day)
    return period_start, period_end
