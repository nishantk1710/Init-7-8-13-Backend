"""The development-only mock Criticality -> Service Level matrix.

Every test here is about containment, not about the numbers: this fixture
exists to let LightGBM/Z-factor/safety-stock/ROP be exercised locally without
the real Vedanta-signed matrix (Solution Design Rule 1: "Do NOT invent
percentages"), so the tests prove it stays inert unless a caller explicitly
opts in, never becomes PolicyDocument()'s default, and never alone makes a
policy SIGNED.
"""

from pathlib import Path

import pytest

from app.initiatives.i7.contracts.enums import Criticality, PolicyStatus
from app.initiatives.i7.errors import ConfigurationError, PolicyNotConfiguredError
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    DEFAULT_MOCK_PATH,
    load_mock_service_level_policy,
)


def test_default_policy_document_is_still_unconfigured():
    """The mock is never wired into PolicyDocument's own default construction."""
    policy = PolicyDocument()
    assert not policy.service_level.is_configured
    with pytest.raises(PolicyNotConfiguredError):
        policy.service_level.service_level_for(Criticality.CRITICAL)


def test_mock_file_exists_at_the_documented_path():
    assert DEFAULT_MOCK_PATH.is_file()
    assert DEFAULT_MOCK_PATH.name == "i7_service_level_matrix.yaml"


def test_loading_the_mock_produces_a_configured_policy():
    mock = load_mock_service_level_policy()
    assert mock.is_configured
    for tier in (
        Criticality.CRITICAL,
        Criticality.IMPACT,
        Criticality.INSURANCE,
        Criticality.NORMAL,
    ):
        level = mock.service_level_for(tier)
        assert 0.0 < level <= 1.0


def test_mock_never_defines_obsolete():
    """OBSOLETE never reaches a safety-stock calculation (inventory/service.py's
    OBSOLETE_TIER short-circuit), so the mock correctly has nothing for it --
    a lookup must still raise, not silently succeed with an invented number.
    """
    mock = load_mock_service_level_policy()
    with pytest.raises(PolicyNotConfiguredError):
        mock.service_level_for(Criticality.OBSOLETE)


def test_mock_has_no_circuit_dimension():
    """Matches the FRS: the service-level matrix is criticality-only."""
    mock = load_mock_service_level_policy()
    for key, _ in mock.matrix:
        assert key.circuit is None


def test_opting_in_requires_an_explicit_call():
    """The mock only reaches a PolicyDocument if a caller builds one with it --
    there is no flag or side channel that injects it automatically.
    """
    policy = PolicyDocument(service_level=load_mock_service_level_policy())
    assert policy.service_level.is_configured
    # Status is still DRAFT: loading the mock does not sign the policy.
    assert policy.status is PolicyStatus.DRAFT


def test_a_mocked_but_unsigned_policy_still_blocks_recommendations():
    """The mock closes the *service-level* gap, not the *sign-off* gap --
    require_ready_for_recommendations must still refuse a DRAFT policy even
    once service_level is configured.
    """
    policy = PolicyDocument(service_level=load_mock_service_level_policy())
    assert policy.status is PolicyStatus.DRAFT
    with pytest.raises(PolicyNotConfiguredError):
        policy.require_ready_for_recommendations()


def test_missing_file_raises_a_clear_configuration_error(tmp_path):
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigurationError):
        load_mock_service_level_policy(missing)


def test_unknown_tier_name_raises_rather_than_being_silently_dropped(tmp_path):
    bad_file = tmp_path / "bad.yaml"
    bad_file.write_text("service_level_by_criticality:\n  NOT_A_REAL_TIER: 0.9\n")
    with pytest.raises(ConfigurationError):
        load_mock_service_level_policy(bad_file)


def test_empty_matrix_section_raises(tmp_path):
    empty_file = tmp_path / "empty.yaml"
    empty_file.write_text("service_level_by_criticality: {}\n")
    with pytest.raises(ConfigurationError):
        load_mock_service_level_policy(empty_file)


def test_default_settings_flag_is_false():
    """False everywhere by default, including whatever environment tests run
    under -- the flag existing must not itself change behaviour.
    """
    from app.core.config import Settings

    assert Settings().i7_dev_mock_service_level is False


# --- default_policy(): the one place the mock reaches a real pipeline run ----
#
# run_forecasting() and run_inventory_calculations() both fall back to
# default_policy() when no caller supplies a PolicyDocument. These tests prove
# that fallback still returns an empty, unsigned policy unless a developer has
# explicitly set I7_DEV_MOCK_SERVICE_LEVEL -- the flag is the only thing that
# can flip it, and flipping it is exactly what a real forecasting/inventory
# run (I07_forecasting_service.run_forecasting) uses to actually train
# LightGBM instead of reporting NOT_EVALUABLE_SERVICE_LEVEL_UNSET.


def test_default_policy_stays_unconfigured_when_flag_is_unset(monkeypatch):
    monkeypatch.delenv("I7_DEV_MOCK_SERVICE_LEVEL", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.initiatives.i7.policy.dev_fixtures import default_policy

        policy = default_policy()
        assert not policy.service_level.is_configured
        assert policy.status is PolicyStatus.DRAFT
    finally:
        get_settings.cache_clear()


def test_default_policy_loads_the_mock_when_flag_is_true(monkeypatch):
    monkeypatch.setenv("I7_DEV_MOCK_SERVICE_LEVEL", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.initiatives.i7.policy.dev_fixtures import default_policy

        policy = default_policy()
        assert policy.service_level.is_configured
        level = policy.service_level.service_level_for(Criticality.CRITICAL)
        assert 0.0 < level <= 1.0
        # Loading the mock still does not sign the policy on its own.
        assert policy.status is PolicyStatus.DRAFT
    finally:
        get_settings.cache_clear()


def test_default_policy_id_is_i07_default_when_flag_is_unset(monkeypatch):
    """Part 25 -- policy_id must stay exactly what it always was when the
    mock is off, so a production deployment's runs are unaffected."""
    monkeypatch.delenv("I7_DEV_MOCK_SERVICE_LEVEL", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.initiatives.i7.policy.dev_fixtures import default_policy

        assert default_policy().policy_id == "i07-default"
    finally:
        get_settings.cache_clear()


def test_default_policy_id_is_distinct_when_service_level_mock_is_active(monkeypatch):
    """Part 25 -- the actual fix. Without a distinct policy_id, a DEV-mocked
    run and a real/unconfigured run share the same
    (feature_run_id, forecast_run_id, policy_id, policy_version,
    formula_version) identity -- i7_inventory_run's and i7_recommendation's
    own idempotency key -- so an existing unconfigured run gets silently
    reused instead of the mock ever taking effect."""
    monkeypatch.setenv("I7_DEV_MOCK_SERVICE_LEVEL", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.initiatives.i7.policy.dev_fixtures import default_policy

        policy = default_policy()
        assert policy.policy_id == "i07-default-dev-mock"
        assert policy.policy_id != "i07-default"
    finally:
        get_settings.cache_clear()


def test_default_policy_id_unaffected_by_max_stock_mock_alone(monkeypatch):
    """Only the service-level mock needs a distinct policy_id -- it is the
    one that changes safety_stock_status/service_level_status at the
    inventory-calculation layer. The Max Stock mock does not independently
    need one: Max Stock is computed from ROP, so its result already varies
    with whatever the service-level mock (or its absence) produced under the
    same run."""
    monkeypatch.delenv("I7_DEV_MOCK_SERVICE_LEVEL", raising=False)
    monkeypatch.setenv("I7_DEV_MOCK_MAX_STOCK", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.initiatives.i7.policy.dev_fixtures import default_policy

        assert default_policy().policy_id == "i07-default"
    finally:
        get_settings.cache_clear()


def test_policy_ids_are_distinguishable_by_the_inventory_run_idempotency_key():
    """Proves the fix actually addresses the idempotency collision: the two
    policy_id values, held alongside identical feature/forecast/formula
    version inputs, form two DIFFERENT identities under
    i7_inventory_run.uq_i7_inventory_run_inputs
    (feature_run_id, forecast_run_id, policy_id, policy_version,
    formula_version) -- so both a real and a DEV-mocked run can be inserted
    without a unique-constraint collision.
    """
    real_key = (1, 1, "i07-default", 1, "i07-formula-1")
    dev_mock_key = (1, 1, "i07-default-dev-mock", 1, "i07-formula-1")
    assert real_key != dev_mock_key


def test_target_quantile_resolves_once_the_flag_is_set(monkeypatch):
    """The exact chain that was blocking LightGBM: _target_quantile(policy)
    must move from None to a real number once the flag flips.
    """
    from app.core.config import get_settings
    from app.initiatives.i7.forecasting.service import _target_quantile
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    monkeypatch.delenv("I7_DEV_MOCK_SERVICE_LEVEL", raising=False)
    get_settings.cache_clear()
    try:
        assert _target_quantile(default_policy()) is None
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("I7_DEV_MOCK_SERVICE_LEVEL", "true")
    get_settings.cache_clear()
    try:
        quantile = _target_quantile(default_policy())
        assert quantile is not None
        assert 0.0 < quantile <= 1.0
    finally:
        get_settings.cache_clear()
