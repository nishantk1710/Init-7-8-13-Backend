"""Domain-friendly config objects built from ``Settings``.

Every I13 threshold lives in ``app.core.config.Settings`` (generic platform
configuration); this module just gives the domain code typed, named access
to them so no service re-reads ``settings.i13_aging_fast_max_days`` (or
worse, repeats the threshold as a literal) at each call site.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.core.config import Settings, get_settings
from app.core.criticality import CriticalityTier


@dataclass(frozen=True)
class AgingThresholds:
    fast_max_days: int
    slow_max_days: int

    def __post_init__(self) -> None:
        if self.fast_max_days <= 0 or self.slow_max_days <= 0:
            raise ValueError(
                f"i13 aging thresholds must be positive days (fast_max_days={self.fast_max_days}, "
                f"slow_max_days={self.slow_max_days})"
            )
        if self.fast_max_days >= self.slow_max_days:
            raise ValueError(
                f"i13_aging_fast_max_days ({self.fast_max_days}) must be less than "
                f"i13_aging_slow_max_days ({self.slow_max_days})"
            )


@dataclass(frozen=True)
class WatchConfig:
    consumption_window_months: int
    gr_not_issued_threshold_days: int

    def __post_init__(self) -> None:
        if self.consumption_window_months <= 0:
            raise ValueError(f"i13_consumption_window_months must be positive, got {self.consumption_window_months}")
        if self.gr_not_issued_threshold_days <= 0:
            raise ValueError(
                f"i13_gr_not_issued_threshold_days must be positive, got {self.gr_not_issued_threshold_days}"
            )


@dataclass(frozen=True)
class ExceptionConfig:
    plan_breach_grace_days: int


@dataclass(frozen=True)
class EscalationConfig:
    """W6.6: how long an ACT exception's requester has to confirm before it
    escalates to the HOD, and today's local/config HOD routing table. The
    30-day GRNI fallback for no-plan cases reuses ``WatchConfig.
    gr_not_issued_threshold_days`` -- it is not duplicated here."""

    requester_response_days: int
    hod_recipients_raw: str

    def __post_init__(self) -> None:
        if self.requester_response_days <= 0:
            raise ValueError(f"i13_requester_response_days must be positive, got {self.requester_response_days}")


@dataclass(frozen=True)
class ReclassificationConfig:
    min_consumption_count: int
    critical_tiers: frozenset[CriticalityTier]


@dataclass(frozen=True)
class ReconciliationConfig:
    tolerance_pct: float


@dataclass(frozen=True)
class AttributionConfig:
    """W6.4: whether cost-centre enrichment runs at all. Reservation/
    requester/order attribution is never gated -- only cost centre, per the
    FRS ("cost-centre path behind a config flag")."""

    cost_centre_enabled: bool


@dataclass(frozen=True)
class QuantitySuggestionConfig:
    """W7.4: the months-of-cover basis the reservation-time quantity
    suggestion is computed on.

    ``cover_ceiling_months`` and ``minimum_history_count`` are ``None`` until
    VZI supplies them (FRS §10 carries both as open). That is a first-class
    state, not a missing default: with either unset the engine declines with
    NOT_CONFIGURED rather than inventing a purchase figure -- see
    ``app.initiatives.i13.quantity_suggestion``.

    ``enabled`` is a separate master gate, so "switched off here" and "the
    business has not given us the numbers" stay distinguishable in the
    persisted reason code."""

    enabled: bool
    cover_ceiling_months: Decimal | None
    minimum_history_count: int | None
    lookback_months: int

    def __post_init__(self) -> None:
        if self.cover_ceiling_months is not None and self.cover_ceiling_months <= 0:
            raise ValueError(
                f"i13_qty_cover_ceiling_months must be positive months when set, got {self.cover_ceiling_months}"
            )
        if self.minimum_history_count is not None and self.minimum_history_count < 0:
            raise ValueError(
                f"i13_qty_minimum_history_count cannot be negative, got {self.minimum_history_count}"
            )
        if self.lookback_months <= 0:
            raise ValueError(f"i13_qty_lookback_months must be positive, got {self.lookback_months}")

    @property
    def thresholds_configured(self) -> bool:
        """Whether VZI's two open values are both present. ``enabled`` is
        deliberately not part of this -- see the class docstring."""
        return self.cover_ceiling_months is not None and self.minimum_history_count is not None


@dataclass(frozen=True)
class I13Config:
    aging: AgingThresholds
    watch: WatchConfig
    exceptions: ExceptionConfig
    reclassification: ReclassificationConfig
    reconciliation: ReconciliationConfig
    attribution: AttributionConfig
    escalation: EscalationConfig
    quantity_suggestion: QuantitySuggestionConfig


def build_i13_config(settings: Settings) -> I13Config:
    return I13Config(
        aging=AgingThresholds(
            fast_max_days=settings.i13_aging_fast_max_days,
            slow_max_days=settings.i13_aging_slow_max_days,
        ),
        watch=WatchConfig(
            consumption_window_months=settings.i13_consumption_window_months,
            gr_not_issued_threshold_days=settings.i13_gr_not_issued_threshold_days,
        ),
        exceptions=ExceptionConfig(plan_breach_grace_days=settings.i13_plan_breach_grace_days),
        reclassification=ReclassificationConfig(
            min_consumption_count=settings.i13_reclass_min_consumption_count,
            critical_tiers=settings.i13_reclass_critical_tier_set,
        ),
        reconciliation=ReconciliationConfig(tolerance_pct=settings.i13_reconciliation_tolerance_pct),
        attribution=AttributionConfig(cost_centre_enabled=settings.i13_cost_centre_attribution_enabled),
        escalation=EscalationConfig(
            requester_response_days=settings.i13_requester_response_days,
            hod_recipients_raw=settings.i13_hod_recipients,
        ),
        quantity_suggestion=QuantitySuggestionConfig(
            enabled=settings.i13_qty_suggestion_enabled,
            cover_ceiling_months=settings.i13_qty_cover_ceiling_months,
            minimum_history_count=settings.i13_qty_minimum_history_count,
            lookback_months=settings.i13_qty_lookback_months,
        ),
    )


def get_i13_config() -> I13Config:
    """FastAPI dependency: the process-wide I13 config snapshot."""
    return build_i13_config(get_settings())
