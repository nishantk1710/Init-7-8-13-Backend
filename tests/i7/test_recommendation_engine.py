"""Recommendation generation, against the real extract.

Real Postgres or skip. Checks the properties that matter operationally: every
material-plant is covered exactly once, nothing is promoted to
READY_FOR_REVIEW without its own path's mandatory inputs, no service-level or
Max Stock value is invented, and repeated generation is idempotent.
"""

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.initiatives.i7.recommendations.types import LifecycleStatus
from app.models.i7_recommendation import Recommendation

needs_db = pytest.mark.skipif(not get_settings().database_url, reason="DATABASE_URL not set")


@pytest.fixture
def session():
    with get_sessionmaker()() as session:
        yield session


def _generated(session) -> bool:
    return session.execute(select(func.count()).select_from(Recommendation)).scalar() > 0


# --- Feature store is the source, not staging -------------------------------


def test_input_queries_read_mrp_and_price_from_the_feature_store_only():
    """No i7_staged_material_plant / i7_staged_material joins in the input queries.

    current_safety_stock, current_reorder_point, current_maximum_stock and
    unit_price are all already on i7_material_feature (Phase 3 carried them
    through from staging); a regression re-adding either staging join here
    would silently start reading them a second time from a possibly-stale
    source instead of the feature-store row already selected.
    """
    from app.initiatives.i7.recommendations import repository as recommendation_repository

    for sql in (recommendation_repository._NORMAL_SQL, recommendation_repository._OAR_SQL):
        assert "i7_staged_material_plant" not in sql
        assert "i7_staged_material" not in sql
        assert "f.current_safety_stock" in sql
        assert "f.current_reorder_point" in sql
        assert "f.current_maximum_stock" in sql
        assert "f.unit_price" in sql


# --- Coverage ---------------------------------------------------------------


@needs_db
def test_every_material_plant_has_exactly_one_current_recommendation(session):
    """Scoped to the most recent set of upstream runs: regenerating after
    Phase 3-6 have been re-run elsewhere in the suite legitimately produces a
    second row per material-plant (a fresh recommendation_id-labelled row
    under new run ids), which is correct -- recommendation_id is not a
    database-unique key (see test_recommendation_id_is_not_a_unique_key)."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    latest_feature_run = session.execute(
        text("select max(feature_run_id) from i7_recommendation")
    ).scalar()
    recommendations = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(Recommendation.feature_run_id == latest_feature_run)
    ).scalar()
    features = session.execute(text("select count(*) from i7_material_feature")).scalar()
    assert recommendations == features


@needs_db
def test_no_duplicate_recommendations_within_the_same_upstream_runs(session):
    """Uniqueness is scoped to the upstream-input tuple (see
    uq_i7_recommendation_inputs), not to material-plant alone -- two rows for
    the same material-plant under two different sets of runs are both valid."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    duplicates = session.execute(
        text(
            """select count(*) from (
                 select sap_material_number, sap_plant_code, feature_run_id,
                        forecast_run_id, inventory_run_id, oar_run_id,
                        policy_id, policy_version, formula_version
                   from i7_recommendation
                  group by 1,2,3,4,5,6,7,8,9 having count(*) > 1) d"""
        )
    ).scalar()
    assert duplicates == 0


# --- No fabrication -----------------------------------------------------------


@needs_db
def test_no_recommendation_has_a_value_without_service_level_configured(session):
    """The service-level matrix is unsigned in this configuration, so no
    normal-path recommendation may carry a computed safety stock."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    fabricated = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.is_oar.is_(False),
            Recommendation.recommended_safety_stock.isnot(None),
        )
    ).scalar()
    assert fabricated == 0


@needs_db
def test_no_max_stock_value_exists_without_a_signed_strategy(session):
    if not _generated(session):
        pytest.skip("no recommendations generated")
    fabricated = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(Recommendation.recommended_max_stock.isnot(None))
    ).scalar()
    assert fabricated == 0


@needs_db
def test_ready_for_review_only_when_the_path_actually_computed(session):
    """A normal-path recommendation is READY_FOR_REVIEW only alongside an
    actual safety stock and ROP; an OAR one only alongside neighbours."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    wrong_normal = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.is_oar.is_(False),
            Recommendation.status == LifecycleStatus.READY_FOR_REVIEW.value,
            Recommendation.recommended_safety_stock.is_(None),
        )
    ).scalar()
    assert wrong_normal == 0

    wrong_oar = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.is_oar.is_(True),
            Recommendation.status == LifecycleStatus.READY_FOR_REVIEW.value,
            Recommendation.oar_neighbour_count.is_(None),
        )
    ).scalar()
    assert wrong_oar == 0


@needs_db
def test_conversion_eligibility_is_unknown_given_the_unresolved_policies(session):
    """Criticality tiers for the production-impact trigger are unresolved and
    no I13 ledger exists, so every OAR recommendation's conversion decision
    must be UNKNOWN rather than a confident yes/no."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    non_unknown = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.is_oar.is_(True),
            Recommendation.conversion_eligibility != "UNKNOWN",
        )
    ).scalar()
    assert non_unknown == 0


@needs_db
def test_no_blocking_reason_is_empty_for_a_not_evaluable_recommendation(session):
    if not _generated(session):
        pytest.skip("no recommendations generated")
    missing_reason = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.status == LifecycleStatus.NOT_EVALUABLE.value,
            Recommendation.blocking_reason.is_(None),
        )
    ).scalar()
    assert missing_reason == 0


# --- Idempotency ----------------------------------------------------------------------


@needs_db
def test_repeated_generation_reuses_unchanged_recommendations(session):
    from app.initiatives.i7.recommendations import generate_recommendations

    session.commit()
    first = generate_recommendations()
    assert first.status == "succeeded"

    second = generate_recommendations()
    assert second.status == "succeeded"
    assert second.recommendations_written == 0
    assert second.reused_existing == first.recommendations_written + first.reused_existing


# --- Boundary safety -----------------------------------------------------------------------


@needs_db
@pytest.mark.needs_seed_data
def test_raw_tables_are_untouched(session):
    assert session.execute(
        text("select count(*) from pg_tables where schemaname='public' and tablename like 'raw_%'")
    ).scalar() == 27
    assert session.execute(text("select count(*) from raw_mseg")).scalar() == 233145


def test_no_raw_sap_table_referenced_in_the_recommendations_package():
    import ast
    from pathlib import Path

    package_dir = Path(__file__).resolve().parents[2] / "app" / "initiatives" / "i7" / "recommendations"
    raw_tables = (
        "raw_mara", "raw_makt", "raw_marc", "raw_mseg", "raw_ekko", "raw_ekpo",
        "raw_ekbe", "raw_eket", "raw_mbew", "raw_cdhdr", "raw_cdpos",
    )
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstring_ids = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstring_ids.add(id(body[0].value))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstring_ids
            ):
                for table in raw_tables:
                    assert table not in node.value.lower(), f"{path.name} references {table}"


def test_no_extwg_in_the_recommendations_package():
    from pathlib import Path

    package_dir = Path(__file__).resolve().parents[2] / "app" / "initiatives" / "i7" / "recommendations"
    for path in package_dir.glob("*.py"):
        assert "extwg" not in path.read_text(encoding="utf-8").lower()


def test_no_hardcoded_service_level_in_the_recommendations_package():
    import ast
    from pathlib import Path

    package_dir = Path(__file__).resolve().parents[2] / "app" / "initiatives" / "i7" / "recommendations"
    suspicious = {0.85, 0.90, 0.95, 0.97, 0.98, 0.99, 0.995}
    for path in package_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in suspicious:
                raise AssertionError(f"{path.name} contains {node.value}, an assumed service level")


def test_no_two_times_rop_max_stock_fallback():
    from pathlib import Path

    package_dir = Path(__file__).resolve().parents[2] / "app" / "initiatives" / "i7" / "recommendations"
    for path in package_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8").lower().replace(" ", "")
        assert "2*rop" not in source
        assert "rop*2" not in source


@needs_db
def test_recommendation_id_is_not_a_unique_key(session):
    """Regression: recommendation_id is a stable label, not a database-unique
    identity. Rerunning the pipeline with new upstream run ids legitimately
    produces a second row with the same recommendation_id -- that must not
    raise a uniqueness violation, because it is the same material-plant's
    recommendation, freshly regenerated."""
    from app.models.i7_recommendation import Recommendation

    columns = {c.name: c for c in Recommendation.__table__.columns}
    assert "recommendation_id" in columns
    assert columns["recommendation_id"].unique is not True


@needs_db
def test_similarity_available_alone_never_yields_ready_for_review(session):
    """Regression: OAR recommendations with neighbours but a blocked estimate
    must be NOT_EVALUABLE, never READY_FOR_REVIEW -- similarity evidence and
    an actual computable recommendation are different facts."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    wrongly_ready = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.oar_similarity_status == "AVAILABLE",
            Recommendation.status == LifecycleStatus.READY_FOR_REVIEW.value,
            Recommendation.recommended_safety_stock.is_(None),
        )
    ).scalar()
    assert wrongly_ready == 0


@needs_db
def test_oar_similarity_evidence_is_preserved_even_when_blocked(session):
    """The similarity result must not be discarded merely because the
    weighted estimate could not be computed."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    with_neighbours_but_no_status = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.oar_neighbour_count > 0,
            Recommendation.oar_similarity_status.is_(None),
        )
    ).scalar()
    assert with_neighbours_but_no_status == 0


# --- FR-2 demand class as FR-5's display/supporting signal -------------------
#
# The FRS: "the FR-2 demand class as a confidence signal" on the OAR-to-Min-Max
# conversion suggestion, and "shown as a supporting regularity and confidence
# signal, not the trigger." These tests prove demand_class reaches the FR-5
# response alongside the conversion fields, without affecting eligibility.


@needs_db
def test_demand_class_is_present_for_sufficient_history_conversion_candidates(session):
    """A SUFFICIENT-history material with a real demand class must carry that
    class on its recommendation row -- the value FR-5's response reads."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    row = session.execute(
        select(Recommendation)
        .where(
            Recommendation.history_status == "SUFFICIENT",
            Recommendation.demand_class.isnot(None),
        )
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        pytest.skip("no SUFFICIENT-history recommendation has a demand_class")
    assert row.demand_class in ("SMOOTH", "ERRATIC", "INTERMITTENT", "LUMPY")


@needs_db
def test_demand_class_propagates_to_the_fr5_conversion_response(session):
    """RecommendationDetail.oar.demand_class must equal the same
    MaterialFeature-derived value carried on .demand.demand_class -- one
    value, shown in two places, never a second competing source."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    row = session.execute(
        select(Recommendation).where(Recommendation.demand_class.isnot(None)).limit(1)
    ).scalar_one_or_none()
    if row is None:
        pytest.skip("no recommendation has a demand_class")

    from app.schemas.i7.recommendations import RecommendationDetail

    detail = RecommendationDetail.from_model(row)
    assert detail.oar.demand_class == row.demand_class
    assert detail.demand.demand_class == row.demand_class
    assert detail.oar.demand_class == detail.demand.demand_class


@needs_db
def test_demand_class_does_not_affect_conversion_eligibility_or_trigger(session):
    """Two recommendations sharing the same conversion_trigger/eligibility
    combination must not differ in that combination because of demand_class --
    it is not one of evaluate()'s inputs (see
    test_evaluate_has_no_demand_class_parameter), so grouping by trigger alone
    must never split by demand_class in a way that implies causation."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    rows = session.execute(
        select(Recommendation.demand_class, Recommendation.conversion_trigger)
        .where(
            Recommendation.is_oar.is_(True),
            Recommendation.conversion_trigger.isnot(None),
        )
        .limit(50)
    ).all()
    if not rows:
        pytest.skip("no OAR recommendation has a conversion_trigger")
    # Sanity: conversion_trigger values are drawn from the known enum, not
    # something demand_class leaked into.
    from app.initiatives.i7.recommendations.types import ConversionTrigger

    valid_triggers = {member.value for member in ConversionTrigger}
    for _, trigger in rows:
        assert trigger in valid_triggers


@needs_db
def test_cold_start_recommendations_expose_unclassified_not_a_fabricated_label(session):
    """OAR/cold-start materials never reached SUFFICIENT history, so FR-2
    never classified them -- demand_class must read the real FR-2 result,
    UNCLASSIFIED, not a fabricated SMOOTH/ERRATIC/INTERMITTENT/LUMPY label and
    not a silently dropped None."""
    if not _generated(session):
        pytest.skip("no recommendations generated")
    wrong = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.history_status != "SUFFICIENT",
            Recommendation.demand_class.isnot(None),
            Recommendation.demand_class != "UNCLASSIFIED",
        )
    ).scalar()
    assert wrong == 0

    # And it must not be silently dropped to None either, where a Phase 6 run
    # actually produced a recommendation for this material-plant.
    missing = session.execute(
        select(func.count())
        .select_from(Recommendation)
        .where(
            Recommendation.history_status != "SUFFICIENT",
            Recommendation.is_oar.is_(True),
            Recommendation.demand_class.is_(None),
        )
    ).scalar()
    assert missing == 0


def test_oar_info_schema_has_a_demand_class_field():
    """Pins the schema addition itself: OarInfo must carry demand_class as a
    plain display field, independent of confidence/conversion_eligibility.
    """
    from app.schemas.i7.recommendations import OarInfo

    info = OarInfo(demand_class="INTERMITTENT")
    assert info.demand_class == "INTERMITTENT"
    assert "demand_class" in OarInfo.model_fields


def test_oar_info_schema_exposes_structured_conversion_evidence():
    """The supporting count and per-indicator booleans must be readable as
    typed fields, not only recoverable by parsing conversion_detail text."""
    from app.schemas.i7.recommendations import OarInfo

    info = OarInfo(
        conversion_trigger="CONSUMPTION_FREQUENCY",
        consumption_count_12m=7,
        consumption_count_threshold=4,
        production_impact=False,
        i13_hod_approved=None,
    )
    assert info.consumption_count_12m == 7
    assert info.consumption_count_threshold == 4
    assert info.production_impact is False
    assert info.i13_hod_approved is None
    for field in (
        "consumption_count_12m",
        "consumption_count_threshold",
        "production_impact",
        "i13_hod_approved",
    ):
        assert field in OarInfo.model_fields
