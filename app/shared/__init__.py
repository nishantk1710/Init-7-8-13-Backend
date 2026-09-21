"""Code genuinely shared by more than one initiative.

Anything an initiative needs from another initiative belongs here instead of
being imported across the I07/I08/I13 boundary.

**Criticality (W3.4)** is the first such thing. All three initiatives need to
know how critical a material is, and all three must get the same answer, so the
lookup exists once and every initiative reaches it through here::

    from app.shared import get_criticality_source

    result = get_criticality_source().get(sap_material_number, sap_plant_code)
    if result.found:
        tier = result.tier          # CriticalityTier.CRITICAL, ...

The port itself lives in ``app.core.criticality`` alongside the other
infrastructure ports (storage, AI, database); these re-exports are the
initiative-facing surface, so an initiative never has to know which provider is
configured or where the data physically comes from.

Criticality is per **material-plant**: the same material can be CRITICAL at one
plant and NORMAL at another, so always pass the plant.
"""

from app.shared.numbers import plain
from app.core.criticality import (
    SEVERITY_ORDER,
    CriticalityError,
    CriticalityNotConfiguredError,
    CriticalityResult,
    CriticalitySource,
    CriticalityTier,
    get_criticality_source,
    parse_tier,
)

__all__ = [
    "SEVERITY_ORDER",
    "CriticalityError",
    "CriticalityNotConfiguredError",
    "CriticalityResult",
    "CriticalitySource",
    "CriticalityTier",
    "get_criticality_source",
    "parse_tier",
    "plain",
]
