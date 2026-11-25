"""Consumption metrics and the calculation anchor for the Min-Max -> OAR recommendation.

Pure functions: no database, no clock, no settings. A caller hands in the staged monthly
movements and the anchor, and gets numbers back; the same inputs always give the same numbers.

DEFINITIONS (all approved)

* **Valid movement** -- a month whose net issued quantity (issues minus reversals) is above zero.
  A month with no row, or whose reversals cancel its issues, is not a movement and is never counted
  as evidence of consumption history.
* **History span** -- ``((last_year - first_year) * 12) + (last_month - first_month) + 1``, over the
  first and the latest valid movement month. Both end months count; months in between that have no
  consumption still count towards the span (it is a distance, not a tally). This is NOT the feature
  store's ``history_months``, which is the window length for anything that ever moved.
* **Latest window** -- the ``window_months`` (12) months ending at the anchor, anchor included.
  **Prior window** -- the 12 months before that.
* **Net issues** -- ``issue_count - reversal_count`` summed over a window and floored at zero. This
  is computed here from staged consumption, not read from the stored ``consumption_count_12m``,
  which is measured to the build's window end and so can include a partial month.
* **Anchor** -- the latest *complete* month. Movements after it are ignored entirely: a partial
  month is excluded, not treated as a complete month with low consumption.
"""

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from app.initiatives.i7.policy.reverse_conversion import ReverseConversionPolicy
from app.initiatives.i7.reverse_conversion.errors import AnchorError
from app.initiatives.i7.reverse_conversion.types import (
    AnchorResolution,
    ConsumptionMetrics,
    MonthlyMovement,
)

_ZERO = Decimal(0)


# --- Calendar helpers ---------------------------------------------------------------


def first_of_month(value: date) -> date:
    """The first day of ``value``'s month."""
    return date(value.year, value.month, 1)


def add_months(value: date, months: int) -> date:
    """The first day of the month ``months`` months after ``value``'s month (negative goes back)."""
    index = value.year * 12 + (value.month - 1) + months
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def months_between(earlier: date, later: date) -> int:
    """Whole months from ``earlier``'s month to ``later``'s month: 0 within one month, 1 for adjacent."""
    return (later.year - earlier.year) * 12 + (later.month - earlier.month)


def month_end(value: date) -> date:
    """The last day of ``value``'s month."""
    return add_months(value, 1) - timedelta(days=1)


def history_span_months(first_valid: date | None, last_valid: date | None) -> int | None:
    """The approved inclusive span; None when either end is missing (no valid movement at all).

    Dec 2024 -> Nov 2025 is 12; Jan 2025 -> Nov 2025 is 11; Dec 2024 -> Nov 2026 is 24; Jun 2025 ->
    Nov 2025 is 6. One movement month on its own is 1.
    """
    if first_valid is None or last_valid is None:
        return None
    return (last_valid.year - first_valid.year) * 12 + (last_valid.month - first_valid.month) + 1


# --- Consumption metrics -------------------------------------------------------------


def compute_consumption_metrics(
    movements: Iterable[MonthlyMovement],
    anchor: date,
    policy: ReverseConversionPolicy,
) -> ConsumptionMetrics:
    """Measure one material-plant's consumption up to ``anchor``.

    ``movements`` may hold several rows for one month (they are added together) and rows after the
    anchor (they are ignored). An empty iterable is a material that never moved.
    """
    anchor = first_of_month(anchor)

    # One figure per month first: a month is valid or not on its total, never row by row.
    quantity: dict[date, Decimal] = defaultdict(lambda: _ZERO)
    issues: dict[date, int] = defaultdict(int)
    for movement in movements:
        period = first_of_month(movement.period)
        if period > anchor:
            continue
        quantity[period] += movement.net_issued_quantity
        issues[period] += movement.issue_count - movement.reversal_count

    valid_months = sorted(period for period, total in quantity.items() if total > _ZERO)

    window = policy.window_months
    latest_start = add_months(anchor, -(window - 1))
    prior_start = add_months(anchor, -(2 * window - 1))
    prior_end = add_months(anchor, -window)

    def in_latest(period: date) -> bool:
        return latest_start <= period <= anchor

    def in_prior(period: date) -> bool:
        return prior_start <= period <= prior_end

    # Net issue transactions per window, floored at zero as a whole: a window whose reversals exceed
    # its issues has no issues, not a negative number of them.
    issues_latest = max(sum(count for period, count in issues.items() if in_latest(period)), 0)
    issues_prior = max(sum(count for period, count in issues.items() if in_prior(period)), 0)

    first_valid = valid_months[0] if valid_months else None
    last_valid = valid_months[-1] if valid_months else None

    return ConsumptionMetrics(
        anchor=anchor,
        issues_latest_window=issues_latest,
        months_with_use_latest_window=sum(1 for period in valid_months if in_latest(period)),
        issues_prior_window=issues_prior,
        months_with_use_prior_window=sum(1 for period in valid_months if in_prior(period)),
        first_valid_month=first_valid,
        last_valid_month=last_valid,
        lifetime_months_with_use=len(valid_months),
        history_span_months=history_span_months(first_valid, last_valid),
        months_since_last_movement=months_between(last_valid, anchor) if last_valid else None,
        material_age_months=months_between(first_valid, anchor) + 1 if first_valid else None,
    )


# --- The anchor ---------------------------------------------------------------------


def _load_date(data_loaded_at: datetime) -> date:
    """The calendar date (UTC) the data was loaded."""
    if data_loaded_at.tzinfo is not None:
        data_loaded_at = data_loaded_at.astimezone(timezone.utc)
    return data_loaded_at.date()


def resolve_anchor(
    *,
    latest_staged_month: date | None,
    data_loaded_at: datetime | None,
    configured_anchor: date | None = None,
) -> AnchorResolution:
    """Fix the latest complete month, or refuse.

    A month is complete only if it had ended *before the day the data was loaded* (the load day itself
    does not count: on 30 November, November is not yet complete). That needs a trustworthy load
    timestamp, so:

    * **Configured anchor** -- an explicit override, for reproducible results. It must have staged
      data, and when the load timestamp is known it must be a month that was complete at that load;
      otherwise it is refused. With no timestamp it is accepted with a warning that completeness
      could not be checked.
    * **No configured anchor** -- derived from the load timestamp: the latest staged month if it had
      ended before the load day, otherwise the month before it. With no timestamp this raises
      :class:`AnchorError` rather than falling back to the server's clock, which says nothing about
      when the data was actually loaded.
    """
    if latest_staged_month is None:
        raise AnchorError("There is no staged consumption, so no calculation anchor can be set.")
    latest = first_of_month(latest_staged_month)
    warnings: list[str] = []

    if configured_anchor is not None:
        month = first_of_month(configured_anchor)
        if month > latest:
            raise AnchorError(
                f"The anchor {month:%Y-%m} is after the latest staged consumption month ({latest:%Y-%m})."
            )
        if data_loaded_at is not None:
            loaded_on = _load_date(data_loaded_at)
            if month_end(month) >= loaded_on:
                raise AnchorError(
                    f"The anchor {month:%Y-%m} was not a complete month when the data was loaded on "
                    f"{loaded_on:%Y-%m-%d}."
                )
        else:
            warnings.append(
                "The anchor is configured and no data-load timestamp is available, so whether "
                f"{month:%Y-%m} was a complete month could not be checked."
            )
        return AnchorResolution(month, "configured", latest, data_loaded_at, tuple(warnings))

    if data_loaded_at is None:
        raise AnchorError(
            "No reliable data-load timestamp is available, so the latest complete month cannot be "
            "determined. Configure the anchor explicitly."
        )
    loaded_on = _load_date(data_loaded_at)
    month = latest if month_end(latest) < loaded_on else add_months(latest, -1)
    if months_between(latest, first_of_month(loaded_on)) > 1:
        warnings.append(
            f"The latest staged month ({latest:%Y-%m}) is more than a month before the data load "
            f"({loaded_on:%Y-%m-%d}); the data may be stale."
        )
    return AnchorResolution(month, "data_load_timestamp", latest, data_loaded_at, tuple(warnings))
