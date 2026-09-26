"""W6.5 HOD-justified reclassification evidence adapter.

The FRS treats an HOD-approved justification request, logged through an
Initiative 13 exception/justification workflow, as reclassification
evidence in its own right (SOP 3.1.1, indicator 3). That workflow (W6.6)
does not exist in this codebase yet -- no ``Justification``/HOD model or
table is seeded or built anywhere (see ``exceptions.py``, which only models
PLAN_BREACH/NO_PLAN/GR_NOT_ISSUED_30_DAY exceptions, none of them an
HOD-approval concept).

This module is the seam a future W6.6-backed provider plugs into without
any change to ``reclassification.py``'s evaluator. Today, with no
deterministic source wired in, ``NullHodJustificationProvider`` is the only
implementation and always returns ``None`` -- the same "unknown source
availability is not the same as false" posture as the criticality module's
``tier=None`` and W6.4's ``NullCostCentreProvider``. Returning ``None``
never claims a material has no HOD justification; it says this provider has
nothing to say either way.
"""

from __future__ import annotations

from typing import Protocol


class HodJustificationProvider(Protocol):
    """Whether an HOD-approved justification request exists for one
    material-plant. Must return ``None`` (never guess) when the source has
    no answer -- ``None`` is UNKNOWN, not ``False``."""

    def get_hod_justification(self, *, material: str, plant: str) -> bool | None: ...


class NullHodJustificationProvider:
    """No HOD-justification workflow (W6.6) is wired in yet -- always
    ``None``, never a fabricated ``False``."""

    def get_hod_justification(self, *, material: str, plant: str) -> bool | None:
        return None
