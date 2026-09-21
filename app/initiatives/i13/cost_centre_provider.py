"""W6.4 cost-centre attribution adapter.

The current Initiative 13 FRS lists EKKN (PO account assignment) and AUFK
(order/cost-centre context) as *proposed*, not-yet-confirmed additions --
W6.4 must not depend on them. This module is the seam a future EKKN/AUFK-
backed provider plugs into later without any change to
``consumption_attribution.py``'s resolver.

Today, no deterministic cost-centre source exists anywhere in this codebase
(no Kostl/EKKN/AUFK extract is loaded -- see ``app/seed/manifest.py``), so
``NullCostCentreProvider`` is the only implementation and always returns
``None``. It is never invoked unless ``I13Config.attribution.cost_centre_enabled``
is true (see ``config.py``), and returning ``None`` never turns an otherwise
valid attribution into an error -- see ``consumption_attribution.py``.
"""

from __future__ import annotations

from typing import Protocol


class CostCentreProvider(Protocol):
    """Deterministic cost-centre lookup for one reservation line. Must
    return ``None`` (never guess/interpolate) when no exact reference
    resolves."""

    def get_cost_centre(
        self, *, reservation_number: str, reservation_item: str, order_number: str | None
    ) -> str | None: ...


class NullCostCentreProvider:
    """No deterministic cost-centre source is wired in yet -- always
    ``None``, never a guess."""

    def get_cost_centre(
        self, *, reservation_number: str, reservation_item: str, order_number: str | None
    ) -> str | None:
        return None
