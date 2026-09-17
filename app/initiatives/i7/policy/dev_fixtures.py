"""Development-only policy fixtures. Never loaded by default.

Two policies live here, both unresolved in
:mod:`app.initiatives.i7.policy.unresolved`: the Criticality x Circuit
service-level matrix (Solution Design Rule 1) and the Max Stock strategy
(Rule 2). Both must come from a Vedanta sign-off, not from code, and both
should block rather than guess while unsigned. This module does not relax
either rule -- it gives local development a way to exercise the chain they
gate (LightGBM's target quantile, the Z-factor, safety stock, ROP, Max Stock,
and the OAR similarity-weighted estimate that can only borrow from donor
materials whose own Phase 5 calculation succeeded), while keeping each mock:

* out of the default construction path (``PolicyDocument()`` never calls
  this module; a caller must explicitly opt in),
* in a config file, not a code literal, so a reviewer sees the numbers were
  never actually chosen "in" the code, and
* clearly, unmissably labelled at every layer -- the YAML file says so, this
  module's docstring says so, and the resulting :class:`PolicyDocument`
  keeps ``status=DRAFT`` unless a caller separately marks it SIGNED, so
  :meth:`PolicyDocument.require_ready_for_recommendations` still blocks real
  recommendation output by default.

Each mock has its own opt-in flag (``I7_DEV_MOCK_SERVICE_LEVEL``,
``I7_DEV_MOCK_MAX_STOCK``) and neither is set anywhere but a developer's own
environment.

One thing a loaded mock *does* change is
:meth:`PolicyDocument.unresolved_policies`, which reports on the shape of the
document and therefore stops listing a policy once something fills it in.
``status`` is what keeps the two apart: it stays ``DRAFT``, nothing in the
codebase ever sets ``SIGNED``, and
:meth:`PolicyDocument.require_ready_for_recommendations` checks it first --
so a mock can never, on its own, make a document claim to be a signed
Vedanta decision.
"""

from pathlib import Path

import yaml

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.errors import ConfigurationError
from app.initiatives.i7.policy.unresolved import (
    MaxStockStrategy,
    ServiceLevelKey,
    ServiceLevelPolicy,
)

DEFAULT_MOCK_PATH = (
    Path(__file__).resolve().parents[4] / "config" / "dev" / "i7_service_level_matrix.yaml"
)

DEFAULT_MAX_STOCK_MOCK_PATH = (
    Path(__file__).resolve().parents[4] / "config" / "dev" / "i7_max_stock_strategy.yaml"
)


def load_mock_service_level_policy(path: Path | str | None = None) -> ServiceLevelPolicy:
    """Build a :class:`ServiceLevelPolicy` from the dev-fixture YAML.

    Raises :class:`ConfigurationError` if the file is missing or a key does
    not name one of the five ZMM065 tiers -- a typo here should fail loudly,
    not silently produce an empty (and therefore still-blocking) matrix.

    Not wired into :class:`app.initiatives.i7.policy.document.PolicyDocument`'s
    default construction. A caller opts in explicitly:

        policy = PolicyDocument(service_level=load_mock_service_level_policy())
    """
    source = Path(path) if path is not None else DEFAULT_MOCK_PATH
    if not source.is_file():
        raise ConfigurationError(
            f"mock service-level fixture not found at {source}. This file is "
            "development-only and is never required for the app to start."
        )

    with source.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    raw_matrix = document.get("service_level_by_criticality") or {}
    if not raw_matrix:
        raise ConfigurationError(
            f"{source} has no service_level_by_criticality section -- "
            "nothing to mock."
        )

    entries: list[tuple[ServiceLevelKey, float]] = []
    for raw_tier, level in raw_matrix.items():
        try:
            tier = Criticality(str(raw_tier).strip().upper())
        except ValueError as exc:
            raise ConfigurationError(
                f"{source}: {raw_tier!r} is not one of the five ZMM065 tiers "
                f"({', '.join(t.value for t in Criticality)})"
            ) from exc
        entries.append((ServiceLevelKey(criticality=tier), float(level)))

    return ServiceLevelPolicy(matrix=tuple(entries))


def load_mock_max_stock_policy(path: Path | str | None = None) -> MaxStockStrategy:
    """Build a :class:`MaxStockStrategy` from the dev-fixture YAML.

        DEV/TEST ONLY -- NOT VZI PRODUCTION POLICY

    The Max Stock counterpart of :func:`load_mock_service_level_policy`, and
    contained the same way. Solution Design Rule 2 requires the formula to be
    agreed with Vedanta and says the choice "is configuration, not a coding
    decision" -- so this selects an already-implemented strategy through the
    already-existing :class:`MaxStockStrategy` schema, and introduces no new
    algorithm. ``app.initiatives.i7.inventory.max_stock`` is untouched: the
    arithmetic is its existing, already-tested
    ``ReviewPeriodMaxStockStrategy`` (``Max = ROP + D_rate x T``).

    Raises :class:`ConfigurationError` if the file is missing, names a
    strategy that is not implemented, or keys a review period by something
    other than one of the five ZMM065 tiers -- a typo must fail loudly rather
    than silently yielding a policy that blocks (or, worse, one that computes
    under a strategy nobody chose).

    Not wired into :class:`~app.initiatives.i7.policy.document.PolicyDocument`'s
    default construction. A caller opts in explicitly:

        policy = PolicyDocument(max_stock=load_mock_max_stock_policy())
    """
    source = Path(path) if path is not None else DEFAULT_MAX_STOCK_MOCK_PATH
    if not source.is_file():
        raise ConfigurationError(
            f"mock Max Stock fixture not found at {source}. This file is "
            "development-only and is never required for the app to start."
        )

    with source.open("r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}

    strategy = document.get("strategy")
    if not strategy:
        raise ConfigurationError(
            f"{source} names no strategy -- nothing to mock."
        )
    strategy = str(strategy).strip()

    # Checked against the implemented strategies rather than accepted on
    # trust: strategy_for() answers an unrecognised name with
    # NotConfiguredMaxStockStrategy, so a typo here would otherwise surface
    # as a silent "no Max Stock" thousands of rows later.
    from app.initiatives.i7.inventory.max_stock import STRATEGIES

    if strategy not in STRATEGIES:
        raise ConfigurationError(
            f"{source}: {strategy!r} is not an implemented Max Stock strategy "
            f"({', '.join(sorted(STRATEGIES))})"
        )

    raw_periods = document.get("review_period_months_by_criticality") or {}
    periods: list[tuple[Criticality, float]] = []
    for raw_tier, months in raw_periods.items():
        try:
            tier = Criticality(str(raw_tier).strip().upper())
        except ValueError as exc:
            raise ConfigurationError(
                f"{source}: {raw_tier!r} is not one of the five ZMM065 tiers "
                f"({', '.join(t.value for t in Criticality)})"
            ) from exc
        periods.append((tier, float(months)))

    if strategy == "review_period" and not periods:
        raise ConfigurationError(
            f"{source} selects the review-period strategy but supplies no "
            "review_period_months_by_criticality -- every material would "
            "report NOT_CONFIGURED, which is what the fixture exists to avoid."
        )

    return MaxStockStrategy(
        strategy=strategy, review_period_months=tuple(periods)
    )


def default_policy():
    """The policy ``run_forecasting`` / ``run_inventory_calculations`` /
    ``run_oar_similarity`` / ``generate_recommendations`` fall back to when no
    caller supplies one -- still :class:`PolicyDocument` with an empty
    ``service_level`` and an unchosen ``max_stock`` in every environment,
    UNLESS ``Settings.i7_dev_mock_service_level`` or
    ``Settings.i7_dev_mock_max_stock`` is explicitly set (opt-in env vars,
    false everywhere by default including production; see
    ``I7_DEV_MOCK_SERVICE_LEVEL`` and ``I7_DEV_MOCK_MAX_STOCK`` in
    ``.env.example``).

    This is the one place the mocks reach a real pipeline run without a
    caller passing them in by hand -- and each still requires a developer to
    have deliberately flipped its own flag, so a production deployment (which
    never sets either) behaves exactly as it did before this function
    existed. The two flags are independent: the service-level mock alone
    unblocks safety stock and ROP, and the Max Stock mock alone unblocks
    nothing, because Max Stock is itself computed from ROP.
    """
    from app.core.config import get_settings
    from app.initiatives.i7.policy.document import PolicyDocument

    settings = get_settings()
    overrides = {}
    if settings.i7_dev_mock_service_level:
        overrides["service_level"] = load_mock_service_level_policy()
    if settings.i7_dev_mock_max_stock:
        overrides["max_stock"] = load_mock_max_stock_policy()
    if not overrides:
        return PolicyDocument()
    return PolicyDocument(**overrides)
