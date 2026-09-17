"""The DEV chain that turns OAR's blocked statuses into actual numbers.

OAR borrows its SS/ROP/Max from neighbours, so it produces nothing until the
neighbours' own Phase 5 calculations succeed. Two unsigned business policies
block them: the Criticality -> Service Level matrix (no Z, so no safety
stock, so no ROP) and the Max Stock strategy (no Max, and
``oar/repository.load_inventory_values`` requires all three statuses to be
SUCCESS before a neighbour may lend anything at all).

These tests walk the whole chain -- dev service level -> Z -> donor SS ->
donor ROP -> dev Max Stock -> OAR weighted estimate -> recommendation -- with
the two development fixtures loaded, and then walk it again under the plain
``PolicyDocument()`` production default to show every leg still blocks there.

Nothing here weakens a gate: minimum_similarity stays 0.60, minimum_neighbours
stays 5, the weighted-average formulas are checked against hand arithmetic
rather than against themselves, and no material's real criticality is read,
written or defaulted anywhere in the path.
"""

import math
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.inventory.lead_time import PurchaseOrderInput
from app.initiatives.i7.inventory.service import calculate_one
from app.initiatives.i7.oar.service import evaluate_target
from app.initiatives.i7.oar.types import CandidateAttributes, EstimateStatus, OarStatus
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    load_mock_max_stock_policy,
    load_mock_service_level_policy,
)
from app.initiatives.i7.recommendations import builder
from app.initiatives.i7.recommendations.types import LifecycleStatus


DEV_POLICY = PolicyDocument(
    service_level=load_mock_service_level_policy(),
    max_stock=load_mock_max_stock_policy(),
)
"""Both development fixtures, opted into explicitly -- exactly what
``default_policy()`` assembles when both env flags are set."""

PRODUCTION_POLICY = PolicyDocument()
"""What every environment builds with no dev flag set."""

ORDERS = [
    PurchaseOrderInput(30, False),
    PurchaseOrderInput(35, False),
    PurchaseOrderInput(28, False),
]

DONORS = [
    ("D000001", 10, 3.0, 100),
    ("D000002", 12, 4.0, 105),
    ("D000003", 8, 2.0, 95),
    ("D000004", 15, 5.0, 110),
    ("D000005", 11, 3.5, 98),
    ("D000006", 9, 2.5, 102),
]


class NoEmbeddingModel:
    """The text dimension unavailable -- scoring renormalises over the other
    two, which is the state on any machine without the model downloaded."""

    is_available = False
    model_name = None
    model_version = "none"

    def encode(self, texts):
        return None


def donor_feature_row(material: str, mean: float, std: float) -> SimpleNamespace:
    """One Phase 3 feature-store row joined to its Phase 4 forecast, shaped as
    ``inventory/service.py``'s ``_INPUT_SQL`` produces it."""
    return SimpleNamespace(
        sap_material_number=material,
        sap_plant_code="BMM",
        demand_class="SMOOTH",
        history_status="SUFFICIENT",
        criticality="NORMAL",
        total_periods=24,
        non_zero_periods=24,
        mean_demand_all_periods=Decimal(str(mean)),
        std_dev_demand_all_periods=Decimal(str(std)),
        mean_non_zero_demand=Decimal(str(mean)),
        std_dev_non_zero_demand=Decimal(str(std)),
        planned_delivery_time_days=30,
        unit_price=Decimal("100"),
        model_name="croston",
        forecast_rate=Decimal(str(mean)),
        forecast_unit="EA",
        parameters=None,
    )


def attributes(material: str, price: float) -> CandidateAttributes:
    return CandidateAttributes(
        sap_material_number=material,
        sap_plant_code="BMM",
        criticality="NORMAL",
        is_active=True,
        history_months=24,
        material_group="PUMP",
        equipment_type=None,
        base_unit_of_measure="EA",
        circuit=None,
        manufacturer="ACME",
        unit_price=Decimal(str(price)),
        description="pump seal kit",
    )


def phase_five(policy: PolicyDocument) -> dict[str, dict]:
    """Run the real Phase 5 calculation for every donor under ``policy``."""
    return {
        material: calculate_one(donor_feature_row(material, mean, std), ORDERS, policy)
        for material, mean, std, _ in DONORS
    }


def inventory_values(calculations: dict[str, dict]) -> dict[tuple[str, str], dict]:
    """The donor values OAR may borrow, applying
    ``oar/repository.load_inventory_values``'s all-three-SUCCESS rule."""
    return {
        (material, "BMM"): {
            "safety_stock": row["safety_stock"],
            "rop": row["rop"],
            "max_stock": row["max_stock"],
        }
        for material, row in calculations.items()
        if row["safety_stock_status"] == "SUCCESS"
        and row["rop_status"] == "SUCCESS"
        and row["max_stock_status"] == "SUCCESS"
    }


def run_oar(policy: PolicyDocument, donor_values: dict):
    target = attributes("T000001", 101)
    candidates = [attributes(material, price) for material, _, _, price in DONORS]
    return evaluate_target(
        target, candidates, NoEmbeddingModel(), Decimal("20"), donor_values, policy
    )


@pytest.fixture(scope="module")
def dev_donors() -> dict[str, dict]:
    return phase_five(DEV_POLICY)


@pytest.fixture(scope="module")
def dev_result(dev_donors):
    return run_oar(DEV_POLICY, inventory_values(dev_donors))


# --- 1. The DEV NORMAL service level reaches the Phase 5 donor calculation ---


def test_dev_service_level_reaches_the_donor_calculation(dev_donors):
    """Not merely loaded into a policy object: resolved, per donor, into the
    service level and Z factor the safety-stock formula actually consumed."""
    expected = load_mock_service_level_policy().service_level_for(Criticality.NORMAL)
    assert expected == 0.85

    for row in dev_donors.values():
        assert row["service_level_status"] == "SUCCESS"
        assert row["service_level"] == Decimal(str(expected))
        assert row["z_factor"] is not None


def test_z_is_computed_by_norm_ppf_and_never_hardcoded(dev_donors):
    """The DEV policy supplies a service-level *fraction*; Z stays whatever
    ``scipy.stats.norm.ppf`` makes of it, via the existing z_factor()."""
    from scipy.stats import norm

    from app.initiatives.i7.inventory.service_level import z_factor

    expected = Decimal(str(round(float(norm.ppf(0.85)), 6)))
    assert z_factor(Decimal("0.85")) == expected
    for row in dev_donors.values():
        assert row["z_factor"] == expected


def test_donor_safety_stock_and_rop_are_numbers(dev_donors):
    for row in dev_donors.values():
        assert row["safety_stock_status"] == "SUCCESS"
        assert isinstance(row["safety_stock"], int)
        assert row["rop_status"] == "SUCCESS"
        assert isinstance(row["rop"], int)


def test_all_four_pipeline_entry_points_resolve_policy_the_same_way():
    """``run_forecasting`` and ``run_inventory_calculations`` always used
    ``default_policy()``; ``run_oar_similarity`` and
    ``generate_recommendations`` constructed a bare ``PolicyDocument()``, so a
    dev run produced Phase 5 donor values that Phase 6 then refused to borrow.
    All four must now resolve through the one mechanism."""
    import inspect

    from app.initiatives.i7.forecasting.service import run_forecasting
    from app.initiatives.i7.inventory.service import run_inventory_calculations
    from app.initiatives.i7.oar.service import run_oar_similarity
    from app.initiatives.i7.recommendations.service import generate_recommendations

    for function in (
        run_forecasting,
        run_inventory_calculations,
        run_oar_similarity,
        generate_recommendations,
    ):
        source = inspect.getsource(function)
        assert "policy or default_policy()" in source, function.__name__
        assert "policy or PolicyDocument()" not in source, function.__name__


def test_run_oar_similarity_actually_calls_default_policy(monkeypatch):
    """Functional, not merely textual: the call is observed, and the run is
    stopped at the first database access so no connection is needed."""
    from app.initiatives.i7.oar import service as oar_service
    from app.initiatives.i7.policy import dev_fixtures

    calls = []

    def spy():
        calls.append("called")
        return DEV_POLICY

    def no_database():
        raise RuntimeError("sentinel: reached the database")

    monkeypatch.setattr(dev_fixtures, "default_policy", spy)
    monkeypatch.setattr(oar_service, "get_sessionmaker", no_database)

    with pytest.raises(RuntimeError, match="sentinel"):
        oar_service.run_oar_similarity(provider=NoEmbeddingModel())

    assert calls == ["called"]


def test_generate_recommendations_actually_calls_default_policy(monkeypatch):
    from app.initiatives.i7.policy import dev_fixtures
    from app.initiatives.i7.recommendations import service as recommendation_service

    calls = []

    def spy():
        calls.append("called")
        return DEV_POLICY

    def no_database():
        raise RuntimeError("sentinel: reached the database")

    monkeypatch.setattr(dev_fixtures, "default_policy", spy)
    monkeypatch.setattr(recommendation_service, "get_sessionmaker", no_database)

    with pytest.raises(RuntimeError, match="sentinel"):
        recommendation_service.generate_recommendations()

    assert calls == ["called"]


def test_an_explicit_policy_argument_still_wins(monkeypatch):
    """``default_policy()`` is a *fallback*. A caller passing a policy -- every
    test above, and any production caller -- must still override it."""
    from app.initiatives.i7.oar import service as oar_service
    from app.initiatives.i7.policy import dev_fixtures

    def must_not_be_called():
        raise AssertionError("default_policy() overrode an explicit argument")

    def no_database():
        raise RuntimeError("sentinel: reached the database")

    monkeypatch.setattr(dev_fixtures, "default_policy", must_not_be_called)
    monkeypatch.setattr(oar_service, "get_sessionmaker", no_database)

    with pytest.raises(RuntimeError, match="sentinel"):
        oar_service.run_oar_similarity(PRODUCTION_POLICY, provider=NoEmbeddingModel())


# --- 2. No material's real criticality is touched ---------------------------


def test_material_feature_criticality_has_no_default_or_override():
    """The column itself: nothing added a default, a coercion to NORMAL, or a
    non-null constraint that would force one."""
    from app.models.i7_features import MaterialFeature

    column = MaterialFeature.__table__.columns["criticality"]
    assert column.default is None
    assert column.server_default is None
    assert column.nullable is True


def test_phase_five_still_resolves_each_material_s_own_criticality():
    """The DEV fixtures configure the *matrix*, never the key it is looked up
    by. Phase 5 must still read row.criticality, so a CRITICAL donor resolves
    at CRITICAL and not at the OAR gate's temporary NORMAL."""
    import inspect

    from app.initiatives.i7.inventory import service as inventory_service

    source = inspect.getsource(inventory_service)
    assert "_criticality(row.criticality)" in source
    assert "OAR_TEMPORARY_CRITICALITY" not in source

    critical_row = donor_feature_row("D000001", 10, 3.0)
    critical_row.criticality = "CRITICAL"
    result = calculate_one(critical_row, ORDERS, DEV_POLICY)

    assert result["criticality"] == "CRITICAL"
    assert result["service_level"] == Decimal("0.98")


def test_a_donor_with_no_criticality_still_blocks_rather_than_defaulting():
    """Criticality is the matrix key. A material without one has nothing to
    look up, and the DEV fixtures must not supply a stand-in."""
    row = donor_feature_row("D000001", 10, 3.0)
    row.criticality = None
    result = calculate_one(row, ORDERS, DEV_POLICY)

    assert result["service_level_status"] == "NOT_EVALUABLE_SERVICE_LEVEL_UNSET"
    assert result["safety_stock"] is None


# --- 3. At least five qualifying neighbours are required --------------------


def test_minimum_neighbours_is_five_and_is_a_hard_gate(dev_donors):
    assert PolicyDocument().similarity.minimum_neighbours == 5

    donor_values = inventory_values(dev_donors)
    target = attributes("T000001", 101)
    four = [attributes(material, price) for material, _, _, price in DONORS[:4]]

    result = evaluate_target(
        target, four, NoEmbeddingModel(), Decimal("20"), donor_values, DEV_POLICY
    )

    assert len(result.neighbours) == 4
    assert result.estimate.status is EstimateStatus.NOT_EVALUABLE_INSUFFICIENT_NEIGHBOURS
    # Four perfectly good neighbours, all with Phase 5 values, and still no
    # partial estimate.
    assert result.estimate.safety_stock is None
    assert result.estimate.rop is None
    assert result.estimate.max_stock is None
    assert result.estimate.qualifying_neighbours == 4
    assert result.estimate.minimum_neighbours == 5


def test_five_qualifying_neighbours_is_enough(dev_donors):
    donor_values = inventory_values(dev_donors)
    target = attributes("T000001", 101)
    five = [attributes(material, price) for material, _, _, price in DONORS[:5]]

    result = evaluate_target(
        target, five, NoEmbeddingModel(), Decimal("20"), donor_values, DEV_POLICY
    )

    assert len(result.neighbours) == 5
    assert result.estimate.status is EstimateStatus.SUCCESS


# --- 4. Similarity below 0.60 does not contribute ---------------------------


def test_minimum_similarity_is_point_six_and_excludes_weaker_candidates(dev_donors):
    assert PolicyDocument().similarity.minimum_similarity == 0.60

    donor_values = inventory_values(dev_donors)
    target = attributes("T000001", 101)

    # A candidate that shares nothing: different group, unit, manufacturer,
    # description and a price at the far end of the range.
    distant = CandidateAttributes(
        sap_material_number="D000099",
        sap_plant_code="BMM",
        criticality="NORMAL",
        is_active=True,
        history_months=24,
        material_group="BEARING",
        equipment_type=None,
        base_unit_of_measure="KG",
        circuit=None,
        manufacturer="OTHER",
        unit_price=Decimal("10000"),
        description="hydraulic hose assembly",
    )
    donor_values[("D000099", "BMM")] = {
        "safety_stock": 9999,
        "rop": 9999,
        "max_stock": 9999,
    }

    candidates = [attributes(m, p) for m, _, _, p in DONORS] + [distant]
    result = evaluate_target(
        target, candidates, NoEmbeddingModel(), Decimal("20"), donor_values, DEV_POLICY
    )

    scored_below_floor = [
        n for n in result.neighbours if n.score.combined < Decimal("0.60")
    ]
    assert scored_below_floor == []
    assert "D000099" not in {n.material for n in result.neighbours}
    # And its values did not leak into the weighted average.
    assert result.estimate.safety_stock < 9999


def test_the_admission_floor_is_inclusive_at_exactly_point_six():
    """``>=``, so a candidate scoring exactly 0.60 qualifies -- pinned on
    ranking.select_top_k directly, where the comparison lives."""
    from app.initiatives.i7.oar import ranking
    from app.initiatives.i7.oar.types import CombinedScore, DimensionScore, SimilarityStatus

    def entry(material, value):
        dimension = DimensionScore(value=Decimal(value), status=SimilarityStatus.AVAILABLE)
        score = CombinedScore(
            combined=Decimal(value),
            structured=dimension,
            text=dimension,
            business=dimension,
            score_completeness=Decimal("1"),
        )
        return (material, "BMM", score, None, {})

    selected = ranking.select_top_k(
        [entry("AT_FLOOR", "0.60"), entry("BELOW", "0.5999")],
        10,
        minimum_similarity=Decimal("0.60"),
    )
    assert [n.material for n in selected] == ["AT_FLOOR"]


# --- 5. Qualifying neighbours with Phase 5 values produce an estimate -------


def test_dev_chain_produces_a_numerical_oar_estimate(dev_donors, dev_result):
    assert len(inventory_values(dev_donors)) == len(DONORS)

    assert dev_result.status is OarStatus.SUCCESS
    assert dev_result.estimate.status is EstimateStatus.SUCCESS
    assert isinstance(dev_result.estimate.safety_stock, int)
    assert isinstance(dev_result.estimate.rop, int)
    assert isinstance(dev_result.estimate.max_stock, int)
    assert dev_result.estimate.label == "SIMILARITY-BASED ESTIMATE"
    assert dev_result.estimate.inventory_eligible_neighbours == len(DONORS)
    assert dev_result.estimate.inventory_ineligible_neighbours == 0


def test_a_neighbour_missing_any_one_phase_five_value_lends_nothing(dev_donors):
    """``load_inventory_values`` requires all three statuses SUCCESS -- which
    is why an unconfigured Max Stock strategy alone was enough to block every
    OAR estimate, even with safety stock and ROP available."""
    donor_values = inventory_values(dev_donors)
    donor_values.pop(("D000001", "BMM"))

    result = run_oar(DEV_POLICY, donor_values)

    assert result.estimate.inventory_eligible_neighbours == len(DONORS) - 1
    assert result.estimate.inventory_ineligible_neighbours == 1
    contributed = {n.material for n in result.neighbours if n.inventory_eligible}
    assert "D000001" not in contributed


# --- 6/7/8. Each parameter is the similarity-weighted average ---------------


def weighted(result, attribute: str) -> int:
    """sum(s_i x v_i) / sum(s_i), rounded up -- computed here by hand so the
    assertion does not simply restate the implementation."""
    numerator = Decimal(0)
    denominator = Decimal(0)
    for neighbour in result.neighbours:
        if not neighbour.inventory_eligible:
            continue
        similarity = neighbour.score.combined
        numerator += similarity * Decimal(getattr(neighbour, attribute))
        denominator += similarity
    return math.ceil(numerator / denominator)


def test_oar_safety_stock_is_the_similarity_weighted_average(dev_result):
    assert dev_result.estimate.safety_stock == weighted(dev_result, "safety_stock")


def test_oar_rop_is_the_similarity_weighted_average(dev_result):
    assert dev_result.estimate.rop == weighted(dev_result, "rop")


def test_oar_max_is_the_similarity_weighted_average(dev_result):
    assert dev_result.estimate.max_stock == weighted(dev_result, "max_stock")


def test_oar_rop_is_borrowed_not_recomputed_from_the_target(dev_result):
    """OAR's ROP is the neighbours' weighted ROP, never
    ``forecast x lead time + SS`` computed for the target -- the target is a
    cold-start material with no forecast and no lead time to compute from."""
    weighted_rop = weighted(dev_result, "rop")
    weighted_ss = weighted(dev_result, "safety_stock")
    assert dev_result.estimate.rop == weighted_rop
    # The borrowed ROP already contains its donors' own lead-time demand, so
    # it is strictly above the borrowed safety stock without anything here
    # adding a lead-time term.
    assert dev_result.estimate.rop > weighted_ss


def test_each_parameter_is_weighted_independently(dev_result):
    """Max is not derived from the estimated ROP, and ROP is not derived from
    the estimated SS -- all three are separate weighted averages over the same
    neighbours."""
    assert dev_result.estimate.max_stock == weighted(dev_result, "max_stock")
    assert dev_result.estimate.max_stock != dev_result.estimate.rop * 2


# --- 9. The DEV Max Stock strategy produces a donor Max value ---------------


def test_dev_max_stock_strategy_produces_donor_max_values(dev_donors):
    for row in dev_donors.values():
        assert row["max_stock_status"] == "SUCCESS"
        assert row["max_stock_strategy"] == "review_period"
        assert isinstance(row["max_stock"], int)
        assert row["max_stock"] > row["rop"]


def test_donor_max_follows_the_existing_review_period_formula(dev_donors):
    """Max = ROP + (D_rate x T), the strategy that already existed."""
    months = Decimal(
        str(dict(load_mock_max_stock_policy().review_period_months)[Criticality.NORMAL])
    )
    for material, mean, _, _ in DONORS:
        row = dev_donors[material]
        expected = Decimal(row["rop"]) + Decimal(str(mean)) * months
        assert row["max_stock"] == math.ceil(expected)


# --- 10. A successful OAR recommendation exposes numerical SS/ROP/Max -------


def oar_recommendation_row(result) -> SimpleNamespace:
    """The Phase 3/6 join ``repository.load_oar_inputs`` produces, filled from
    the estimate this chain actually computed."""
    return SimpleNamespace(
        sap_material_number=result.sap_material_number,
        sap_plant_code=result.sap_plant_code,
        history_status="NO_HISTORY",
        criticality="NORMAL",
        non_zero_periods=None,
        consumption_count_12m=None,
        demand_class="UNCLASSIFIED",
        feature_run_id=1,
        status=result.status.value,
        confidence=result.confidence.value,
        candidates_considered=result.candidates_considered,
        eligible_candidates=result.eligible_candidates,
        neighbour_count=len(result.neighbours),
        best_similarity=result.best_similarity,
        estimate_status=result.estimate.status.value,
        oar_safety_stock=result.estimate.safety_stock,
        oar_rop=result.estimate.rop,
        oar_max_stock=result.estimate.max_stock,
        oar_run_id=7,
        current_safety_stock=None,
        current_reorder_point=None,
        current_maximum_stock=None,
        unit_price=Decimal("101"),
    )


def test_successful_oar_recommendation_carries_the_numbers(dev_result):
    built = builder.build_oar_recommendation(oar_recommendation_row(dev_result), DEV_POLICY)

    assert built.status is LifecycleStatus.READY_FOR_REVIEW
    assert built.blocking_reason is None
    assert built.recommended_safety_stock == dev_result.estimate.safety_stock
    assert built.recommended_rop == dev_result.estimate.rop
    assert built.recommended_max_stock == dev_result.estimate.max_stock
    assert built.safety_stock_method == "similarity_weighted"
    assert built.oar_estimate_status == "SUCCESS"


def test_the_api_detail_schema_exposes_the_recommended_values(dev_result):
    """``StockParameters.recommended`` -- the shape the FastAPI response
    serialises -- carries all three, with no new field introduced."""
    from app.schemas.i7.recommendations import StockParameters

    built = builder.build_oar_recommendation(oar_recommendation_row(dev_result), DEV_POLICY)
    recommended = StockParameters(
        safety_stock=built.recommended_safety_stock,
        rop=built.recommended_rop,
        max_stock=built.recommended_max_stock,
    )

    assert recommended.safety_stock is not None
    assert recommended.rop is not None
    assert recommended.max_stock is not None


# --- The verified scenario, pinned to its exact expected numbers ------------


EXPECTED_DONORS = {
    # material -> (similarity, SS, ROP, Max)
    "D000001": ("0.9875", 5, 15, 25),
    "D000006": ("0.9875", 4, 13, 22),
    "D000005": ("0.9625", 5, 16, 27),
    "D000002": ("0.95", 6, 18, 30),
    "D000003": ("0.925", 4, 12, 20),
    "D000004": ("0.8875", 7, 22, 37),
}

EXPECTED_ESTIMATE = {"safety_stock": 6, "rop": 16, "max_stock": 27}


def test_verified_scenario_donor_table(dev_donors, dev_result):
    """Each donor's similarity and its own Phase 5 SS/ROP/Max, exactly.

    Pinned as literals so a change in any upstream formula -- safety stock,
    ROP, the review-period Max, or the similarity weights -- shows up here as
    a specific number rather than as a still-passing relative assertion.
    """
    actual = {
        neighbour.material: (
            str(neighbour.score.combined),
            neighbour.safety_stock,
            neighbour.rop,
            neighbour.max_stock,
        )
        for neighbour in dev_result.neighbours
    }
    assert actual == EXPECTED_DONORS


def test_verified_scenario_produces_six_sixteen_twentyseven(dev_result):
    """SS = 6, ROP = 16, Max = 27 -- from the existing weighted average over
    the donor table above, with nothing in this file adjusting the formulas to
    reach them (``weighted()`` recomputes them independently, and the two
    agree)."""
    assert dev_result.estimate.status is EstimateStatus.SUCCESS
    assert dev_result.estimate.safety_stock == EXPECTED_ESTIMATE["safety_stock"]
    assert dev_result.estimate.rop == EXPECTED_ESTIMATE["rop"]
    assert dev_result.estimate.max_stock == EXPECTED_ESTIMATE["max_stock"]

    for attribute, expected in EXPECTED_ESTIMATE.items():
        assert weighted(dev_result, attribute) == expected


def test_verified_scenario_reaches_the_recommendation_unchanged(dev_result):
    """The same three numbers, at the end of the recommendation chain."""
    built = builder.build_oar_recommendation(oar_recommendation_row(dev_result), DEV_POLICY)

    assert built.recommended_safety_stock == 6
    assert built.recommended_rop == 16
    assert built.recommended_max_stock == 27


# --- No dev fixture is reachable from calculation logic ---------------------


def test_no_calculation_module_imports_a_dev_fixture():
    """The fixtures are assembled into a policy by ``default_policy()`` and
    handed *in*. No module that computes a number may reach for one itself --
    that is what keeps a dev value out of production arithmetic even if a flag
    were set by accident.

    Orchestration is the deliberate exception and is checked separately below:
    the four ``run_*`` entry points exist precisely to resolve a policy.
    """
    import inspect

    from app.initiatives.i7.inventory import (
        max_stock as max_stock_module,
        rop as rop_module,
        safety_stock as safety_stock_module,
        service_level as service_level_module,
    )
    from app.initiatives.i7.oar import estimate as estimate_module
    from app.initiatives.i7.oar import ranking as ranking_module

    for module in (
        max_stock_module,
        rop_module,
        safety_stock_module,
        service_level_module,
        estimate_module,
        ranking_module,
    ):
        source = inspect.getsource(module)
        assert "dev_fixtures" not in source, module.__name__
        assert "load_mock_" not in source, module.__name__
        assert "i7_max_stock_strategy.yaml" not in source, module.__name__
        assert "i7_service_level_matrix.yaml" not in source, module.__name__


def test_orchestrators_resolve_a_policy_but_never_load_a_fixture_themselves():
    """The four entry points may call ``default_policy()`` -- that is their
    job -- but none may call a ``load_mock_*`` loader directly. Only
    ``default_policy()`` does, and only behind its flags, so there is exactly
    one place a fixture can enter a run."""
    import inspect

    from app.initiatives.i7.forecasting import service as forecasting_service
    from app.initiatives.i7.inventory import service as inventory_service
    from app.initiatives.i7.oar import service as oar_service
    from app.initiatives.i7.recommendations import service as recommendation_service

    for module in (
        forecasting_service,
        inventory_service,
        oar_service,
        recommendation_service,
    ):
        source = inspect.getsource(module)
        assert "load_mock_service_level_policy" not in source, module.__name__
        assert "load_mock_max_stock_policy" not in source, module.__name__


def test_calculation_modules_hold_no_dev_placeholder_values():
    """Neither the 0.85 service level nor the 1-month review period may appear
    as a literal in the code that computes with them."""
    import inspect

    from app.initiatives.i7.inventory import (
        max_stock as max_stock_module,
        service as inventory_service,
        service_level as service_level_module,
    )

    for module in (max_stock_module, service_level_module, inventory_service):
        source = inspect.getsource(module)
        assert "0.85" not in source, module.__name__
        assert "review_period_months = " not in source, module.__name__


# --- 11. Production configuration still behaves as production --------------


def test_production_policy_blocks_every_donor_calculation():
    for row in phase_five(PRODUCTION_POLICY).values():
        assert row["service_level_status"] == "NOT_EVALUABLE_SERVICE_LEVEL_UNSET"
        assert row["safety_stock"] is None
        assert row["rop"] is None
        assert row["max_stock"] is None
        assert row["max_stock_status"] == "NOT_CONFIGURED"


def test_production_policy_yields_no_borrowable_donor_values():
    assert inventory_values(phase_five(PRODUCTION_POLICY)) == {}


def test_production_policy_blocks_the_oar_estimate():
    result = run_oar(PRODUCTION_POLICY, inventory_values(phase_five(PRODUCTION_POLICY)))

    # The similarity work still happens and is still recorded -- only the
    # borrowing is blocked.
    assert result.status is OarStatus.SUCCESS
    assert len(result.neighbours) == len(DONORS)
    assert result.estimate.status is EstimateStatus.NOT_EVALUABLE_SERVICE_LEVEL_UNSET
    assert result.estimate.safety_stock is None
    assert result.estimate.rop is None
    assert result.estimate.max_stock is None


def test_production_policy_recommendation_stays_not_evaluable():
    result = run_oar(PRODUCTION_POLICY, {})
    built = builder.build_oar_recommendation(
        oar_recommendation_row(result), PRODUCTION_POLICY
    )

    assert built.status is LifecycleStatus.NOT_EVALUABLE
    assert built.recommended_safety_stock is None
    assert built.recommended_rop is None
    assert built.recommended_max_stock is None


def test_the_gates_themselves_are_unchanged():
    """The four policy constants this task was forbidden to weaken."""
    similarity = PolicyDocument().similarity
    assert similarity.minimum_similarity == 0.60
    assert similarity.minimum_neighbours == 5
    assert similarity.maximum_neighbours == 10
    assert similarity.minimum_neighbour_history_months == 12
