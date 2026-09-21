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

**Lead time is a second, independent signal -- not a fallback.** Confirmed by
the team lead on 21-Sep: *"can we keep the aging and when the aging goes beyond
lead time we can highlight it"*, and it applies to **every** line, not only the
63 without a due date. So :func:`overdue_state` and :func:`lead_time_state` are
separate functions answering separate questions, and a line can be ON_TIME
against its agreed date while already BEYOND_LEAD_TIME against the standard:

    overdue_state    did this line pass the date somebody promised?
    lead_time_state  has it taken longer than this material normally takes?

The first is a commitment, the second is a benchmark. Ruling out precedence
between them was deliberate -- a line that is late by one measure and not the
other is a finding, not a contradiction to be resolved away.

**The grace period does NOT apply to the lead-time check.** Also confirmed on
21-Sep, asked explicitly and answered "No not needed". Grace exists because a
promised date is a commitment somebody made and a few days' slack is courtesy;
a planned delivery time is already an average with slack baked into it, and
discounting it twice would just move the threshold nobody agreed to.
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

# Lead-time states. Parallel to the overdue ones and deliberately named apart:
# nothing here means "late", it means "longer than this material usually takes".
NO_LEAD_TIME: Final = "NO_LEAD_TIME"
WITHIN_LEAD_TIME: Final = "WITHIN_LEAD_TIME"
BEYOND_LEAD_TIME: Final = "BEYOND_LEAD_TIME"

LEAD_TIME_STATES: Final[tuple[str, ...]] = (
    NO_LEAD_TIME,
    WITHIN_LEAD_TIME,
    BEYOND_LEAD_TIME,
)


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


def elapsed_days(
    *, raised_at: date | None, received_at: date | None, today: date
) -> int | None:
    """How long this repair has actually taken, so far or in total.

    To the receipt where there is one, to today where there is not -- the same
    shape as ``daysAtVendor``, and the reason a closed repair stops ageing the
    moment the unit comes back. ``days_open`` deliberately keeps counting to
    today for every line; that is the right answer for "how old is this record"
    and the wrong one for "did this repair overrun", which is what the
    lead-time check asks.

    >>> elapsed_days(raised_at=date(2026, 1, 1), received_at=date(2026, 2, 1),
    ...              today=date(2026, 9, 1))
    31
    >>> elapsed_days(raised_at=date(2026, 1, 1), received_at=None,
    ...              today=date(2026, 3, 2))
    60
    >>> elapsed_days(raised_at=None, received_at=None, today=date(2026, 3, 2)) is None
    True
    """
    return days_between(raised_at, received_at or today)


def lead_time_state(
    *, elapsed: int | None, lead_time_days: int | None
) -> str:
    """Whether this repair has run past the planned delivery time.

    ``lead_time_days`` is ``MARC.PLIFZ`` for the line's material at its plant --
    planned delivery time, in CALENDAR days, measured PO to received. Confirmed
    with Khushi on 21-Sep as the same field and the same meaning Initiative 07
    uses, so the two initiatives cannot report different turnarounds for the
    same part.

    **Zero and NULL both mean "not maintained", never "this repair should take
    no days".** PLIFZ is routinely left blank on non-stock and service-type
    materials, and reading a blank literally would put every such line into
    breach on the day its PO was raised -- a register that is entirely red says
    nothing. Not flagging is the safe failure here; falsely flagging is not.

    No grace period, by ruling. See the module docstring.

    >>> lead_time_state(elapsed=30, lead_time_days=21)
    'BEYOND_LEAD_TIME'
    >>> lead_time_state(elapsed=21, lead_time_days=21)
    'WITHIN_LEAD_TIME'
    >>> lead_time_state(elapsed=300, lead_time_days=0)
    'NO_LEAD_TIME'
    >>> lead_time_state(elapsed=300, lead_time_days=None)
    'NO_LEAD_TIME'
    >>> lead_time_state(elapsed=None, lead_time_days=21)
    'NO_LEAD_TIME'
    """
    if lead_time_days is None or lead_time_days <= 0 or elapsed is None:
        return NO_LEAD_TIME
    return BEYOND_LEAD_TIME if elapsed > lead_time_days else WITHIN_LEAD_TIME


def days_over_lead_time(
    *, elapsed: int | None, lead_time_days: int | None
) -> int | None:
    """Days past the planned delivery time. Negative while still inside it.

    The sign matches ``days_remaining``, which the frontend already documents as
    "negative once overdue" -- inverted here because this number counts up as
    things get worse, and a column that means the opposite of its neighbour by
    the same sign is how a reader misreads a queue.

    None when there is no lead time to measure against, rather than 0: "we were
    never told how long this takes" and "it finished exactly on time" are
    different answers, and a dashboard that averages them together reports a
    fleet of unmaintained materials as perfectly punctual.

    >>> days_over_lead_time(elapsed=30, lead_time_days=21)
    9
    >>> days_over_lead_time(elapsed=14, lead_time_days=21)
    -7
    >>> days_over_lead_time(elapsed=30, lead_time_days=0) is None
    True
    """
    if lead_time_days is None or lead_time_days <= 0 or elapsed is None:
        return None
    return elapsed - lead_time_days
