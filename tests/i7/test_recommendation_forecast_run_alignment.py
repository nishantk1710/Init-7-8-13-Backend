"""Part 29 -- the recommendation's forecast join is pinned to the selected
inventory run's own forecast_run_id, not independently re-derived from the
latest feature run.

Part 28 found the defect against the real extract: feature_run_id had
advanced to 67 while the newest executed forecast run (id=10) was still
built against feature_run_id=56, so
``latest_forecast_run(feature_run_id=67)`` returned ``None`` and every
recommendation's ``forecast_rate`` came back NULL -- even for material-plants
whose selected inventory run (id=85) had itself correctly joined to forecast
run 10 and already produced real, non-NULL SS/ROP values. Reading
``i7_inventory_run.forecast_run_id`` instead ties the recommendation's own
forecast join to the exact run its SS/ROP came from, so the two can never
disagree.
"""

from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations import repository
from app.models.i7_recommendation import Recommendation

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


# --- Unit-level: the helper itself ------------------------------------------


@needs_db
def test_forecast_run_for_inventory_run_reads_the_pinned_value(session):
    """The helper reads i7_inventory_run.forecast_run_id verbatim -- not the
    latest forecast run, not the latest feature run's forecast run."""
    inventory_run_id = session.execute(
        text("select max(id) from i7_inventory_run where forecast_run_id is not null")
    ).scalar()
    if inventory_run_id is None:
        pytest.skip("no inventory run with a forecast_run_id on this database")

    expected = session.execute(
        text("select forecast_run_id from i7_inventory_run where id = :id"),
        {"id": inventory_run_id},
    ).scalar()

    assert repository.forecast_run_for_inventory_run(session, inventory_run_id) == expected


@needs_db
def test_forecast_run_for_inventory_run_can_disagree_with_latest_feature_run(session):
    """Reproduces the exact Part 28 scenario against the real data: the
    latest feature run has no forecast run of its own
    (latest_forecast_run(feature_run_id=latest) is None), while a specific,
    already-selected inventory run still has a real, non-None
    forecast_run_id from an earlier feature generation. This is precisely
    the case the old service.py logic got wrong -- it derived None here and
    every recommendation lost its forecast_rate.
    """
    latest_feature_run_id = repository.latest_feature_run(session)
    if latest_feature_run_id is None:
        pytest.skip("no feature run on this database")

    unscoped_for_latest_feature = repository.latest_forecast_run(session, latest_feature_run_id)

    inventory_run_id = session.execute(
        text("select max(id) from i7_inventory_run where forecast_run_id is not null")
    ).scalar()
    if inventory_run_id is None:
        pytest.skip("no inventory run with a forecast_run_id on this database")

    pinned = repository.forecast_run_for_inventory_run(session, inventory_run_id)

    assert pinned is not None
    if unscoped_for_latest_feature is None:
        # The exact drift Part 28 found: proves the old path would have
        # produced None here while the new path still resolves a real run.
        assert pinned != unscoped_for_latest_feature


# --- Integration: the three real materials from Part 28 --------------------

_TARGET_MATERIALS = ("5000092261", "5000092262", "5000092269")


@needs_db
def test_target_materials_have_a_non_null_forecast_rate_after_alignment_fix(session):
    """The exact Part 28 regression: these three material-plants' selected
    inventory run (id=85 at the time of the audit) is pinned to forecast run
    10, where SBA succeeded with forecast_rate=1.149129. Before this fix,
    the recommendation layer's independent forecast-run lookup returned None
    for these rows regardless. This proves generation now carries the real
    rate through instead.
    """
    from app.initiatives.i7.recommendations.service import generate_recommendations

    result = generate_recommendations()
    assert result.status == "succeeded"

    for material in _TARGET_MATERIALS:
        row = session.execute(
            select(Recommendation)
            .where(
                Recommendation.sap_material_number == material,
                Recommendation.sap_plant_code == "1300",
            )
            .order_by(Recommendation.id.desc())
        ).scalars().first()
        assert row is not None, f"no recommendation found for {material}/1300"

        assert row.forecast_rate is not None, (
            f"{material}/1300: forecast_rate is still NULL after the alignment fix"
        )
        assert row.forecast_rate == pytest.approx(Decimal("1.149129"), abs=Decimal("0.000001"))

        # SS/ROP must be unchanged by this fix -- it only changes which
        # forecast run the display/rate fields are joined against, never a
        # calculated value.
        assert row.recommended_safety_stock == 2
        assert row.recommended_rop == 3
        assert row.status == "READY_FOR_REVIEW"


@needs_db
def test_recommendation_forecast_run_id_matches_its_own_inventory_run(session):
    """Governance-level proof: a recommendation's own forecast_run_id must
    equal the forecast_run_id of the inventory_run it was actually built
    from -- the two can never disagree once forecast_run_id is read from the
    inventory run itself rather than re-derived independently.
    """
    rows = session.execute(
        select(Recommendation)
        .where(Recommendation.inventory_run_id.is_not(None))
        .order_by(Recommendation.id.desc())
        .limit(50)
    ).scalars().all()
    if not rows:
        pytest.skip("no recommendation with an inventory_run_id on this database")

    checked = 0
    for row in rows:
        expected = repository.forecast_run_for_inventory_run(session, row.inventory_run_id)
        assert row.forecast_run_id == expected, (
            f"{row.recommendation_id}: forecast_run_id={row.forecast_run_id} "
            f"but its own inventory_run {row.inventory_run_id} used forecast_run_id={expected}"
        )
        checked += 1
    assert checked > 0
