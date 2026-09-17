"""Development-only policy fixtures. Never loaded by default.

:mod:`app.initiatives.i7.policy.unresolved` is explicit that the
Criticality x Circuit service-level matrix must come from a Vedanta sign-off,
not from code, and that an unsigned matrix should block rather than guess.
This module does not relax that rule -- it gives local development a way to
exercise the chain that rule gates (LightGBM's target quantile, the Z-factor,
safety stock, ROP) without the real matrix, while keeping the mock:

* out of the default construction path (``PolicyDocument()`` never calls
  this module; a caller must explicitly opt in),
* in a config file, not a code literal, so a reviewer sees the numbers were
  never actually chosen "in" the code, and
* clearly, unmissably labelled at every layer -- the YAML file says so, this
  module's docstring says so, and the resulting :class:`PolicyDocument`
  keeps ``status=DRAFT`` unless a caller separately marks it SIGNED, so
  :meth:`PolicyDocument.require_ready_for_recommendations` still blocks real
  recommendation output by default.
"""

from pathlib import Path

import yaml

from app.initiatives.i7.contracts.enums import Criticality
from app.initiatives.i7.errors import ConfigurationError
from app.initiatives.i7.policy.unresolved import ServiceLevelKey, ServiceLevelPolicy

DEFAULT_MOCK_PATH = (
    Path(__file__).resolve().parents[4] / "config" / "dev" / "i7_service_level_matrix.yaml"
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


def default_policy():
    """The policy ``run_forecasting``/``run_inventory_calculations`` fall back
    to when no caller supplies one -- still :class:`PolicyDocument` with an
    empty ``service_level`` in every environment, UNLESS
    ``Settings.i7_dev_mock_service_level`` is explicitly set (an opt-in env
    var, false everywhere by default including production; see
    ``I7_DEV_MOCK_SERVICE_LEVEL`` in ``.env.example``).

    This is the one place the mock reaches a real pipeline run without a
    caller passing it in by hand -- and it still requires a developer to have
    deliberately flipped that flag, so a production deployment (which never
    sets it) behaves exactly as it did before this function existed.
    """
    from app.core.config import get_settings
    from app.initiatives.i7.policy.document import PolicyDocument

    settings = get_settings()
    if not settings.i7_dev_mock_service_level:
        return PolicyDocument()
    return PolicyDocument(service_level=load_mock_service_level_policy())
