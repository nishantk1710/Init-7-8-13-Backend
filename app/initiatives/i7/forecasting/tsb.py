"""Teunter-Syntetos-Babai -- specialised candidate for obsolescence risk.

Formula Reference, Stage 5D. When demand occurs in period ``t``::

    p_t = p_(t-1) + beta * (1 - p_(t-1))
    z_t = z_(t-1) + alpha * (d_t - z_(t-1))

and when it does not::

    p_t = p_(t-1) + beta * (0 - p_(t-1))
    z_t = z_(t-1)

    y_TSB = p_t * z_t

What distinguishes TSB from SBA is that the occurrence probability ``p`` decays
on every empty period, so a material approaching end-of-life sees its forecast
fall. SBA's interval only updates when demand arrives, so it never learns from
silence.

**TSB is a candidate, not a demand class.** It is evaluated when an approved
obsolescence rule flags a material -- and no such rule is configured. The
Formula Reference says the trigger, the initialisation and the parameter ranges
"must be validated on Vedanta history". So :func:`is_candidate` returns ``False``
until a policy supplies the rule, and the model reports
``NOT_EVALUABLE_TRIGGER_UNSET``.

``MSTAE = '01'`` is **not** the trigger. It marks a material already flagged
obsolete in SAP; the trigger the document describes identifies materials
*approaching* obsolescence, which is a different and unapproved question.
"""

from decimal import Decimal

from app.initiatives.i7.forecasting.series import PreparedSeries
from app.initiatives.i7.forecasting.types import (
    MODEL_VERSIONS,
    ForecastResult,
    ModelName,
    ModelStatus,
)

MINIMUM_OBSERVATIONS = 2

DEFAULT_ALPHA = Decimal("0.10")
DEFAULT_BETA = Decimal("0.10")
"""Starting values used only when a caller explicitly runs TSB with a configured
trigger. The Formula Reference leaves the search range to be validated on
Vedanta history, so these are not presented as approved parameters and no search
over them is performed."""


def is_candidate(obsolescence_trigger_configured: bool) -> bool:
    """Whether TSB may be evaluated for a material.

    Takes the configured flag explicitly rather than reading policy directly, so
    the gate is visible at every call site. There is no path that infers a
    trigger from material status.
    """
    return obsolescence_trigger_configured


def smooth(
    values: list[Decimal], alpha: Decimal, beta: Decimal
) -> tuple[Decimal, Decimal]:
    """Run the TSB recurrence. Returns ``(p_final, z_final)``.

    ``p`` is initialised to the observed demand frequency and ``z`` to the mean
    non-zero size -- both computed from the training window only.
    """
    non_zero = [value for value in values if value > 0]
    probability = Decimal(len(non_zero)) / Decimal(len(values))
    size = (
        sum(non_zero, Decimal(0)) / Decimal(len(non_zero)) if non_zero else Decimal(0)
    )

    for value in values:
        if value > 0:
            probability = probability + beta * (Decimal(1) - probability)
            size = size + alpha * (value - size)
        else:
            # The defining behaviour: an empty period lowers the probability of
            # occurrence while leaving the expected size untouched.
            probability = probability + beta * (Decimal(0) - probability)

    return probability, size


def forecast(
    series: PreparedSeries,
    horizon_months: int,
    *,
    obsolescence_trigger_configured: bool,
    alpha: Decimal = DEFAULT_ALPHA,
    beta: Decimal = DEFAULT_BETA,
) -> ForecastResult:
    """Forecast a demand rate, or report why TSB was not evaluated."""
    version = MODEL_VERSIONS[ModelName.TSB]

    if not is_candidate(obsolescence_trigger_configured):
        return ForecastResult(
            model=ModelName.TSB,
            model_version=version,
            status=ModelStatus.NOT_EVALUABLE_TRIGGER_UNSET,
            detail=(
                "no approved obsolescence-risk rule is configured; the trigger, "
                "initialisation and parameter range must be validated on Vedanta "
                "history before TSB is evaluated"
            ),
        )

    if series.length < MINIMUM_OBSERVATIONS:
        return ForecastResult(
            model=ModelName.TSB,
            model_version=version,
            status=ModelStatus.INSUFFICIENT_HISTORY,
            detail=f"{series.length} observations, need {MINIMUM_OBSERVATIONS}",
        )

    if series.non_zero_count == 0:
        return ForecastResult(
            model=ModelName.TSB,
            model_version=version,
            status=ModelStatus.NO_NON_ZERO_DEMAND,
            detail="no demand events in the series",
        )

    probability, size = smooth(series.values, alpha, beta)

    return ForecastResult(
        model=ModelName.TSB,
        model_version=version,
        status=ModelStatus.SUCCESS,
        rate=max(probability * size, Decimal(0)),
        unit=series.unit,
        training_start=series.periods[0],
        training_end=series.periods[-1],
        horizon_months=horizon_months,
        parameters=(
            ("alpha", str(alpha)),
            ("beta", str(beta)),
            ("p_final", str(probability)),
            ("z_final", str(size)),
        ),
    )
