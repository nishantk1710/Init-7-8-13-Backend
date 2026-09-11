"""Domain-friendly config objects built from ``Settings``.

Every I13 threshold lives in ``app.core.config.Settings`` (generic platform
configuration); this module just gives the domain code typed, named access
to them so no service re-reads ``settings.i13_aging_fast_max_days`` (or
worse, repeats the threshold as a literal) at each call site.
"""

from dataclasses import dataclass

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class AgingThresholds:
    fast_max_days: int
    slow_max_days: int


@dataclass(frozen=True)
class WatchConfig:
    consumption_window_months: int
    gr_not_issued_threshold_days: int


@dataclass(frozen=True)
class ExceptionConfig:
    plan_breach_grace_days: int


@dataclass(frozen=True)
class ReclassificationConfig:
    min_consumption_count: int


@dataclass(frozen=True)
class ReconciliationConfig:
    tolerance_pct: float


@dataclass(frozen=True)
class I13Config:
    aging: AgingThresholds
    watch: WatchConfig
    exceptions: ExceptionConfig
    reclassification: ReclassificationConfig
    reconciliation: ReconciliationConfig


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
            min_consumption_count=settings.i13_reclass_min_consumption_count
        ),
        reconciliation=ReconciliationConfig(tolerance_pct=settings.i13_reconciliation_tolerance_pct),
    )


def get_i13_config() -> I13Config:
    """FastAPI dependency: the process-wide I13 config snapshot."""
    return build_i13_config(get_settings())
