"""Target-demand sanity check: is a proposed OAR ROP plausible against the target's own consumption?

The estimate borrows its numbers from donors, so nothing in it has looked at the one thing the target itself
can say: how much of it has actually been consumed. This module is that independent look. It is a DIAGNOSTIC:

* it is **not evidence of identity** -- a material can consume little and still be the right neighbour of a
  filter, and a high ROP is not a sign of a wrong match (``oar.identity`` decides that);
* it **never rejects** a recommendation, changes its status or alters a number -- a ROP above annual
  consumption is routine for a critical spare held against a long lead time;
* it is kept apart from similarity scoring and donor eligibility, and does not touch the approved OAR
  conversion usage rule (``recommendations.conversion``).

THE WINDOW

The observation window is the ``DemandSanityPolicy.window_months`` (12) months ending at the latest month
the SOURCE DATA holds -- the extract's last staged month, the same reference ``consumption_count_12m`` uses --
never the system date. The date a run happens to execute on says nothing about which months were delivered.
A target with fewer observed months than the window has an *incomplete* window.

WHAT IS COMPARED

With ``mean`` and ``sigma`` the monthly demand over the window (zeros included) and ``LT`` the target's lead
time in months::

    requirement = mean x LT  +  z x sigma x sqrt(LT)        z = DemandSanityPolicy.service_z

the demand over the lead time plus a buffer for how variable it is. A proposed ROP above
``high_multiple`` times that requirement is ``HIGH_VS_DEMAND``. Annual consumption (``mean x 12``) and the
ratio of ROP to it are recorded for the reader, but they decide nothing.

MISSING IS NOT ZERO

========================  ==========================================================================
``INSUFFICIENT_DATA_*``   an input is missing or the window is incomplete: no judgement is made and no
                          value is invented. No history at all is NOT zero consumption.
``ZERO_CONSUMPTION``      a COMPLETE window in which nothing was consumed. A genuine finding; flagged
                          when a ROP above zero is proposed for it.
========================  ==========================================================================
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.initiatives.i7.policy.thresholds import DemandSanityPolicy

DAYS_PER_MONTH = Decimal("30.4375")
"""365.25 / 12."""

_PLACES = Decimal("0.01")


class DemandCheckStatus(StrEnum):
    PLAUSIBLE = "PLAUSIBLE"
    HIGH_VS_DEMAND = "HIGH_VS_DEMAND"
    ZERO_CONSUMPTION = "ZERO_CONSUMPTION"
    INSUFFICIENT_DATA_NO_HISTORY = "INSUFFICIENT_DATA_NO_HISTORY"
    INSUFFICIENT_DATA_INCOMPLETE_WINDOW = "INSUFFICIENT_DATA_INCOMPLETE_WINDOW"
    INSUFFICIENT_DATA_NO_LEAD_TIME = "INSUFFICIENT_DATA_NO_LEAD_TIME"
    INSUFFICIENT_DATA_NO_VARIABILITY = "INSUFFICIENT_DATA_NO_VARIABILITY"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    """No proposed ROP to check, or the check is switched off."""


@dataclass(frozen=True)
class DemandInputs:
    """The target's own recorded consumption, as the feature store holds it."""

    observed_months: int | None
    """Months of history in the series, zeros included (``total_periods``). ``None``/0: no history."""
    total_demand: Decimal | None
    mean_monthly: Decimal | None
    """Mean monthly demand over the observed months, zeros included (``mean_demand_all_periods``)."""
    std_monthly: Decimal | None
    lead_time_days: Decimal | None
    """The target's resolved lead time, or its planned delivery time as the fallback."""
    window_end: date | None = None
    """The latest month the source data holds. Reported, never defaulted to today."""

    @classmethod
    def from_row(cls, row: Any) -> "DemandInputs":
        """From a recommendation-input row; any column the row lacks is simply unknown."""

        def number(name: str) -> Decimal | None:
            value = getattr(row, name, None)
            return None if value is None else Decimal(str(value))

        lead_time = number("lead_time_days")
        if lead_time is None:
            lead_time = number("planned_delivery_time_days")
        observed = getattr(row, "total_periods", None)
        return cls(
            observed_months=None if observed is None else int(observed),
            total_demand=number("total_demand"),
            mean_monthly=number("mean_demand_all_periods"),
            std_monthly=number("std_dev_demand_all_periods"),
            lead_time_days=lead_time,
            window_end=getattr(row, "window_end", None),
        )


@dataclass(frozen=True)
class DemandCheck:
    status: DemandCheckStatus
    flagged: bool
    """True only for ``HIGH_VS_DEMAND`` and for ``ZERO_CONSUMPTION`` with a ROP above zero."""
    detail: str
    proposed_rop: int | None = None
    observed_months: int | None = None
    window_months: int | None = None
    window_end: date | None = None
    annual_consumption: Decimal | None = None
    lead_time_days: Decimal | None = None
    requirement: Decimal | None = None
    """``mean x LT + z x sigma x sqrt(LT)`` -- what the lead time plausibly calls for."""
    rop_to_requirement: Decimal | None = None
    rop_to_annual: Decimal | None = None
    """Informational: a ROP above one year's consumption is not, by itself, a flag."""

    @property
    def insufficient(self) -> bool:
        return self.status.value.startswith("INSUFFICIENT_DATA")

    def trace_entries(self) -> tuple[tuple[str, str], ...]:
        """``(key, value)`` pairs for the recommendation's calculation trace."""
        entries: list[tuple[str, str]] = [
            ("demand_check_status", self.status.value),
            ("demand_check_flag", "true" if self.flagged else "false"),
            ("demand_check_detail", self.detail),
        ]
        for key, value in (
            ("demand_check_window_end", self.window_end),
            ("demand_check_annual_consumption", self.annual_consumption),
            ("demand_check_requirement", self.requirement),
            ("demand_check_rop_to_requirement", self.rop_to_requirement),
        ):
            if value is not None:
                entries.append(
                    (key, value.isoformat() if isinstance(value, date) else str(value.quantize(_PLACES)))
                )
        return tuple(entries)


def evaluate(
    inputs: DemandInputs, proposed_rop: int | None, policy: DemandSanityPolicy | None = None
) -> DemandCheck:
    """Whether ``proposed_rop`` is plausible against ``inputs``. See the module docstring."""
    policy = policy or DemandSanityPolicy()
    window = policy.window_months

    def result(status: DemandCheckStatus, detail: str, flagged: bool = False, **extra: Any) -> DemandCheck:
        return DemandCheck(
            status=status,
            flagged=flagged,
            detail=detail,
            proposed_rop=proposed_rop,
            observed_months=inputs.observed_months,
            window_months=window,
            window_end=inputs.window_end,
            **extra,
        )

    if not policy.enabled or proposed_rop is None:
        return result(
            DemandCheckStatus.NOT_APPLICABLE,
            "demand check switched off" if not policy.enabled else "no proposed ROP to check",
        )

    if not inputs.observed_months or inputs.total_demand is None or inputs.mean_monthly is None:
        return result(
            DemandCheckStatus.INSUFFICIENT_DATA_NO_HISTORY,
            "no consumption history recorded for the target -- this is missing data, not zero consumption",
        )

    if inputs.observed_months < window:
        return result(
            DemandCheckStatus.INSUFFICIENT_DATA_INCOMPLETE_WINDOW,
            f"{inputs.observed_months} month(s) of history, fewer than the {window}-month window -- "
            "consumption over the window is unknown",
        )

    annual = inputs.mean_monthly * 12

    if inputs.total_demand == 0:
        flagged = proposed_rop > 0
        return result(
            DemandCheckStatus.ZERO_CONSUMPTION,
            f"no consumption in a complete {inputs.observed_months}-month window"
            + (f"; a ROP of {proposed_rop} is proposed" if flagged else ""),
            flagged=flagged,
            annual_consumption=annual,
        )

    if inputs.lead_time_days is None or inputs.lead_time_days <= 0:
        return result(
            DemandCheckStatus.INSUFFICIENT_DATA_NO_LEAD_TIME,
            "the target has no lead time, so demand over the lead time cannot be judged",
            annual_consumption=annual,
        )

    if inputs.std_monthly is None:
        return result(
            DemandCheckStatus.INSUFFICIENT_DATA_NO_VARIABILITY,
            "the target's demand variability is unavailable",
            annual_consumption=annual,
            lead_time_days=inputs.lead_time_days,
        )

    lead_months = inputs.lead_time_days / DAYS_PER_MONTH
    requirement = (
        inputs.mean_monthly * lead_months
        + Decimal(str(policy.service_z)) * inputs.std_monthly * lead_months.sqrt()
    )
    common = dict(
        annual_consumption=annual,
        lead_time_days=inputs.lead_time_days,
        requirement=requirement,
        rop_to_annual=(Decimal(proposed_rop) / annual) if annual > 0 else None,
    )
    ratio = Decimal(proposed_rop) / requirement if requirement > 0 else None
    limit = Decimal(str(policy.high_multiple)) * requirement
    if requirement > 0 and Decimal(proposed_rop) > limit:
        return result(
            DemandCheckStatus.HIGH_VS_DEMAND,
            f"ROP {proposed_rop} is {ratio:.1f}x the {requirement:.1f} the target's own demand calls for "
            f"over a {inputs.lead_time_days:.0f}-day lead time (flagged above {policy.high_multiple:g}x)",
            flagged=True,
            rop_to_requirement=ratio,
            **common,
        )
    return result(
        DemandCheckStatus.PLAUSIBLE,
        f"ROP {proposed_rop} is within {policy.high_multiple:g}x of the {requirement:.1f} the target's own "
        "demand calls for over its lead time",
        rop_to_requirement=ratio,
        **common,
    )
