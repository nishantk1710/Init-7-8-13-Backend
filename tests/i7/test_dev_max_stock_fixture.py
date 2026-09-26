"""The development-only mock Max Stock strategy.

The Max Stock counterpart of ``test_dev_service_level_fixture.py``, and these
tests are about the same thing: containment, not the numbers. Solution Design
Rule 2 requires the formula to be agreed with Vedanta and forbids picking one
without sign-off, while also saying the choice "is configuration, not a coding
decision" -- so the fixture selects an already-implemented strategy through
the already-existing ``MaxStockStrategy`` schema, and these tests prove it
stays inert unless a caller explicitly opts in, never becomes
``PolicyDocument()``'s default, never alone makes a policy SIGNED, and never
reaches ``inventory/max_stock.py``'s arithmetic as a new formula.
"""

from decimal import Decimal

import pytest

from app.initiatives.i7.contracts.enums import Criticality, PolicyStatus
from app.initiatives.i7.errors import ConfigurationError, PolicyNotConfiguredError
from app.initiatives.i7.inventory.max_stock import (
    MaxStockContext,
    NotConfiguredMaxStockStrategy,
    ReviewPeriodMaxStockStrategy,
    strategy_for,
)
from app.initiatives.i7.inventory.types import CalculationStatus
from app.initiatives.i7.policy import PolicyDocument
from app.initiatives.i7.policy.dev_fixtures import (
    DEFAULT_MAX_STOCK_MOCK_PATH,
    load_mock_max_stock_policy,
)


# --- Containment ------------------------------------------------------------


def test_default_policy_document_still_has_no_max_stock_strategy():
    """The mock is never wired into PolicyDocument's own default construction."""
    policy = PolicyDocument()
    assert not policy.max_stock.is_configured
    with pytest.raises(PolicyNotConfiguredError):
        policy.max_stock.require_configured()


def test_production_default_still_resolves_to_the_not_configured_strategy():
    """What production actually computes with: a strategy that returns a
    status and never a number, however complete the context around it."""
    strategy = strategy_for(PolicyDocument().max_stock)
    assert isinstance(strategy, NotConfiguredMaxStockStrategy)

    result = strategy.calculate(
        MaxStockContext(
            safety_stock=5,
            rop=15,
            forecast_rate=Decimal("10"),
            criticality="NORMAL",
            unit_price=Decimal("100"),
            ordering_cost=None,
            holding_cost_rate=None,
            review_period_months=Decimal("1"),
        )
    )
    assert result.status is CalculationStatus.NOT_CONFIGURED
    assert result.max_stock is None


def test_mock_file_exists_at_the_documented_path():
    assert DEFAULT_MAX_STOCK_MOCK_PATH.is_file()
    assert DEFAULT_MAX_STOCK_MOCK_PATH.name == "i7_max_stock_strategy.yaml"


def test_mock_file_labels_itself_dev_test_only():
    """A deliberately fragile guard: the fixture must say so in the file a
    reviewer opens, not only in the module that loads it."""
    text = DEFAULT_MAX_STOCK_MOCK_PATH.read_text(encoding="utf-8")
    assert "DEV/TEST ONLY" in text
    assert "NOT VZI PRODUCTION POLICY" in text


def test_loading_the_mock_produces_a_configured_review_period_policy():
    mock = load_mock_max_stock_policy()
    assert mock.is_configured
    assert mock.require_configured() == "review_period"
    tiers = {tier for tier, _ in mock.review_period_months}
    assert Criticality.NORMAL in tiers
    for _, months in mock.review_period_months:
        assert months > 0


def test_mock_never_defines_obsolete():
    """OBSOLETE short-circuits to NOT_APPLICABLE_OBSOLETE in
    inventory/service.py before Max Stock is reached, so a review period for
    it would never be looked up."""
    mock = load_mock_max_stock_policy()
    assert all(tier is not Criticality.OBSOLETE for tier, _ in mock.review_period_months)


def test_opting_in_requires_an_explicit_call():
    policy = PolicyDocument(max_stock=load_mock_max_stock_policy())
    assert policy.max_stock.is_configured
    # Status is still DRAFT: loading the mock does not sign the policy.
    assert policy.status is PolicyStatus.DRAFT


def test_a_mocked_but_unsigned_policy_still_blocks_recommendations():
    """The mock closes the *strategy* gap, not the *sign-off* gap."""
    policy = PolicyDocument(max_stock=load_mock_max_stock_policy())
    with pytest.raises(PolicyNotConfiguredError):
        policy.require_ready_for_recommendations()


def test_default_settings_flag_is_false():
    """False everywhere by default, including whatever environment tests run
    under -- the flag existing must not itself change behaviour."""
    from app.core.config import Settings

    assert Settings().i7_dev_mock_max_stock is False


# --- The fixture introduces no new algorithm --------------------------------


def test_mock_selects_an_already_implemented_strategy():
    """Not a new formula: the name must resolve to one of the strategies that
    existed before this fixture, so nothing here can compute under a formula
    ``inventory/max_stock.py`` does not already define and test."""
    mock = load_mock_max_stock_policy()
    assert isinstance(strategy_for(mock), ReviewPeriodMaxStockStrategy)


def test_mock_does_not_configure_eoq():
    """EOQ needs an ordering cost and a holding rate that exist in no document
    and no table. Mocking those would invent two business figures with no
    source to be replaced from; the fixture deliberately does not."""
    mock = load_mock_max_stock_policy()
    assert mock.ordering_cost is None
    assert mock.holding_cost_rate is None


def test_review_period_arithmetic_is_the_existing_formula():
    """Max = ROP + (D_rate x T), computed by the pre-existing strategy."""
    mock = load_mock_max_stock_policy()
    months = dict(mock.review_period_months)[Criticality.NORMAL]

    result = strategy_for(mock).calculate(
        MaxStockContext(
            safety_stock=5,
            rop=15,
            forecast_rate=Decimal("10"),
            criticality="NORMAL",
            unit_price=Decimal("100"),
            ordering_cost=None,
            holding_cost_rate=None,
            review_period_months=Decimal(str(months)),
        )
    )
    assert result.status is CalculationStatus.SUCCESS
    assert result.strategy == "review_period"
    assert result.max_stock == 15 + int(10 * months)


# --- Loader validation ------------------------------------------------------


def test_the_review_period_comes_from_the_yaml_not_from_code():
    """The one number this fixture supplies must be traceable to the file.

    Changing the YAML must change what the calculation uses, and the value
    must not appear as a literal in the strategy or the orchestration around
    it -- otherwise "configuration, not a coding decision" would be true only
    on paper.
    """
    import inspect

    from app.initiatives.i7.inventory import max_stock as max_stock_module
    from app.initiatives.i7.inventory import service as inventory_service

    for module in (max_stock_module, inventory_service):
        source = inspect.getsource(module)
        assert "1.0" not in source, module.__name__
        assert "Decimal(1)" not in source, module.__name__

    # And the value the calculation consumes is the one the file supplies.
    from_file = dict(load_mock_max_stock_policy().review_period_months)
    assert from_file[Criticality.NORMAL] == 1.0


def test_a_different_yaml_value_changes_the_result(tmp_path):
    """Proves the coupling in the other direction: nothing downstream pins the
    review period independently of the file."""
    custom = tmp_path / "custom.yaml"
    custom.write_text(
        "strategy: review_period\n"
        "review_period_months_by_criticality:\n  NORMAL: 3.0\n"
    )
    policy = load_mock_max_stock_policy(custom)
    months = Decimal(str(dict(policy.review_period_months)[Criticality.NORMAL]))
    assert months == Decimal("3.0")

    result = strategy_for(policy).calculate(
        MaxStockContext(
            safety_stock=5,
            rop=15,
            forecast_rate=Decimal("10"),
            criticality="NORMAL",
            unit_price=Decimal("100"),
            ordering_cost=None,
            holding_cost_rate=None,
            review_period_months=months,
        )
    )
    # 15 + (10 x 3) = 45, not the 25 the shipped one-month fixture gives.
    assert result.max_stock == 45


def test_missing_file_raises_a_clear_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError):
        load_mock_max_stock_policy(tmp_path / "does_not_exist.yaml")


def test_unimplemented_strategy_name_raises(tmp_path):
    """A typo must fail at load, not silently resolve to
    NotConfiguredMaxStockStrategy thousands of rows later."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("strategy: hybrid_not_implemented\n")
    with pytest.raises(ConfigurationError):
        load_mock_max_stock_policy(bad)


def test_unknown_tier_name_raises(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "strategy: review_period\n"
        "review_period_months_by_criticality:\n  NOT_A_REAL_TIER: 1.0\n"
    )
    with pytest.raises(ConfigurationError):
        load_mock_max_stock_policy(bad)


def test_review_period_strategy_without_periods_raises(tmp_path):
    """Selecting review_period with no T would make every material report
    NOT_CONFIGURED -- exactly what the fixture exists to avoid, so it is a
    load-time error rather than a silent no-op."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("strategy: review_period\n")
    with pytest.raises(ConfigurationError):
        load_mock_max_stock_policy(bad)


def test_empty_file_raises(tmp_path):
    empty = tmp_path / "empty.yaml"
    empty.write_text("{}\n")
    with pytest.raises(ConfigurationError):
        load_mock_max_stock_policy(empty)


# --- default_policy(): the one place the mock reaches a real pipeline run ---


def test_default_policy_leaves_max_stock_unchosen_when_flag_is_unset(monkeypatch):
    monkeypatch.delenv("I7_DEV_MOCK_MAX_STOCK", raising=False)
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.initiatives.i7.policy.dev_fixtures import default_policy

        policy = default_policy()
        assert not policy.max_stock.is_configured
        assert isinstance(strategy_for(policy.max_stock), NotConfiguredMaxStockStrategy)
    finally:
        get_settings.cache_clear()


def test_default_policy_loads_the_mock_when_flag_is_true(monkeypatch):
    monkeypatch.setenv("I7_DEV_MOCK_MAX_STOCK", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        from app.initiatives.i7.policy.dev_fixtures import default_policy

        policy = default_policy()
        assert policy.max_stock.is_configured
        assert policy.status is PolicyStatus.DRAFT
    finally:
        get_settings.cache_clear()


def test_the_two_dev_flags_are_independent(monkeypatch):
    """Setting the Max Stock flag must not quietly configure a service level,
    and vice versa -- each answers its own unresolved policy."""
    from app.core.config import get_settings
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    monkeypatch.delenv("I7_DEV_MOCK_SERVICE_LEVEL", raising=False)
    monkeypatch.setenv("I7_DEV_MOCK_MAX_STOCK", "true")
    get_settings.cache_clear()
    try:
        policy = default_policy()
        assert policy.max_stock.is_configured
        assert not policy.service_level.is_configured
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv("I7_DEV_MOCK_SERVICE_LEVEL", "true")
    monkeypatch.delenv("I7_DEV_MOCK_MAX_STOCK", raising=False)
    get_settings.cache_clear()
    try:
        policy = default_policy()
        assert policy.service_level.is_configured
        assert not policy.max_stock.is_configured
    finally:
        get_settings.cache_clear()


def test_dev_max_stock_cannot_silently_become_production_configuration(monkeypatch):
    """Three independent guards, each of which alone keeps the fixture out of
    production: the env flag defaults false, the document stays DRAFT, and
    ``inventory/max_stock.py`` holds no reference to the fixture at all --
    so nothing in the calculation path can reach it without the flag."""
    import inspect

    from app.core.config import Settings, get_settings
    from app.initiatives.i7.inventory import max_stock as max_stock_module
    from app.initiatives.i7.policy.dev_fixtures import default_policy

    assert Settings().i7_dev_mock_max_stock is False

    source = inspect.getsource(max_stock_module)
    assert "dev_fixtures" not in source
    assert "load_mock_max_stock_policy" not in source
    assert "i7_max_stock_strategy.yaml" not in source

    monkeypatch.setenv("I7_DEV_MOCK_MAX_STOCK", "true")
    get_settings.cache_clear()
    try:
        assert default_policy().status is PolicyStatus.DRAFT
    finally:
        get_settings.cache_clear()


def test_no_two_times_rop_fallback_anywhere_in_max_stock():
    """Solution Design Rule 2 prohibits ``Max = 2 x ROP`` by name. The dev
    fixture must not have introduced one as a convenience."""
    import inspect

    from app.initiatives.i7.inventory import max_stock as max_stock_module

    source = inspect.getsource(max_stock_module)
    assert "2 * context.rop" not in source
    assert "Decimal(2) * context.rop" not in source
