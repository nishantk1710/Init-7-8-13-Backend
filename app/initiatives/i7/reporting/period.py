"""Calendar-period resolution for the I07 Deep-Dive Report: a quarter (``"Q3 2026"``) or a single month (``"Oct 2026"``).

Pure functions, deliberately kept separate from the aggregation service so
they are trivially unit-testable without a database session.

A monthly report is the same report over one month. It is saved in the same table under its own label, which never
collides with a quarter's (``"Oct 2026"`` against ``"Q4 2026"``), so the table needed no change; the code and routes
keep the name "quarterly" for that reason, and :func:`period_kind` says which kind a label is.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta

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


# --- The months of a period, and how much of it has happened -----------------------------------------

_MONTH_ABBREVIATIONS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def month_label(month: date) -> str:
    """``date(2026, 10, 1)`` -> ``"Oct 2026"``."""
    return f"{_MONTH_ABBREVIATIONS[month.month - 1]} {month.year}"


_MONTH_RE = re.compile(r"^(" + "|".join(_MONTH_ABBREVIATIONS) + r")\s+(\d{4})$")

#: The two kinds of period a report can cover.
QUARTER, MONTH = "quarter", "month"


def latest_closed_month(today: date | None = None) -> str:
    """The most recently *completed* calendar month as of ``today``, in the form :func:`resolve_period` accepts.
    The current month is always in progress, so this is always the previous one, wrapping the year at January::

        2026-11-01 -> "Oct 2026"
        2027-01-01 -> "Dec 2026"
    """
    today = today or date.today()
    return month_label(date(today.year, today.month, 1) - timedelta(days=1))


def latest_closed_period(kind: str, today: date | None = None) -> str:
    """The latest closed period of ``kind`` (``"quarter"`` or ``"month"``)."""
    if kind == MONTH:
        return latest_closed_month(today)
    if kind == QUARTER:
        return latest_closed_quarter(today)
    raise ValueError(f"unknown period kind '{kind}'; expected '{QUARTER}' or '{MONTH}'")


def period_kind(label: str) -> str:
    """``"quarter"`` for ``"Q4 2026"``, ``"month"`` for ``"Oct 2026"``; ``ValueError`` for anything else."""
    text = label.strip()
    if _QUARTER_RE.match(text):
        return QUARTER
    if _MONTH_RE.match(text):
        return MONTH
    raise ValueError(
        f"'{label}' is not a valid report period; expected a quarter 'Q<1-4> <year>' (e.g. 'Q3 2026') "
        "or a month '<Mon> <year>' (e.g. 'Oct 2026')."
    )


def resolve_period(label: str) -> tuple[date, date]:
    """Parse a quarter or a month label into ``(period_start, period_end)``, both inclusive.

    ``"Q3 2026"`` -> ``(2026-07-01, 2026-09-30)`` and ``"Oct 2026"`` -> ``(2026-10-01, 2026-10-31)``. As with
    :func:`resolve_quarter` the end is the last day of the period, not the first of the next. Raises ``ValueError``
    for anything that is neither form exactly (lowercase, no space, an unknown month).
    """
    if period_kind(label) == QUARTER:
        return resolve_quarter(label)
    match = _MONTH_RE.match(label.strip())
    assert match is not None  # period_kind just matched it
    month = _MONTH_ABBREVIATIONS.index(match.group(1)) + 1
    year = int(match.group(2))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def exclusive_end_of(period_end: date) -> date:
    """The day after ``period_end``: the exclusive upper bound every period filter uses, so a quarter ending
    on 31 December is ``< 1 January`` and a row stamped late on the 31st is still inside it."""
    return period_end + timedelta(days=1)


def months_of(period_start: date, period_end: date) -> list[date]:
    """The first day of every calendar month from ``period_start`` to ``period_end`` inclusive -- the three
    months of a quarter. Year transitions roll over (Q4 2026 is Oct, Nov, Dec; Q1 2027 follows)."""
    months: list[date] = []
    current = date(period_start.year, period_start.month, 1)
    while current <= period_end:
        months.append(current)
        current = date(current.year + 1, 1, 1) if current.month == 12 else date(current.year, current.month + 1, 1)
    return months


def reporting_cutoff(period_end: date, today: date | None = None) -> date:
    """The last day a report can speak for: the period's end, or today while the period is still running."""
    today = today or date.today()
    return min(period_end, today)


def is_period_complete(period_end: date, today: date | None = None) -> bool:
    """A period is complete once its last day has passed."""
    today = today or date.today()
    return today > period_end


def month_state(month: date, period_end: date, today: date | None = None) -> str:
    """``ended`` / ``in_progress`` / ``not_started`` for one month of a period, as of ``today``."""
    today = today or date.today()
    next_month = date(month.year + 1, 1, 1) if month.month == 12 else date(month.year, month.month + 1, 1)
    if today >= next_month:
        return "ended"
    if today >= month:
        return "in_progress"
    return "not_started"
