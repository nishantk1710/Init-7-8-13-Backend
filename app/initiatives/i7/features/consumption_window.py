"""The demand observation window: ``window_months`` calendar months ending at the latest verified complete month.

This module is the one place that decides which months of consumption I07 reads for demand classification
(ADI / CV-squared), forecasting and the stock figures. The feature builder resolves the window once per build
and records it on the feature run; forecasting reads it back from that run, and the inventory stage reads the
statistics the builder computed over it, so the three cannot drift apart.

**A month is not complete because it has rows.** ``i7_staged_consumption`` holds a row per material-plant-month
that had a movement, so the staged table cannot say "this month was delivered in full" -- the last three months
of the July 2026 extract hold 339, 13 and 1 movements against about 2,400 in May, and every one of them has rows.
Counting them as months with zero demand would drag every mean and every safety stock down, so each month is
judged against the months before it (:class:`~app.initiatives.i7.policy.thresholds.ConsumptionWindowPolicy`):

* its movement volume is compared with the median volume of the complete months before it;
* a month under ``completeness_min_ratio`` of that is ``MISSING_DATA``;
* an operator who has checked a month against SAP can validate it (``I7_DEMAND_VALIDATED_MONTHS``); reloading the
  extract is the other way out.

A flagged month's volume is **not** added to the reference, so one bad month cannot lower the bar for the next.
The window ends on the latest complete month; the months after it are reported as ``trailing_incomplete_months``
and are not read. Months inside the window that are flagged make the window ``MISSING_DATA``: statistics over it
would have to treat a month nobody observed as either zero or absent, and neither is true.

**Zero is a finding, not a default.** Inside a ``COMPLETE`` month a material-plant with no row consumed nothing and
its observation is an explicit zero (:func:`app.initiatives.i7.features.builder._densify`). Outside one, it is
unknown.

The window starts ``window_months - 1`` months before its end, or at the first month the delivery holds if that
is later -- months before the delivery began are not observed, so they are neither zeros nor missing.

Nothing here knows the system date. Today says nothing about which months were delivered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from statistics import median
from typing import Iterable, Mapping

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.initiatives.i7.policy.thresholds import ConsumptionWindowPolicy
from app.models.i7_features import FeatureBuildRun
from app.models.i7_staging import StagedConsumption


class MonthStatus(StrEnum):
    COMPLETE = "COMPLETE"
    """Delivered in full (or validated by an operator). A material-plant with no movement in it consumed nothing."""

    MISSING_DATA = "MISSING_DATA"
    """Absent or incomplete in the source. Never zero consumption, and never the end of the window."""


class WindowStatus(StrEnum):
    USABLE = "USABLE"
    """Every month of the window is complete."""

    MISSING_DATA = "MISSING_DATA"
    """A month inside the window is missing or incomplete. Nothing computed over it is reliable."""

    NO_COMPLETE_MONTH = "NO_COMPLETE_MONTH"
    """Consumption is staged but no month of it can be verified complete."""

    NO_SOURCE_DATA = "NO_SOURCE_DATA"
    """No consumption is staged at all."""


@dataclass(frozen=True)
class MonthAssessment:
    """One calendar month of the delivery and why it is, or is not, complete."""

    month: date
    status: MonthStatus
    volume: int
    reference: float | None
    reason: str


@dataclass(frozen=True)
class ConsumptionWindow:
    status: WindowStatus
    start: date | None
    end: date | None
    window_months: int
    first_staged_month: date | None
    latest_staged_month: date | None
    assessments: tuple[MonthAssessment, ...] = field(default=(), repr=False)
    missing_months: tuple[date, ...] = ()
    trailing_incomplete_months: tuple[date, ...] = ()

    @property
    def is_usable(self) -> bool:
        return self.status is WindowStatus.USABLE

    @property
    def range(self) -> tuple[date, date] | None:
        """``(first month, last month)`` the series covers, or ``None`` when there is no window."""
        if self.start is None or self.end is None:
            return None
        return self.start, self.end

    @property
    def months(self) -> tuple[date, ...]:
        if self.start is None or self.end is None:
            return ()
        return tuple(_month_range(self.start, self.end))

    def reason_for(self, month: date) -> str | None:
        for assessment in self.assessments:
            if assessment.month == month:
                return assessment.reason
        return None

    def describe(self) -> str:
        """One plain sentence, for logs, a feature row's routing reason and the audit."""
        if self.status is WindowStatus.NO_SOURCE_DATA:
            return "no consumption is staged"
        if self.status is WindowStatus.NO_COMPLETE_MONTH:
            return "no month of the staged consumption can be verified complete"
        span = f"{_label(self.start)}..{_label(self.end)}"
        if self.status is WindowStatus.MISSING_DATA:
            return f"window {span} has missing or incomplete months: {_labels(self.missing_months)}"
        if self.trailing_incomplete_months:
            return f"window {span}; later months are incomplete and not read: {_labels(self.trailing_incomplete_months)}"
        return f"window {span}"


def _label(month: date | None) -> str:
    return month.strftime("%Y-%m") if month else "-"


def _labels(months: Iterable[date]) -> str:
    return ", ".join(_label(month) for month in months)


def _shift(month: date, delta: int) -> date:
    """The first day of the month ``delta`` months from ``month`` (negative goes back)."""
    total = month.year * 12 + (month.month - 1) + delta
    year, index = divmod(total, 12)
    return date(year, index + 1, 1)


def _month_range(first: date, last: date) -> Iterable[date]:
    month = first
    while month <= last:
        yield month
        month = _shift(month, 1)


def assess_months(
    volumes: Mapping[date, int],
    policy: ConsumptionWindowPolicy,
    validated: frozenset[date] = frozenset(),
) -> tuple[MonthAssessment, ...]:
    """Every calendar month from the first to the last the delivery holds, each judged complete or not.

    ``volumes`` is movement lines per month; a month absent from it has none. Months are judged in order, each
    against the median of the last ``completeness_reference_months`` *complete* months before it, so a run of
    bad months at the end cannot lower the reference they are measured against.
    """
    normalised: dict[date, int] = {}
    for month, volume in volumes.items():
        key = month.replace(day=1)
        normalised[key] = normalised.get(key, 0) + int(volume)
    if not normalised:
        return ()

    complete_volumes: list[int] = []
    assessments: list[MonthAssessment] = []

    for month in _month_range(min(normalised), max(normalised)):
        volume = normalised.get(month, 0)

        if month in validated:
            assessments.append(
                MonthAssessment(month, MonthStatus.COMPLETE, volume, None,
                                "validated as complete by an operator (I7_DEMAND_VALIDATED_MONTHS)")
            )
            continue

        if volume <= 0:
            assessments.append(
                MonthAssessment(month, MonthStatus.MISSING_DATA, volume, None,
                                "no consumption rows were staged for this month")
            )
            continue

        if len(complete_volumes) < policy.completeness_min_reference_months:
            complete_volumes.append(volume)
            assessments.append(
                MonthAssessment(month, MonthStatus.COMPLETE, volume, None,
                                "opening month of the delivery: nothing before it to compare with, accepted "
                                "because it holds movements")
            )
            continue

        reference = float(median(complete_volumes[-policy.completeness_reference_months:]))
        ratio = volume / reference
        if ratio >= policy.completeness_min_ratio:
            complete_volumes.append(volume)
            assessments.append(
                MonthAssessment(month, MonthStatus.COMPLETE, volume, reference,
                                f"{volume:,} movements is {ratio:.0%} of the usual {reference:,.0f}")
            )
        else:
            assessments.append(
                MonthAssessment(month, MonthStatus.MISSING_DATA, volume, reference,
                                f"{volume:,} movements is {ratio:.0%} of the usual {reference:,.0f}; "
                                f"under the {policy.completeness_min_ratio:.0%} a complete month holds")
            )

    return tuple(assessments)


def resolve_window(
    volumes: Mapping[date, int],
    policy: ConsumptionWindowPolicy | None = None,
    validated: frozenset[date] = frozenset(),
) -> ConsumptionWindow:
    """The window for a delivery whose monthly movement volumes are ``volumes``. Pure."""
    policy = policy or ConsumptionWindowPolicy()
    assessments = assess_months(volumes, policy, validated)

    if not assessments:
        return ConsumptionWindow(WindowStatus.NO_SOURCE_DATA, None, None, policy.window_months, None, None)

    first_staged, last_staged = assessments[0].month, assessments[-1].month
    complete = [a.month for a in assessments if a.status is MonthStatus.COMPLETE]
    if not complete:
        return ConsumptionWindow(
            WindowStatus.NO_COMPLETE_MONTH, None, None, policy.window_months, first_staged, last_staged,
            assessments=assessments,
            trailing_incomplete_months=tuple(a.month for a in assessments),
        )

    end = complete[-1]
    start = max(_shift(end, -(policy.window_months - 1)), first_staged)
    missing = tuple(a.month for a in assessments if a.status is MonthStatus.MISSING_DATA and start <= a.month <= end)
    trailing = tuple(a.month for a in assessments if a.month > end)

    return ConsumptionWindow(
        WindowStatus.MISSING_DATA if missing else WindowStatus.USABLE,
        start, end, policy.window_months, first_staged, last_staged,
        assessments=assessments, missing_months=missing, trailing_incomplete_months=trailing,
    )


def load_month_volumes(session: Session, staging_run_id: int | None = None) -> dict[date, int]:
    """Movement lines per month, from the staged consumption of one staging run (or all of it)."""
    statement = select(
        StagedConsumption.period, func.coalesce(func.sum(StagedConsumption.movement_count), 0)
    ).group_by(StagedConsumption.period)
    if staging_run_id is not None:
        statement = statement.where(StagedConsumption.staging_run_id == staging_run_id)
    return {period: int(total) for period, total in session.execute(statement)}


def resolve_window_for_run(
    session: Session,
    staging_run_id: int | None = None,
    policy: ConsumptionWindowPolicy | None = None,
    validated: frozenset[date] | None = None,
) -> ConsumptionWindow:
    """:func:`resolve_window` over a staging run, with the operator's validated months from settings."""
    if validated is None:
        validated = get_settings().i7_demand_validated_month_set
    return resolve_window(load_month_volumes(session, staging_run_id), policy, validated)


@dataclass(frozen=True)
class RunWindow:
    """The window a feature run recorded, as the stages after it read it."""

    status: str
    start: date | None
    end: date | None


def latest_run_window(session: Session) -> RunWindow | None:
    """The window of the newest succeeded feature run; ``None`` when no run has succeeded.

    A run built before the window existed has ``status`` ``None`` -- its features are over the extract's own
    window, and readers fall back to that. So does a database the window migration has not reached: the table
    or the columns are missing, which is "no window recorded", not an error (the same reading
    ``reporting.data_coverage`` gives a table that is not built yet).
    """
    try:
        row = session.execute(
            select(FeatureBuildRun.window_status, FeatureBuildRun.window_start, FeatureBuildRun.window_end)
            .where(FeatureBuildRun.status == "succeeded")
            .order_by(FeatureBuildRun.id.desc())
            .limit(1)
        ).first()
    except (OperationalError, ProgrammingError):
        session.rollback()
        return None
    if row is None:
        return None
    return RunWindow(row.window_status, row.window_start, row.window_end)
