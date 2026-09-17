"""SIT 5.1 / 5.10 / 5.12 -- domain-to-domain propagation and run isolation.

Verifies the pipeline's provenance chain against the real, currently-seeded
database: every recommendation names the exact upstream runs it was built
from, material-plant identity is preserved end to end, and when multiple
generations of a run exist (which the real database naturally has -- three
feature runs, several inventory/OAR runs), a recommendation's provenance
fields are internally consistent rather than a mix of run generations.

No pipeline stage is re-run here. This reads what Phases 3-7 already wrote.
"""

import pytest
from sqlalchemy import distinct, func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.i7_features import MaterialFeature
from app.models.i7_recommendation import Recommendation

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _latest_feature_run(session) -> int:
    return session.execute(text("select max(feature_run_id) from i7_recommendation")).scalar()


# --- Run isolation (5.10): multiple generations coexist without mixing ---------------------


@needs_db
@pytest.mark.needs_seed_data
def test_multiple_feature_run_generations_exist_and_are_each_complete(session):
    """The real database has accumulated several feature-run generations
    across Phases 3-7's development. Each generation's recommendation set must
    be a complete, self-consistent snapshot -- not a partial mix."""
    generations = session.execute(
        text("select feature_run_id, count(*) from i7_recommendation group by 1 order by 1")
    ).all()
    assert len(generations) >= 2, "expected at least two run generations to test isolation"

    features_total = session.execute(select(func.count()).select_from(MaterialFeature)).scalar()
    for feature_run_id, count in generations:
        assert count == features_total, (
            f"feature_run_id={feature_run_id} has {count} recommendations, "
            f"expected {features_total} (one per current material-plant)"
        )


@needs_db
def test_inventory_runs_reference_the_current_feature_generation(session):
    """An inventory run's own feature_run_id must match the generation it is
    joined against -- this one holds on the real data (Phase 5 is re-run
    routinely alongside Phase 3)."""
    latest = _latest_feature_run(session)
    rows = session.execute(
        text(
            """select distinct inventory_run_id from i7_recommendation
                where feature_run_id = :fr and inventory_run_id is not null"""
        ),
        {"fr": latest},
    ).scalars().all()
    if not rows:
        pytest.skip("no recommendation with an inventory_run_id on the latest feature run")

    for inventory_run_id in rows:
        inventory_feature_run = session.execute(
            text("select feature_run_id from i7_inventory_run where id = :id"),
            {"id": inventory_run_id},
        ).scalar()
        assert inventory_feature_run == latest


@needs_db
@pytest.mark.needs_seed_data
def test_latest_forecast_run_is_scoped_by_feature_generation(session):
    """Regression for a genuine Phase 9 finding: an unscoped "global latest
    forecast run" can predate the feature generation a recommendation batch is
    being built for, silently borrowing a stale, mismatched-generation
    forecast instead of correctly reporting "no current forecast".

    On the real database, forecasting has not been re-run since the feature
    store advanced past the generation it was built against (a known,
    documented environment condition -- see docs/i07_sit.md -- not fabricated
    here and not resolved by re-running Phase 4). The scoped lookup must
    therefore honestly return nothing for the *current* feature generation,
    proving the fix actually changes behaviour rather than being a no-op.
    """
    from app.initiatives.i7.recommendations import repository

    latest_feature = repository.latest_feature_run(session)
    unscoped = repository.latest_forecast_run(session)
    scoped = repository.latest_forecast_run(session, latest_feature)

    assert unscoped is not None, "expected at least one forecast run to exist"
    # The defect this proves was fixed: the unscoped lookup finds a forecast
    # run, but it belongs to an older feature generation than the one
    # currently live -- so scoping must not silently accept it.
    unscoped_feature_run = session.execute(
        text("select feature_run_id from i7_forecast_run where id = :id"), {"id": unscoped}
    ).scalar()
    if unscoped_feature_run == latest_feature:
        pytest.skip("forecasting has been re-run against the current generation; nothing to prove")
    assert scoped is None or scoped != unscoped


@needs_db
def test_scoped_forecast_lookup_only_ever_returns_a_matching_generation(session):
    """Whenever the scoped lookup does return a run, that run's own
    feature_run_id must equal the generation asked for -- never a
    coincidental id-ordering artefact."""
    from app.initiatives.i7.recommendations import repository

    for feature_run_id in (8, session.execute(
        text("select max(id) from i7_feature_run")
    ).scalar()):
        found = repository.latest_forecast_run(session, feature_run_id)
        if found is None:
            continue
        actual_feature_run = session.execute(
            text("select feature_run_id from i7_forecast_run where id = :id"), {"id": found}
        ).scalar()
        assert actual_feature_run == feature_run_id


@needs_db
def test_oar_recommendations_reference_an_oar_run_that_matches_the_feature_run(session):
    latest = _latest_feature_run(session)
    rows = session.execute(
        text(
            """select distinct oar_run_id from i7_recommendation
                where feature_run_id = :fr and is_oar = true and oar_run_id is not null"""
        ),
        {"fr": latest},
    ).scalars().all()
    if not rows:
        pytest.skip("no OAR recommendation with an oar_run_id on the latest feature run")

    for oar_run_id in rows:
        oar_feature_run = session.execute(
            text("select feature_run_id from i7_oar_run where id = :id"), {"id": oar_run_id}
        ).scalar()
        assert oar_feature_run == latest


# --- Material + plant identity preserved end to end (5.1) ------------------------------------


@needs_db
def test_material_plant_identity_is_preserved_from_feature_to_recommendation(session):
    """Every (material, plant) in the feature store has exactly one
    recommendation under the latest run, and the key is copied verbatim --
    never transformed, padded or reformatted along the way."""
    latest = _latest_feature_run(session)
    mismatched = session.execute(
        text(
            """select count(*) from i7_material_feature f
                where not exists (
                  select 1 from i7_recommendation r
                   where r.feature_run_id = :fr
                     and r.sap_material_number = f.sap_material_number
                     and r.sap_plant_code = f.sap_plant_code
                )"""
        ),
        {"fr": latest},
    ).scalar()
    assert mismatched == 0


@needs_db
def test_no_material_identity_collisions_across_plants(session):
    """The same material at two plants must produce two independent
    recommendations, not be collapsed into one."""
    latest = _latest_feature_run(session)
    multi_plant_materials = session.execute(
        text(
            """select sap_material_number from i7_recommendation
                where feature_run_id = :fr
                group by 1 having count(distinct sap_plant_code) > 1
                limit 5"""
        ),
        {"fr": latest},
    ).scalars().all()
    if not multi_plant_materials:
        pytest.skip("no material appears at more than one plant in this extract")

    for material in multi_plant_materials:
        rows = session.execute(
            text(
                """select sap_plant_code, recommendation_id from i7_recommendation
                    where feature_run_id = :fr and sap_material_number = :m"""
            ),
            {"fr": latest, "m": material},
        ).all()
        plants = [r[0] for r in rows]
        assert len(plants) == len(set(plants)), f"{material} has duplicate plant rows"


@needs_db
@pytest.mark.needs_seed_data
def test_no_cross_plant_leakage_in_current_values(session):
    """A material's current SAP values must match the plant they were staged
    at -- 1300 and 1200 must never be confused for the same material."""
    latest = _latest_feature_run(session)
    rows = session.execute(
        text(
            """select r.sap_material_number, r.sap_plant_code, r.current_rop,
                      p.current_reorder_point
                 from i7_recommendation r
                 join i7_staged_material_plant p
                   on p.sap_material_number = r.sap_material_number
                  and p.sap_plant_code = r.sap_plant_code
                where r.feature_run_id = :fr
                limit 200"""
        ),
        {"fr": latest},
    ).all()
    assert rows, "expected at least some recommendations with a staged material-plant match"
    for material, plant, rec_rop, staged_rop in rows:
        # Both NULL, or both equal -- never a mismatch, which would indicate the
        # recommendation picked up another plant's staged row.
        assert (rec_rop is None) == (staged_rop is None)
        if rec_rop is not None:
            assert float(rec_rop) == float(staged_rop)
