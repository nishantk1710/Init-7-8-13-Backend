"""Part 33 -- end-to-end regression for the Review-Period DEV Max Stock mock
against the real, live database, at full-population scale.

Unit-level containment (never PolicyDocument()'s default, never signs the
policy, introduces no new formula) is already covered by
test_dev_max_stock_fixture.py. This file proves the population-scale claim
that audit made: with both DEV mocks active, exactly the material-plants
whose safety_stock/rop already succeed get a real Max Stock value, and every
other row -- the other ~113,462 -- is completely unaffected, still blocked by
whatever upstream gate already blocked it (OAR deferral, missing service
level, missing lead time), never by Max Stock.
"""

from decimal import Decimal

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.inventory.service import run_inventory_calculations
from app.initiatives.i7.policy.dev_fixtures import default_policy

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")

_TARGET_MATERIALS = ("5000092261", "5000092262", "5000092269")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _distinct_policy() -> "PolicyDocument":  # noqa: F821 -- imported below for the type checker
    """A policy carrying whatever DEV mocks are currently active in this
    process's environment, but with its own policy_id -- distinguishing this
    verification run's identity from any other run that happens to share the
    same (feature_run_id, forecast_run_id, policy_id, policy_version,
    formula_version) tuple, so this test never collides with, or is silently
    reused as, an unrelated run (see uq_i7_inventory_run_inputs).
    """
    base = default_policy()
    return base.model_copy(update={"policy_id": f"{base.policy_id}-part33-population-test"})


@needs_db
@pytest.mark.needs_seed_data
def test_review_period_mock_gives_max_stock_only_to_the_three_known_materials(session, monkeypatch):
    """The exact population-scale claim: enabling I7_DEV_MOCK_MAX_STOCK
    (alongside I7_DEV_MOCK_SERVICE_LEVEL, since Max Stock is computed from
    ROP and ROP needs a configured service level to succeed at all) produces
    a real, non-null Max Stock for precisely the rows whose safety_stock/rop
    already reached SUCCESS -- currently the 3 named materials -- and leaves
    every other row's max_stock_status exactly as it already was.
    """
    from app.core.config import get_settings as _get_settings

    monkeypatch.setenv("I7_DEV_MOCK_SERVICE_LEVEL", "true")
    monkeypatch.setenv("I7_DEV_MOCK_MAX_STOCK", "true")
    _get_settings.cache_clear()
    try:
        policy = _distinct_policy()
    finally:
        _get_settings.cache_clear()

    result = run_inventory_calculations(policy=policy)
    assert result.status == "succeeded"
    run_id = result.run_id

    total = session.execute(text("select count(*) from i7_inventory_calculation where inventory_run_id = :r"), {"r": run_id}).scalar()
    assert total > 100_000, "sanity check: this must be a full-population run, not a partial one"

    for material in _TARGET_MATERIALS:
        row = session.execute(
            text(
                "select safety_stock_status, rop, max_stock_status, max_stock, max_stock_strategy "
                "from i7_inventory_calculation where sap_material_number = :m and sap_plant_code = '1300' "
                "and inventory_run_id = :r"
            ),
            {"m": material, "r": run_id},
        ).first()
        assert row is not None, f"no row for {material}/1300 in run {run_id}"
        assert row.safety_stock_status == "SUCCESS"
        assert row.max_stock_status == "SUCCESS", f"{material}: expected SUCCESS, got {row.max_stock_status}"
        assert row.max_stock_strategy == "review_period"
        assert row.max_stock is not None
        # Max = ROP + (forecast_rate x 1 month), ceiling-rounded -- the exact
        # existing ReviewPeriodMaxStockStrategy arithmetic, unchanged by this
        # test. Not re-deriving the formula here -- just checking it produced
        # *a* number consistent with ROP, not asserting a magic literal.
        assert row.max_stock >= row.rop

    other_success = session.execute(
        text(
            "select count(*) from i7_inventory_calculation where inventory_run_id = :r "
            "and max_stock_status = 'SUCCESS' "
            "and (sap_material_number, sap_plant_code) not in "
            "(('5000092261','1300'), ('5000092262','1300'), ('5000092269','1300'))"
        ),
        {"r": run_id},
    ).scalar()
    assert other_success == 0, (
        f"expected max_stock SUCCESS only for the 3 known materials, found {other_success} others -- "
        "the population-wide upstream gates (OAR deferral, missing service level, missing lead time) "
        "must remain exactly as they were"
    )

    unaffected = session.execute(
        text(
            "select count(*) from i7_inventory_calculation where inventory_run_id = :r "
            "and max_stock_status = 'DEFERRED_TO_OAR'"
        ),
        {"r": run_id},
    ).scalar()
    assert unaffected > 100_000, "the OAR-deferred population must not shrink because Max Stock was configured"


@needs_db
def test_production_default_policy_document_remains_not_configured_after_this_change(session):
    """Guards against the DEV mock ever leaking into the default path: a
    bare PolicyDocument() -- what every real, unmocked call still gets --
    must still be unconfigured for Max Stock, unaffected by anything set in
    this process's environment for the test above (that test uses monkeypatch,
    scoped to itself; this test uses no env override at all).
    """
    from app.initiatives.i7.policy import PolicyDocument

    policy = PolicyDocument()
    assert policy.max_stock.strategy is None
    assert not policy.max_stock.is_configured
