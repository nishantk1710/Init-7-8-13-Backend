"""Material criticality -- the shared source-agnostic port (W3.4).

I07, I08 and I13 all need to know how critical a material is, and all three need
the *same* answer. So the lookup lives here, once, and every initiative reaches
it through :func:`get_criticality_source`. None of them knows, or may depend on,
where the value came from.

    I07 --+
    I08 --+--> CriticalitySource --> the configured provider
    I13 --+

**Criticality is per material-plant, not per material.** The same material can be
CRITICAL at one plant and NORMAL at another -- 561 materials appear in both the
Black Mountain and Gamsberg deliveries -- and collapsing that to one value per
material would either invent a tier or hide a real one. This mirrors the OAR
scope rule, which is evaluated per material-plant for the same reason.

**The five tiers are ZMM065's own vocabulary**, not a 1..5 ordinal. The FRS
designates them authoritative, and they are measured facts about the delivered
data, not a modelling choice -- see :class:`CriticalityTier`.

**An unrecognised tier never becomes a known one.** Every provider returns
``None`` for a value it does not recognise rather than defaulting to NORMAL.
Criticality drives the service level, so inventing one would silently change how
much stock the platform recommends holding.

The structure here is the one ``app.core.storage`` and ``app.core.ai`` already
use: an error hierarchy that separates "not configured" from "configured but
failing", an ABC port, a factory that is the only place the config-to-provider
mapping lives, and a cached accessor with a reset hook for tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# --- Errors ---------------------------------------------------------------
#
# Mirrors app.core.storage: "not configured" is a distinct type from "configured
# but failing", because they need different fixes and readiness reports them
# separately.


class CriticalityError(RuntimeError):
    """Base class for every criticality failure raised by this package."""


class CriticalityNotConfiguredError(CriticalityError):
    """The selected source has nothing to read -- no database, no credentials."""


class UnknownCriticalitySourceError(CriticalityError):
    """CRITICALITY_SOURCE names a provider that does not exist."""


# --- The tiers ------------------------------------------------------------


class CriticalityTier(str, Enum):
    """The five ZMM065 criticality tiers.

    These are the exact values carried in the delivered extracts -- verified
    across both plants, where they are the *only* five that occur:

        NORMAL      7301 (BMM) + 5392 (GB)
        OBSOLETE    2802       +  326
        CRITICAL     265       +  559
        IMPACT       109       + 1011
        INSURANCE     44       +  161

    Deliberately not an ordinal 1..5. The tiers are a nominal SAP vocabulary and
    are not uniformly ordered -- OBSOLETE is not "more critical" than NORMAL, it
    is a different kind of statement about the material. Code that needs a
    ranking must state its own, which is what :data:`SEVERITY_ORDER` is for.
    """

    CRITICAL = "CRITICAL"
    IMPACT = "IMPACT"
    INSURANCE = "INSURANCE"
    NORMAL = "NORMAL"
    OBSOLETE = "OBSOLETE"


#: Severity ranking, most severe first. Used only where a caller must break a
#: tie *and* has said so explicitly. It is not a property of the tiers
#: themselves, and no provider applies it while reading a single plant's row.
SEVERITY_ORDER: tuple[CriticalityTier, ...] = (
    CriticalityTier.CRITICAL,
    CriticalityTier.IMPACT,
    CriticalityTier.INSURANCE,
    CriticalityTier.NORMAL,
    CriticalityTier.OBSOLETE,
)


def parse_tier(value: str | None) -> CriticalityTier | None:
    """Map raw source text to a tier, or ``None`` if it is not one of the five.

    Case- and whitespace-insensitive, because the extracts are human-touched
    workbooks. Anything unrecognised returns ``None`` rather than a default:
    an unexpected tier is not silently coerced to NORMAL, since criticality
    drives the service level and a wrong tier is worse than a missing one.
    """
    if value is None:
        return None
    cleaned = str(value).strip().upper()
    if not cleaned:
        return None
    try:
        return CriticalityTier(cleaned)
    except ValueError:
        return None


# --- The result -----------------------------------------------------------


@dataclass(frozen=True)
class CriticalityResult:
    """One material-plant's criticality, and where it came from.

    Provenance is part of the answer, not a logging detail. A caller that cannot
    tell a primary-source hit from a fallback cannot honestly report how much to
    trust the number, and W3.4 requires the fallback be *observable*.
    """

    sap_material_number: str
    sap_plant_code: str | None
    tier: CriticalityTier | None
    source: str
    """Name of the provider that actually answered, e.g. ``"zmm065"``."""

    is_fallback: bool = False
    """True when the configured primary source could not answer and a fallback did."""

    reason: str | None = None
    """Why there is no tier, or why the fallback ran. ``None`` on a clean primary hit."""

    @property
    def found(self) -> bool:
        """Whether a usable tier was resolved."""
        return self.tier is not None


# --- The port -------------------------------------------------------------


class CriticalitySource(ABC):
    """One criticality provider. One implementation per backing system.

    Deliberately small: every method has to be implemented, and proven by the
    shared conformance suite, once per provider.
    """

    name: str = "unknown"

    @abstractmethod
    def get(self, sap_material_number: str, sap_plant_code: str | None = None) -> CriticalityResult:
        """Resolve one material-plant's criticality.

        Never raises for an absent material: a material this source has never
        heard of is an ordinary, expected answer, and is returned as a result
        with ``tier=None`` and a ``reason``. Raises only when the *source
        itself* is unusable.
        """

    @abstractmethod
    def check_connection(self) -> None:
        """Prove the source is reachable and usable. Raises on failure.

        Used by readiness, and the reason a criticality outage can be reported
        as honestly as a database one.
        """


# --- Fallback -------------------------------------------------------------


class FallbackCriticalitySource(CriticalitySource):
    """Try a primary source, then a fallback, and say which one answered.

    This is how ``CRITICALITY_SOURCE=zzcritic`` behaves today: ZZCRITIC is not
    exposed by the CPI service (see ``app.integrations.criticality.zzcritic``),
    so every lookup falls through to ZMM065 and is *marked* as having done so.
    The platform keeps working and the degradation stays visible, rather than
    the configuration silently doing nothing.
    """

    def __init__(self, primary: CriticalitySource, fallback: CriticalitySource) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+{fallback.name}"

    def get(self, sap_material_number: str, sap_plant_code: str | None = None) -> CriticalityResult:
        try:
            result = self.primary.get(sap_material_number, sap_plant_code)
        except CriticalityError as exc:
            reason = f"{self.primary.name} unavailable: {exc}"
        else:
            if result.found:
                return result
            reason = result.reason or f"{self.primary.name} returned no value"

        demoted = self.fallback.get(sap_material_number, sap_plant_code)
        return CriticalityResult(
            sap_material_number=demoted.sap_material_number,
            sap_plant_code=demoted.sap_plant_code,
            tier=demoted.tier,
            source=demoted.source,
            is_fallback=True,
            reason=reason if demoted.found else f"{reason}; {demoted.reason}",
        )

    def check_connection(self) -> None:
        """Healthy when *either* source can answer -- that is what fallback means."""
        try:
            self.primary.check_connection()
        except CriticalityError:
            self.fallback.check_connection()


# --- Factory --------------------------------------------------------------

SOURCES = ("zmm065", "zzcritic")


def build_criticality_source(settings: Settings | None = None) -> CriticalitySource:
    """Construct the configured source. The only place that mapping lives.

    Exposed (rather than private) so tests can build a source without touching
    process-wide settings, the same way ``build_storage`` is.
    """
    settings = settings or get_settings()
    choice = (settings.criticality_source or "zmm065").strip().lower()

    if choice == "zmm065":
        from app.integrations.criticality.zmm065 import Zmm065CriticalitySource

        logger.info("Criticality source: zmm065 (local ZMM065 extracts).")
        return Zmm065CriticalitySource()

    if choice == "zzcritic":
        from app.integrations.criticality.zmm065 import Zmm065CriticalitySource
        from app.integrations.criticality.zzcritic import ZzcriticCriticalitySource

        # ZZCRITIC is unconfirmed and unexposed, so it is never used alone.
        # Pairing it with ZMM065 is what makes selecting it safe to do today.
        logger.info("Criticality source: zzcritic, falling back to zmm065.")
        return FallbackCriticalitySource(
            primary=ZzcriticCriticalitySource(),
            fallback=Zmm065CriticalitySource(),
        )

    raise UnknownCriticalitySourceError(
        f"Unknown CRITICALITY_SOURCE {choice!r}. Supported: {', '.join(SOURCES)}."
    )


@lru_cache
def get_criticality_source() -> CriticalitySource:
    """The process-wide criticality source, built on first use.

    Lazy for the same reason the database engine and storage adapter are:
    importing the application must not require criticality to be resolvable, or
    the liveness endpoint and the test suite stop working on a bare machine.
    """
    return build_criticality_source()


def reset_criticality_cache() -> None:
    """Drop the cached source. For tests that change CRITICALITY_SOURCE."""
    get_criticality_source.cache_clear()
