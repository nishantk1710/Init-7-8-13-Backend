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

**Plant scope** is the other platform-wide fact every initiative shares: the
delivery covers plants 1300 and 1500 only. See ``app.shared.plant_scope`` --
never hard-code a plant code against it::

    from app.shared import IN_SCOPE_PLANTS, is_in_scope
"""

from app.shared.numbers import plain
from app.shared.plant_scope import (
    IN_SCOPE_PLANTS,
    PLANT_NAMES,
    is_in_scope,
    plant_name,
    sql_literals,
    sql_predicate,
)
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
    "IN_SCOPE_PLANTS",
    "PLANT_NAMES",
    "SEVERITY_ORDER",
    "CriticalityError",
    "CriticalityNotConfiguredError",
    "CriticalityResult",
    "CriticalitySource",
    "CriticalityTier",
    "get_criticality_source",
    "is_in_scope",
    "parse_tier",
    "plain",
    "plant_name",
    "sql_literals",
    "sql_predicate",
]
