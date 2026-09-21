"""Unit tests for the reusable ``compare_metric`` comparison function.

Real Postgres or skip (matches ``tests/i7/test_policy_persistence.py`` and
``tests/i7/api/test_i7_api.py`` -- SQLite would accept types/DDL Postgres
rejects). Synthetic rows only, isolated by a unique ``recommendation_id``
prefix and deleted in fixture teardown, so this proves the counting logic in
isolation rather than depending on whatever the live extract happens to
contain.
"""

from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.reporting.baseline_comparison import compare_metric
from app.models.i7_recommendation import Recommendation
from app.schemas.i7.reports import AvailabilityStatus

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

PREFIX = "TEST-BASELINE-COMPARISON-"


def _row(suffix: str, *, current_ss=None, recommended_ss=None, status="READY_FOR_REVIEW"):
    return Recommendation(
        recommendation_id=f"{PREFIX}{suffix}",
        sap_material_number=f"MAT-{suffix}",
        sap_plant_code="1300",
        policy_id="test-policy",
        policy_version=1,
        formula_version="v1",
        current_safety_stock=current_ss,
        recommended_safety_stock=recommended_ss,
        impact_status="NO_CHANGE",
        status=status,
    )


@pytest.fixture
def session():
    factory = get_sessionmaker()
    with factory() as session:
        yield session
        session.execute(
            delete(Recommendation).where(Recommendation.recommendation_id.like(f"{PREFIX}%"))
        )
        session.commit()


@needs_db
def test_compare_metric_counts_both_available_missing_and_not_evaluable(session):
    """Four synthetic rows: one both-available, one baseline-missing, one
    recommendation-missing, one NOT_EVALUABLE (also recommendation-missing) --
    proving each bucket is counted independently, not collapsed."""
    rows = [
        _row("both", current_ss=Decimal("10"), recommended_ss=Decimal("15")),
        _row("baseline-missing", current_ss=None, recommended_ss=Decimal("20")),
        _row("recommendation-missing", current_ss=Decimal("30"), recommended_ss=None),
        _row(
            "not-evaluable",
            current_ss=None,
            recommended_ss=None,
            status="NOT_EVALUABLE",
        ),
    ]
    for r in rows:
        session.add(r)
    session.flush()

    base = select(Recommendation).where(Recommendation.recommendation_id.like(f"{PREFIX}%"))
    result = compare_metric(
        session,
        base,
        metric="Safety Stock",
        baseline_column=Recommendation.current_safety_stock,
        recommendation_column=Recommendation.recommended_safety_stock,
    )

    assert result.metric == "Safety Stock"
    assert result.both_available_count == 1
    assert result.baseline_missing_count == 2  # baseline-missing + not-evaluable
    assert result.recommendation_missing_count == 2  # recommendation-missing + not-evaluable
    assert result.not_evaluable_count == 1
    assert result.availability_status == AvailabilityStatus.AVAILABLE

    # Only the one both-available row (10 -> 15) feeds delta/delta_percentage.
    assert result.delta == Decimal("5")
    assert result.delta_percentage == Decimal("50")


@needs_db
def test_compare_metric_reports_not_available_when_nothing_is_both_populated(session):
    """No row has both sides populated -- the honest Safety Stock case in
    production today. The row must still be returned (never omitted), with
    both_available_count=0 and NOT_AVAILABLE, and delta/delta_percentage None
    rather than fabricated or defaulted to 0."""
    rows = [
        _row("only-baseline", current_ss=Decimal("10"), recommended_ss=None),
        _row("only-recommendation", current_ss=None, recommended_ss=Decimal("20")),
    ]
    for r in rows:
        session.add(r)
    session.flush()

    base = select(Recommendation).where(Recommendation.recommendation_id.like(f"{PREFIX}%"))
    result = compare_metric(
        session,
        base,
        metric="Safety Stock",
        baseline_column=Recommendation.current_safety_stock,
        recommendation_column=Recommendation.recommended_safety_stock,
    )

    assert result.both_available_count == 0
    assert result.delta is None
    assert result.delta_percentage is None
    assert result.availability_status == AvailabilityStatus.NOT_AVAILABLE
    # Aggregates over whichever side IS populated are still reported.
    assert result.baseline_value == Decimal("10")
    assert result.recommendation_value == Decimal("20")


@needs_db
def test_compare_metric_guards_divide_by_zero_on_percentage_delta(session):
    """A zero baseline value must not raise or silently fabricate a
    percentage -- the row is excluded from the percentage average, not the
    whole metric."""
    rows = [
        _row("zero-baseline", current_ss=Decimal("0"), recommended_ss=Decimal("5")),
    ]
    for r in rows:
        session.add(r)
    session.flush()

    base = select(Recommendation).where(Recommendation.recommendation_id.like(f"{PREFIX}%"))
    result = compare_metric(
        session,
        base,
        metric="Safety Stock",
        baseline_column=Recommendation.current_safety_stock,
        recommendation_column=Recommendation.recommended_safety_stock,
    )

    assert result.both_available_count == 1
    assert result.delta == Decimal("5")
    assert result.delta_percentage is None  # guarded, not a divide-by-zero error


@needs_db
def test_compare_metric_same_column_both_sides_has_zero_delta_by_construction(session):
    """Mirrors the Lead Time call site: passing the same column for baseline
    and recommendation means every populated row agrees with itself."""
    rows = [
        _row("lead-time-like", current_ss=Decimal("14"), recommended_ss=Decimal("14")),
    ]
    for r in rows:
        session.add(r)
    session.flush()

    base = select(Recommendation).where(Recommendation.recommendation_id.like(f"{PREFIX}%"))
    result = compare_metric(
        session,
        base,
        metric="Lead Time",
        baseline_column=Recommendation.current_safety_stock,
        recommendation_column=Recommendation.current_safety_stock,
    )

    assert result.both_available_count == 1
    assert result.delta == Decimal("0")
