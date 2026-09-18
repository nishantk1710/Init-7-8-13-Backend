"""W5.2 Layer 3 -- per-stage aging and the overdue rule.

Pure functions over dates. No database, no session, no settings object --
every rule here takes the numbers it needs (grace days, reference date, aging
boundaries) as plain parameters, so it is unit-testable without any
environment at all. ``I8Settings`` unpacks its fields at the call site; this
module never imports it.

Two decisions worth reading before changing anything.

**The reference date is a parameter, not ``date.today()``.** The extract is a
July-2026 snapshot. Measured against the wall clock every open line is already
weeks old, every number moves each morning, and a test that asserts "this line
is 43 days overdue" passes this week and fails next. ``I8_REFERENCE_DATE`` pins
it for demos and the UAT pack; blank means today.

**A missing due date is its own state.** 63 of the 1,225 repair lines have no
schedule line in EKET at all. Letting a NULL fall through to "not overdue"
hides them: they are precisely the lines nobody is chasing, because nobody
agreed a date. They come back as ``NO_DUE_DATE`` so the queue can chase them.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

# The default bands -- what the frontend rendered before either side could
# read these from configuration. Still the fallback when I8_AGING_BAND_
# BOUNDARIES is left unset, and still what AGING_BUCKETS below equals.
DEFAULT_AGING_BOUNDARIES: Final[tuple[int, ...]] = (15, 30, 45, 60)


def bucket_labels(boundaries: tuple[int, ...]) -> tuple[str, ...]:
    """Band labels derived from ascending day boundaries.

    The number of bands is however many boundaries are configured, not a
    fixed five -- a shorter or longer list changes how many bands exist, not
    just where they fall.

    >>> bucket_labels((15, 30, 45, 60))
    ('0-15', '16-30', '31-45', '46-60', '60+')
    >>> bucket_labels((10, 20))
    ('0-10', '11-20', '20+')
    """
    labels: list[str] = []
    lower = 0
    for limit in boundaries:
        labels.append(f"{lower}-{limit}")
        lower = limit + 1
    labels.append(f"{boundaries[-1]}+")
    return tuple(labels)


# Kept as the default-configuration label set. A test asserts this still
# equals the frontend's AgingBucket literal union -- that type is what a
# custom I8_AGING_BAND_BOUNDARIES value now has to widen away from, on the
# frontend side, to actually render a different band count.
AGING_BUCKETS: Final[tuple[str, ...]] = bucket_labels(DEFAULT_AGING_BOUNDARIES)

# Overdue states. RECEIVED and NO_DUE_DATE are outcomes, not failures to decide.
RECEIVED: Final = "RECEIVED"
NO_DUE_DATE: Final = "NO_DUE_DATE"
OVERDUE: Final = "OVERDUE"
ON_TIME: Final = "ON_TIME"

OVERDUE_STATES: Final[tuple[str, ...]] = (RECEIVED, NO_DUE_DATE, OVERDUE, ON_TIME)


def aging_bucket(
    days_open: int | None,
    boundaries: tuple[int, ...] = DEFAULT_AGING_BOUNDARIES,
) -> str | None:
    """Which aging band this many days falls in, or None if unknown.

    Negative input (a future start date, which the extract does contain) is
    treated as day zero rather than silently bucketed as the top band.

    ``boundaries`` defaults to the shipped bands so every existing caller and
    doctest keeps working unchanged; a caller passing
    ``cfg.aging_band_boundaries_list`` gets the configured bands instead.

    >>> aging_bucket(0), aging_bucket(15), aging_bucket(16), aging_bucket(61)
    ('0-15', '0-15', '16-30', '60+')
    >>> aging_bucket(None) is None
    True
    """
    if days_open is None:
        return None
    days = max(days_open, 0)
    labels = bucket_labels(boundaries)
    for limit, label in zip(boundaries, labels):
        if days <= limit:
            return label
    return labels[-1]


def days_between(start: date | None, end: date | None) -> int | None:
    """Whole days from ``start`` to ``end``, or None if either is unknown.

    None rather than 0 on purpose: "we do not know how long this took" and "it
    took no time" are different answers, and averaging them together is how a
    vendor with no recorded dispatch ends up looking instantaneous.

    >>> days_between(date(2026, 1, 1), date(2026, 1, 31))
    30
    >>> days_between(None, date(2026, 1, 31)) is None
    True
    """
    if start is None or end is None:
        return None
    return (end - start).days


def overdue_state(
    *,
    received_at: date | None,
    due_date: date | None,
    today: date,
    grace_days: int,
) -> str:
    """Where this line stands against its promised delivery date.

    Overdue is measured against the schedule-line delivery date plus a
    configurable grace period, and only for lines that have not come back yet.

    >>> overdue_state(received_at=date(2026, 2, 1), due_date=date(2026, 1, 1),
    ...               today=date(2026, 3, 1), grace_days=7)
    'RECEIVED'
    >>> overdue_state(received_at=None, due_date=None,
    ...               today=date(2026, 3, 1), grace_days=7)
    'NO_DUE_DATE'
    >>> overdue_state(received_at=None, due_date=date(2026, 1, 1),
    ...               today=date(2026, 3, 1), grace_days=7)
    'OVERDUE'
    >>> overdue_state(received_at=None, due_date=date(2026, 3, 5),
    ...               today=date(2026, 3, 1), grace_days=7)
    'ON_TIME'
    """
    if received_at is not None:
        return RECEIVED
    if due_date is None:
        # 63 lines. A finding to chase, not a silent pass.
        return NO_DUE_DATE
    if today > due_date + timedelta(days=grace_days):
        return OVERDUE
    return ON_TIME


def days_remaining(due_date: date | None, today: date) -> int | None:
    """Days left until the promised date. Negative once it has passed.

    Matches the frontend's ``daysRemainingInRepair``, which documents itself as
    "negative once overdue" -- so the sign is the contract, not a detail.

    >>> days_remaining(date(2026, 3, 10), date(2026, 3, 1))
    9
    >>> days_remaining(date(2026, 2, 20), date(2026, 3, 1))
    -9
    """
    if due_date is None:
        return None
    return (due_date - today).days
