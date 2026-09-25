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


def most_recently_completed_quarter(today: date) -> str:
    """The last calendar quarter that has fully elapsed as of ``today``, as
    ``"Q<1-4> <year>"``. Used by the scheduled/CLI trigger so it does not need
    a hardcoded quarter string -- e.g. on any day in Q1 2027 this returns
    ``"Q4 2026"``, never the still-in-progress current quarter.
    """
    current_q = (today.month - 1) // 3 + 1
    if current_q == 1:
        return f"Q4 {today.year - 1}"
    return f"Q{current_q - 1} {today.year}"
