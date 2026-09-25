"""I07 inventory calculation engine.

    Demand Forecast (Phase 4)
           |
    Lead-Time Analysis
           |
    Service-Level Policy  ->  Z
           |
    Safety Stock
           |
    ROP
           |
    Max Stock

**ML and statistical models predict demand. These formulas calculate SS, ROP and
Max.** The separation is the Solution Design's, and it is what keeps the
stocking numbers auditable: every one can be re-derived by hand from the trace
stored beside it. No model output is ever a stocking parameter.

| Module | Responsibility |
| --- | --- |
| ``lead_time`` | PO durations, fallback tiers, sigma_LT |
| ``variability`` | D_avg and sigma_D over all periods, zeros included |
| ``service_level`` | signed matrix -> service level -> Z |
| ``safety_stock`` | Path A (normal) and Path B (compound Poisson) |
| ``monte_carlo`` | simulated lead-time demand for LUMPY |
| ``rop`` | E[LTD] + SS |
| ``max_stock`` | pluggable strategies behind a policy gate |
| ``service`` | orchestration and persistence |

Two business gates are closed on the current configuration and block by design
rather than defaulting: the service-level matrix is unsigned, so no Z exists and
no safety stock is produced; and no Max Stock strategy is signed, so no maximum
is produced. Cold-start materials defer to the Phase 6 OAR similarity engine --
nothing here invents a neighbour.
"""

from app.initiatives.i7.inventory.service import (
    InventoryRunResult,
    calculate_one,
    run_inventory_calculations,
)
from app.initiatives.i7.inventory.types import (
    FORMULA_VERSION,
    CalculationStatus,
    DemandVariabilityResult,
    InventoryCalculationResult,
    LeadTimeMethod,
    LeadTimeResult,
    MaxStockResult,
    RopResult,
    SafetyStockResult,
    ServiceLevelResult,
)

__all__ = [
    "FORMULA_VERSION",
    "CalculationStatus",
    "DemandVariabilityResult",
    "InventoryCalculationResult",
    "InventoryRunResult",
    "LeadTimeMethod",
    "LeadTimeResult",
    "MaxStockResult",
    "RopResult",
    "SafetyStockResult",
    "ServiceLevelResult",
    "calculate_one",
    "run_inventory_calculations",
]
