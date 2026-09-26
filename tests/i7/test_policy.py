"""Policy configuration, validation and versioning tests.

The central property: a *configured* value and an *undecided* one are different
states, and the second must block rather than default. Most of these tests exist
to stop a plausible default sneaking in later.
"""

from datetime import date

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.initiatives.i7.contracts import Criticality, PolicyStatus
from app.initiatives.i7.errors import (
    ConfigurationError,
    I07Error,
    PolicyNotConfiguredError,
)
from app.initiatives.i7.policy import (
    ClassificationPolicy,
    ConfidencePolicy,
    ConversionTriggerPolicy,
    HistoryGatePolicy,
    LeadTimePolicy,
    MaxStockStrategy,
    ModelAdoptionPolicy,
    PolicyDocument,
    PolicyVersionRef,
    ServiceLevelKey,
    ServiceLevelPolicy,
    SimilarityPolicy,
)


# --- Documented values are preserved ----------------------------------


def test_classification_cutoffs_match_the_documents():
    policy = ClassificationPolicy()
    assert policy.adi_cutoff == 1.32
    assert policy.cv_squared_cutoff == 0.49


def test_history_gate_matches_the_documents():
    policy = HistoryGatePolicy()
    assert policy.minimum_non_zero_periods == 5
    assert policy.minimum_history_months == 6


def test_model_adoption_thresholds_match_the_documents():
    policy = ModelAdoptionPolicy()
    assert policy.minimum_pinball_improvement == 0.05
    assert policy.maximum_bias_deterioration == 0.05
    assert policy.minimum_backtest_origins == 12


def test_similarity_weights_match_the_documents():
    policy = SimilarityPolicy()
    assert (policy.structural_weight, policy.text_weight, policy.business_weight) == (
        0.35,
        0.30,
        0.35,
    )
    assert (policy.minimum_neighbours, policy.maximum_neighbours) == (5, 10)


def test_confidence_thresholds_kept_despite_data_not_reaching_them():
    """No material has 24 months today. That is a data finding, not a reason to
    redefine what HIGH confidence means to an approver."""
    assert ConfidencePolicy().high_minimum_history_months == 24


def test_conversion_consumption_trigger_matches_the_documents():
    policy = ConversionTriggerPolicy()
    assert policy.consumption_count_threshold == 4
    assert policy.consumption_count_months == 12


# --- Invalid configuration is rejected --------------------------------


@pytest.mark.parametrize("cutoff", [0, -1.32])
def test_invalid_adi_cutoff_rejected(cutoff):
    with pytest.raises(PydanticValidationError):
        ClassificationPolicy(adi_cutoff=cutoff)


@pytest.mark.parametrize("cutoff", [0, -0.49])
def test_invalid_cv_squared_cutoff_rejected(cutoff):
    with pytest.raises(PydanticValidationError):
        ClassificationPolicy(cv_squared_cutoff=cutoff)


def test_negative_history_threshold_rejected():
    with pytest.raises(PydanticValidationError):
        HistoryGatePolicy(minimum_history_months=-1)


def test_similarity_weights_must_sum_to_one():
    with pytest.raises(PydanticValidationError, match="must sum to 1.0"):
        SimilarityPolicy(structural_weight=0.5, text_weight=0.3, business_weight=0.3)


def test_documented_weights_survive_float_addition():
    """0.35 + 0.30 + 0.35 is not exactly 1.0 in binary; the tolerance matters."""
    assert SimilarityPolicy().structural_weight == 0.35


def test_inverted_neighbour_range_rejected():
    with pytest.raises(PydanticValidationError, match="minimum_neighbours"):
        SimilarityPolicy(minimum_neighbours=10, maximum_neighbours=5)


def test_confidence_bands_must_be_ordered():
    with pytest.raises(PydanticValidationError, match="must not exceed HIGH"):
        ConfidencePolicy(high_minimum_history_months=12, medium_minimum_history_months=24)


def test_inverted_lead_time_bounds_rejected():
    with pytest.raises(PydanticValidationError, match="minimum_valid_days"):
        LeadTimePolicy(minimum_valid_days=730, maximum_valid_days=1)


def test_negative_pinball_improvement_rejected():
    with pytest.raises(PydanticValidationError):
        ModelAdoptionPolicy(minimum_pinball_improvement=-0.05)


# --- Unresolved policies stay unresolved -------------------------------


def test_service_level_matrix_is_empty_by_default():
    assert ServiceLevelPolicy().is_configured is False


def test_service_level_lookup_blocks_when_unsigned():
    with pytest.raises(PolicyNotConfiguredError) as exc:
        ServiceLevelPolicy().service_level_for(Criticality.CRITICAL, "Milling")
    assert exc.value.policy == "service_level_matrix"


def test_service_level_lookup_works_once_signed():
    policy = ServiceLevelPolicy(
        matrix=((ServiceLevelKey(criticality=Criticality.CRITICAL, circuit="Milling"), 0.98),)
    )
    assert policy.service_level_for(Criticality.CRITICAL, "Milling") == 0.98


def test_circuit_wildcard_entry_is_supported():
    """No SAP field supplies circuit, so a criticality-only matrix must work."""
    policy = ServiceLevelPolicy(
        matrix=((ServiceLevelKey(criticality=Criticality.NORMAL), 0.90),)
    )
    assert policy.service_level_for(Criticality.NORMAL, "Crushing") == 0.90


def test_unlisted_combination_blocks_rather_than_falling_back():
    policy = ServiceLevelPolicy(
        matrix=((ServiceLevelKey(criticality=Criticality.CRITICAL, circuit="Milling"), 0.98),)
    )
    with pytest.raises(PolicyNotConfiguredError):
        policy.service_level_for(Criticality.NORMAL, "Milling")


def test_max_stock_strategy_is_unset_by_default():
    assert MaxStockStrategy().is_configured is False


def test_max_stock_blocks_when_unchosen():
    with pytest.raises(PolicyNotConfiguredError) as exc:
        MaxStockStrategy().require_configured()
    assert exc.value.policy == "max_stock_strategy"


def test_max_stock_eoq_inputs_absent_by_default():
    """Ordering cost and holding rate appear in no document and no table."""
    strategy = MaxStockStrategy()
    assert strategy.ordering_cost is None
    assert strategy.holding_cost_rate is None
    assert strategy.review_period_months == ()


def test_conversion_criticality_tiers_unset_because_the_frs_contradicts_itself():
    """FR-5 says "Critical"; section 3.1 says "Critical or significant production
    impact". Different tier sets -- not ours to choose. This describes
    ConversionTriggerPolicy's own bare default; PolicyDocument's default now
    resolves this via current_conversion_trigger_policy() instead (see the
    test immediately below)."""
    assert ConversionTriggerPolicy().criticality_trigger_tiers is None


def test_policy_document_default_resolves_conversion_criticality_tiers_to_normal():
    """Business-confirmed: NORMAL is the tier that fires the OAR -> Min-Max
    conversion's Trigger 2 in this system, not CRITICAL/IMPACT despite the FRS
    wording (see policy/thresholds.py::current_conversion_trigger_policy).
    PolicyDocument's own default now carries this resolved value -- a fresh
    PolicyDocument() is no longer blocked on this specific gap."""
    policy = PolicyDocument()
    assert policy.conversion_triggers.criticality_trigger_tiers == ("NORMAL",)
    assert "conversion_criticality_tiers" not in policy.unresolved_policies()


# --- Policy document ---------------------------------------------------


def test_document_defaults_to_draft():
    assert PolicyDocument().status is PolicyStatus.DRAFT


def test_document_lists_every_unresolved_policy():
    # conversion_criticality_tiers is no longer here: PolicyDocument's own
    # default now resolves it via current_conversion_trigger_policy()
    # (criticality_trigger_tiers=("NORMAL",), a confirmed business decision --
    # see policy/thresholds.py). ConversionTriggerPolicy() built bare (with no
    # PolicyDocument around it) still defaults criticality_trigger_tiers to
    # None on its own, which is what test_conversion_criticality_tiers_unset_
    # because_the_frs_contradicts_itself asserts -- the two tests are not in
    # tension; they check different construction paths.
    # adoption_monitoring_window is no longer here: AdoptionPolicy's default
    # monitoring_window_days is now 15 (a confirmed decision -- see
    # policy/thresholds.py), so it resolves automatically.
    outstanding = PolicyDocument().unresolved_policies()
    assert set(outstanding) == {
        "service_level_matrix",
        "max_stock_strategy",
        "oar_rule_confirmation",
        "oar_rollup_policy",
    }


def test_document_is_not_ready_for_recommendations():
    assert PolicyDocument().is_ready_for_recommendations is False


def test_unsigned_policy_blocks_recommendations():
    """Solution Design Rule 1: unsigned policy blocks recommendations."""
    with pytest.raises(PolicyNotConfiguredError, match="not SIGNED"):
        PolicyDocument().require_ready_for_recommendations()


def test_signed_policy_with_gaps_still_blocks():
    document = PolicyDocument(status=PolicyStatus.SIGNED)
    with pytest.raises(PolicyNotConfiguredError, match="unresolved policies"):
        document.require_ready_for_recommendations()


def test_document_validates_cross_policy_consistency():
    PolicyDocument().validate_document()


def test_effective_dates_must_be_ordered():
    document = PolicyDocument(
        effective_from=date(2026, 9, 1), effective_to=date(2026, 8, 1)
    )
    with pytest.raises(ConfigurationError, match="precedes effective_from"):
        document.validate_document()


def test_confidence_below_history_gate_is_rejected():
    """Otherwise a material could grade HIGH without ever being classified."""
    document = PolicyDocument(
        history_gate=HistoryGatePolicy(minimum_history_months=24),
        confidence=ConfidencePolicy(
            high_minimum_history_months=12, medium_minimum_history_months=12
        ),
    )
    with pytest.raises(ConfigurationError, match="confidence HIGH"):
        document.validate_document()


def test_similarity_neighbour_history_below_gate_is_rejected():
    document = PolicyDocument(
        history_gate=HistoryGatePolicy(minimum_history_months=24),
        similarity=SimilarityPolicy(minimum_neighbour_history_months=12),
    )
    with pytest.raises(ConfigurationError, match="too little history"):
        document.validate_document()


# --- Versioning --------------------------------------------------------


def test_document_exposes_a_version_reference():
    reference = PolicyDocument(policy_id="i07-pilot", policy_version=3).reference
    assert (reference.policy_id, reference.policy_version) == ("i07-pilot", 3)
    assert str(reference) == "i07-pilot@v3"


def test_version_must_be_positive():
    with pytest.raises(PydanticValidationError):
        PolicyVersionRef(policy_id="i07-default", policy_version=0)


def test_policy_id_must_not_be_blank():
    with pytest.raises(PydanticValidationError):
        PolicyVersionRef(policy_id="", policy_version=1)


def test_document_is_immutable():
    """A stored version must still read the same months later."""
    document = PolicyDocument()
    with pytest.raises(PydanticValidationError):
        document.policy_version = 2


# --- Errors ------------------------------------------------------------


def test_every_domain_error_shares_one_base():
    assert issubclass(ConfigurationError, I07Error)
    assert issubclass(PolicyNotConfiguredError, I07Error)


def test_not_configured_is_distinct_from_invalid():
    """"Nobody decided" and "someone typed it wrong" need different responses."""
    assert not issubclass(PolicyNotConfiguredError, ConfigurationError)


def test_not_configured_error_names_the_policy_and_explains():
    error = PolicyNotConfiguredError("service_level_matrix", "Signed matrix required.")
    assert error.policy == "service_level_matrix"
    assert "service_level_matrix" in str(error)
    assert "Signed matrix required." in str(error)
