"""Local reconciliation of I13's computed counts against reference reports.

No real ZMM065 / 30-Day GR Report export exists in this repository, so
``reference_count`` is optional input here (a query param, a future file
feed) rather than a second live system this build reaches into. When it's
absent the result is reported as ``REFERENCE_UNAVAILABLE`` rather than
silently treated as "within tolerance" -- runs entirely locally, no Azure
dependency.
"""

from decimal import Decimal

from app.initiatives.i13.models import ReconciliationResult


def reconcile(
    source_name: str, computed_count: int, reference_count: int | None, *, tolerance_pct: float
) -> ReconciliationResult:
    if reference_count is None:
        return ReconciliationResult(
            source_name=source_name,
            computed_count=computed_count,
            reference_count=None,
            absolute_difference=None,
            percentage_difference=None,
            within_tolerance=None,
            status="REFERENCE_UNAVAILABLE",
        )

    absolute_difference = abs(computed_count - reference_count)
    if reference_count == 0:
        percentage_difference = Decimal("100") if absolute_difference else Decimal("0")
    else:
        percentage_difference = (Decimal(absolute_difference) / Decimal(abs(reference_count))) * Decimal(100)

    within_tolerance = percentage_difference <= Decimal(str(tolerance_pct))
    return ReconciliationResult(
        source_name=source_name,
        computed_count=computed_count,
        reference_count=reference_count,
        absolute_difference=absolute_difference,
        percentage_difference=percentage_difference,
        within_tolerance=within_tolerance,
        status="RECONCILED" if within_tolerance else "OUT_OF_TOLERANCE",
    )
