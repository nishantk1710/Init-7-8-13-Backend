"""Safety stock, reorder point and maximum stock from a material-plant's OWN consumption.

For an eligible OAR material-plant the similarity engine comes first. Only when it found no reliable
estimate is this module asked, and only when the material-plant consumed something in the window. It is a
fallback: it never replaces a similarity result, and it is never applied to a non-OAR material (those are
forecast, ``inventory.service``).

THE FORMULAS (all over the policy's window of complete calendar months, six by default)::

    ADC          = total consumption in the window / calendar days in the window
    CV           = sample standard deviation of the monthly figures / their mean   (zero months included)
    Z            = inverse normal of the service level of the plant's criticality tier
    safety days  = Z x CV x sqrt(lead-time days x 30.44)
    SS           = ceil(ADC x safety days)
    ROP          = ceil(ADC x (lead-time days + safety days))
    Max          = ceil(ROP + ADC x lead-time days)

``safety days = SS / ADC`` for the usual normal safety stock ``Z x sigma_m x sqrt(LT_months)``, written in
days of cover. Max is the order-up-to level: ``Max - ROP`` is one lead time of consumption, the quantity
bought each time. It is not the shelf peak, which sits about one lead time of demand lower after a delivery.

WHAT IT REFUSES TO DO
* It does not treat a missing month as zero. The months come from the window the feature run verified
  complete; a month nobody observed is never filled in.
* It does not push a material-plant with no consumption through the formulas. CV is undefined at a zero mean,
  and "nothing consumed" does not mean "nothing needs stocking" -- least of all for a CRITICAL or IMPACT spare.
  That is ``ZERO_CONSUMPTION``, left to the similarity result or manual review.
* It does not cap, shrink or round down a calculated value that looks large. It flags it for review.
* Its confidence grade is separate from the formulas, and the criticality tier can only make it stricter.

The service level is read from the policy's matrix by the caller and passed in, so a tier has one service
level wherever it is used. Lead time is the one inventory already uses (MARC planned delivery time).
"""

from __future__ import annotations

import calendar
import math
import statistics
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Mapping, Sequence

from app.initiatives.i7.inventory.types import (
    DAYS_PER_MONTH,
    CalculationStatus,
    LeadTimeMethod,
    LeadTimeResult,
    ServiceLevelResult,
)
from app.initiatives.i7.policy.thresholds import OwnConsumptionPolicy

ESTIMATION_METHOD = "CONSUMPTION_BASED"
SIMILARITY_METHOD = "SIMILARITY_BASED"
SAFETY_STOCK_METHOD = "consumption_based"
"""``Recommendation.safety_stock_method`` (32 characters at most)."""
MAX_STOCK_STRATEGY = "rop_plus_lead_time_demand"

HIGH_SAFETY_STOCK_FLAG = "Unusually high safety stock: review"
MANUAL_REVIEW_REASON = "Eligible – No Reliable Estimate – Manual Review Required"

#: Tiers whose service level is 95% or more are held to the stricter confidence band.
_STRICT_SERVICE_LEVEL = Decimal("0.95")

_ROUNDING_NOISE = Decimal("0.000000001")


class OwnConsumptionStatus(StrEnum):
    SUCCESS = "SUCCESS"

    NO_WINDOW = "NO_WINDOW"
    """The feature run recorded no usable demand window, or no consumption was supplied. Nothing is assumed."""

    ZERO_CONSUMPTION = "ZERO_CONSUMPTION"
    """Nothing was consumed in the window. The formulas do not apply (CV is undefined at a zero mean)."""

    LEAD_TIME_UNAVAILABLE = "LEAD_TIME_UNAVAILABLE"
    SERVICE_LEVEL_UNSET = "SERVICE_LEVEL_UNSET"


@dataclass(frozen=True)
class OwnConsumptionResult:
    """One material-plant's own-consumption estimate, with everything needed to reproduce it."""

    status: OwnConsumptionStatus
    detail: str

    window_start: date | None = None
    window_end: date | None = None
    window_months: int | None = None
    window_days: int | None = None
    active_months: int | None = None
    monthly_values: tuple[Decimal, ...] = ()

    total_consumption: Decimal | None = None
    adc: Decimal | None = None
    monthly_mean: Decimal | None = None
    monthly_std: Decimal | None = None
    cv: Decimal | None = None

    lead_time_days: Decimal | None = None
    lead_time_method: str | None = None
    lead_time_source: str | None = None
    criticality: str | None = None
    service_level: Decimal | None = None
    z_factor: Decimal | None = None

    safety_days: Decimal | None = None
    safety_stock: int | None = None
    rop: int | None = None
    max_stock: int | None = None

    high_safety_stock: bool = False
    confidence: str | None = None
    confidence_basis: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is OwnConsumptionStatus.SUCCESS

    @property
    def warning(self) -> str | None:
        return HIGH_SAFETY_STOCK_FLAG if self.high_safety_stock else None


@dataclass(frozen=True)
class OwnConsumptionOutcome:
    """What the consumption fallback decided for one eligible OAR material-plant.

    Handed to the recommendation builder; its mere presence says the fallback is switched on and the
    material-plant passed eligibility. ``result`` is ``None`` when the similarity engine already produced an
    estimate, so the fallback was not needed and was not run.
    """

    result: OwnConsumptionResult | None = None


# --- The window -------------------------------------------------------------------


def window_months_ending(last_month: date, count: int) -> list[date]:
    """``count`` calendar months (as the first day of each), oldest first, ending at ``last_month``."""
    ordinal = last_month.year * 12 + (last_month.month - 1)
    return [date((ordinal - back) // 12, (ordinal - back) % 12 + 1, 1) for back in range(count - 1, -1, -1)]


def window_bounds(months: Sequence[date]) -> tuple[date, date]:
    """First day of the first month and last day of the last month."""
    last = months[-1]
    return months[0], date(last.year, last.month, calendar.monthrange(last.year, last.month)[1])


def calendar_days(start: date, end: date) -> int:
    """Days from ``start`` to ``end`` inclusive: May to October 2026 is 184."""
    return (end - start).days + 1


# --- Rounding -----------------------------------------------------------------------


def _ceil(value: Decimal) -> int:
    """Round up to a whole unit. Float noise a hair above a whole number (17.0000000001) is not a reason to
    order a unit more, so it is trimmed before rounding."""
    trimmed = value.quantize(_ROUNDING_NOISE)
    return int(trimmed.to_integral_value(rounding=ROUND_CEILING))


# --- The estimate -------------------------------------------------------------------


def evaluate(
    *,
    monthly: Mapping[date, Decimal] | None,
    last_complete_month: date | None,
    criticality: str | None,
    lead: LeadTimeResult,
    service: ServiceLevelResult,
    policy: OwnConsumptionPolicy,
) -> OwnConsumptionResult:
    """The own-consumption estimate for one material-plant.

    ``monthly`` maps the first day of a month to the quantity consumed in it; a month with no entry is a month
    with no consumption. That is only sound because ``last_complete_month`` comes from a window the feature
    run verified complete, which is why ``None`` for either is :attr:`OwnConsumptionStatus.NO_WINDOW` and not
    zero consumption.
    """
    if monthly is None or last_complete_month is None:
        return OwnConsumptionResult(
            status=OwnConsumptionStatus.NO_WINDOW,
            detail="no verified consumption window, so recent consumption cannot be measured",
            criticality=criticality,
        )

    months = window_months_ending(last_complete_month, policy.window_months)
    start, end = window_bounds(months)
    days = calendar_days(start, end)
    values = tuple(Decimal(str(monthly.get(month, 0))) for month in months)
    total = sum(values, Decimal(0))
    active = sum(1 for value in values if value > 0)

    window = dict(
        window_start=start,
        window_end=end,
        window_months=len(months),
        window_days=days,
        active_months=active,
        monthly_values=values,
        total_consumption=total,
        criticality=criticality,
    )

    if total == 0:
        return OwnConsumptionResult(
            status=OwnConsumptionStatus.ZERO_CONSUMPTION,
            detail=f"no consumption in the {len(months)} complete months to {end:%d %b %Y}; "
            "no demand-based value is created for it",
            **window,
        )
    if not lead.is_available or lead.lt_avg_days is None or lead.lt_avg_days <= 0:
        return OwnConsumptionResult(
            status=OwnConsumptionStatus.LEAD_TIME_UNAVAILABLE,
            detail=lead.detail or "no usable lead time",
            **window,
        )
    if service.status is not CalculationStatus.SUCCESS or service.z_factor is None:
        return OwnConsumptionResult(
            status=OwnConsumptionStatus.SERVICE_LEVEL_UNSET,
            detail=service.detail or "no service level for this criticality tier",
            **window,
        )

    lead_days = Decimal(lead.lt_avg_days)
    mean = total / Decimal(len(months))
    std = Decimal(str(statistics.stdev(float(value) for value in values)))
    cv = std / mean
    adc = total / Decimal(days)
    z = Decimal(service.z_factor)

    safety_days = z * cv * Decimal(str(math.sqrt(float(lead_days * DAYS_PER_MONTH))))
    safety_stock = _ceil(adc * safety_days)
    rop = _ceil(adc * (lead_days + safety_days))
    max_stock = _ceil(Decimal(rop) + adc * lead_days)

    lead_time_demand = adc * lead_days
    high = safety_days > Decimal(str(policy.high_safety_days)) or (
        Decimal(safety_stock) > Decimal(str(policy.high_safety_stock_lead_time_multiple)) * lead_time_demand
    )

    method = lead.method.value if lead.method else "UNKNOWN"
    source = method + " (MARC planned delivery time)"
    grade, basis = grade_confidence(
        active_months=active,
        window_months=len(months),
        cv=cv,
        lead_method=lead.method,
        service_level=service.service_level,
        high_safety_stock=high,
        policy=policy,
    )

    return OwnConsumptionResult(
        status=OwnConsumptionStatus.SUCCESS,
        detail="calculated from the material-plant's own consumption",
        monthly_mean=mean,
        monthly_std=std,
        cv=cv,
        adc=adc,
        lead_time_days=lead_days,
        lead_time_method=method,
        lead_time_source=source,
        service_level=service.service_level,
        z_factor=z,
        safety_days=safety_days,
        safety_stock=safety_stock,
        rop=rop,
        max_stock=max_stock,
        high_safety_stock=high,
        confidence=grade,
        confidence_basis=basis,
        **window,
    )


# --- Confidence ---------------------------------------------------------------------


def grade_confidence(
    *,
    active_months: int,
    window_months: int,
    cv: Decimal,
    lead_method: LeadTimeMethod | None,
    service_level: Decimal | None,
    high_safety_stock: bool,
    policy: OwnConsumptionPolicy,
) -> tuple[str, str]:
    """``(grade, explanation)``. Weakest link: one weak input makes it LOW, HIGH needs every input strong.

    Kept apart from the formulas: nothing here changes a number. The criticality tier never raises the
    grade -- a tier at a 95%+ service level is held to the stricter consistency band instead.
    """
    reasons: list[str] = []
    levels: list[int] = []  # 2 strong, 1 fair, 0 weak

    # History: every month of the window active is strong; enough months (policy.confident_active_months) is
    # fair; fewer is weak, which grades the estimate LOW. Thin history does not stop an estimate -- it is what
    # the LOW grade, and the review flag when the safety stock comes out large, are for.
    if active_months >= window_months:
        levels.append(2)
        reasons.append(f"consumption in all {window_months} months (strong)")
    elif active_months >= policy.confident_active_months:
        levels.append(1)
        reasons.append(f"consumption in {active_months} of {window_months} months (fair)")
    else:
        levels.append(0)
        reasons.append(
            f"consumption in only {active_months} of {window_months} months (weak: "
            f"fewer than {policy.confident_active_months})"
        )

    # Consistency of demand.
    strict = service_level is not None and service_level >= _STRICT_SERVICE_LEVEL
    cv_value = float(cv)
    if cv_value <= policy.high_confidence_maximum_cv:
        levels.append(2)
        reasons.append(f"demand variability CV {cv_value:.2f} (strong)")
    elif cv_value <= policy.medium_confidence_maximum_cv and not strict:
        levels.append(1)
        reasons.append(f"demand variability CV {cv_value:.2f} (fair)")
    else:
        levels.append(0)
        reasons.append(
            f"demand variability CV {cv_value:.2f} (weak"
            + (", a tier at a 95%+ service level needs the stricter band" if strict and cv_value <= policy.medium_confidence_maximum_cv else "")
            + ")"
        )

    # Lead time: an observed lead time is strong; the planned delivery time, which is what the system uses
    # today, is a plan and not an observation.
    if lead_method in (LeadTimeMethod.ACTUAL_STATISTICAL,):
        levels.append(2)
        reasons.append("lead time observed from purchase orders (strong)")
    else:
        levels.append(1)
        reasons.append("lead time is the planned delivery time, not observed (fair)")

    if high_safety_stock:
        levels.append(0)
        reasons.append("unusually high safety stock (weak)")

    weakest = min(levels)
    grade = {2: "HIGH", 1: "MEDIUM", 0: "LOW"}[weakest]
    return grade, "; ".join(reasons)


# --- What the recommendation records -----------------------------------------------------


def trace_entries(result: OwnConsumptionResult) -> list[tuple[str, str]]:
    """Ordered label/value pairs for ``Recommendation.calculation_trace``."""
    entries: list[tuple[str, str]] = [("estimation_method", ESTIMATION_METHOD)]
    if result.window_start:
        entries.append(("consumption_window", f"{result.window_start.isoformat()}..{result.window_end.isoformat()}"))
        entries.append(("window_days", str(result.window_days)))
        entries.append(("active_months", f"{result.active_months}/{result.window_months}"))
    for label, value in (
        ("total_consumption", result.total_consumption),
        ("adc", result.adc),
        ("monthly_mean", result.monthly_mean),
        ("monthly_std", result.monthly_std),
        ("cv", result.cv),
        ("lead_time_days", result.lead_time_days),
        ("lead_time_source", result.lead_time_source),
        ("criticality", result.criticality),
        ("service_level", result.service_level),
        ("z_factor", result.z_factor),
        ("safety_days", result.safety_days),
        ("safety_stock", result.safety_stock),
        ("rop", result.rop),
        ("max_stock", result.max_stock),
    ):
        if value is not None:
            entries.append((label, _plain(value)))
    if result.high_safety_stock:
        entries.append(("high_safety_stock_warning", HIGH_SAFETY_STOCK_FLAG))
    if result.confidence:
        entries.append(("confidence", result.confidence))
        entries.append(("confidence_basis", result.confidence_basis or ""))
    return entries


def _plain(value: object) -> str:
    """Decimals shown to a readable number of places; everything else as it is."""
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.0001")))
    return str(value)
