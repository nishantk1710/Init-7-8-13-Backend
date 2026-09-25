"""W7.1 -- which flow does this material get?

The BAdI pop-up knows the material and the plant. It does **not** know whether
that material is 80-series or OAR, which is the entire reason this module
exists and the reason the entry point cannot live under ``/api/i8`` or
``/api/i13``: the caller cannot choose.

Thin on purpose
---------------
Both halves of the predicate were already built and tested before WS7 started,
so this module calls them and does not re-implement either:

* **80-series (repairable)** -- ``is_eighty_series`` from
  ``app.initiatives.i8.material_number``, driven by ``I8Settings.series_prefixes``.
* **OAR (planned on demand)** -- ``classify_material_scope`` from
  ``app.shared.material_scope``, driven by ``Settings.i13_oar_mrp_types`` over
  MARC's ``DISMM``. That package's own scope note already named "any
  reservation-time assistant/router" as an intended consumer.

A router that re-derived either rule would drift from the register and the
exception queue within a release, and the two would then disagree about the same
material in front of a user.

The overlap, and why I08 wins
-----------------------------
Nothing prevents a material from being both 80-series *and* OAR -- the two rules
read different fields (the material number, and MARC's MRP type) and neither
excludes the other. So a precedence is needed, and picking one silently would
bury a business decision in a conditional.

**This is not an edge case. Measured against the seeded extract on 21-Sep, 2,294
of the 2,350 80-series material+plant rows in MARC -- 97.6% -- are also OAR.**
So precedence is not a tie-breaker for a rare collision; it is the rule that
decides which flow essentially every repairable spare gets. It deserved
measuring before being chosen, and it is recorded here so nobody has to re-derive
it to review the decision.

**I08 wins, and the OAR match is recorded rather than discarded**
(:attr:`RoutedFlow.also_matched`). The reasoning: the I08 intervention is the
stronger one. "A repair for this part is already open and the vendor has it" can
stop a purchase outright; the I13 flow's job is to size and record a purchase
that is going ahead anyway. Showing the weaker advice while the stronger one
existed would be the expensive way round.

**This is a decision, not a derivation, and it needs confirming** -- which is why
the losing match travels on the result instead of being dropped. If VZI wants
both flows, the data to build that is already on every routing decision.

No scope, no session
--------------------
A material that is neither gets :attr:`Flow.NONE` and **no session is minted**.
That is not a failure and must not be reported as one: the assistant has nothing
to say about a consumable, and minting a session to record silence would put
rows in an append-only table for every reservation the platform has no opinion
about.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from sqlalchemy.orm import Session

from app.initiatives.i8.config import I8Settings, get_i8_settings
from app.initiatives.i8.material_number import is_eighty_series, normalise
from app.integrations.sap.postgres_material import fetch_material_scope_index
from app.shared.material_scope import MaterialScope, classify_material_scope


class Flow(str, Enum):
    """Which conversation a reservation gets."""

    I08 = "i08"
    """80-series repairable. FR-5/6/7: is a repair already open for this?"""

    I13 = "i13"
    """OAR, planned on demand. FR-2/3/4: what is the cover, what is the plan?"""

    NONE = "none"
    """Neither. Nothing to say, and nothing is minted."""


@dataclass(frozen=True)
class RoutedFlow:
    """The routing decision, with everything it was decided from.

    Carries its inputs rather than just its answer. A requester who is shown the
    wrong flow -- or no flow -- asks "why?", and the answer has to be
    reconstructable from the record months later, when the MRP type in MARC may
    have changed and the configuration with it.
    """

    flow: Flow
    material_id: str
    """Normalised (leading zeros stripped) -- ruling 5.1. Never compare two raw
    material numbers."""

    plant: str

    eighty_series: bool
    """Whether the I08 predicate matched."""

    material_scope: MaterialScope
    """The I13 classification. ``EXCLUDED`` also covers "MARC has no row"."""

    mrp_type: str | None
    """The raw DISMM the scope was classified from. ``None`` where MARC has no
    row for this material at this plant -- which is 47% of rows today, and is a
    data gap rather than a classification."""

    also_matched: Flow | None = None
    """Set when both predicates matched and precedence decided it. The flow that
    did NOT run, kept so the decision stays visible."""

    @property
    def in_scope(self) -> bool:
        return self.flow is not Flow.NONE

    @property
    def reason(self) -> str:
        """One sentence, for the session record and for a person asking why."""
        if self.flow is Flow.I08:
            both = (
                " It is also OAR by MRP type, and the repairable flow takes "
                "precedence."
                if self.also_matched is Flow.I13
                else ""
            )
            return f"{self.material_id} is an 80-series repairable material.{both}"
        if self.flow is Flow.I13:
            return (
                f"{self.material_id} is OAR (planned on demand) at plant "
                f"{self.plant} -- MRP type {self.mrp_type}."
            )
        if self.mrp_type is None:
            return (
                f"{self.material_id} is not 80-series, and MARC has no MRP type "
                f"for it at plant {self.plant}, so it cannot be classified as "
                "OAR either. The assistant has nothing to say about it."
            )
        return (
            f"{self.material_id} is not 80-series, and MRP type "
            f"{self.mrp_type} is not in the OAR list. The assistant has nothing "
            "to say about it."
        )


def route(
    db: Session,
    material_id: str,
    plant: str,
    *,
    i8_config: I8Settings | None = None,
) -> RoutedFlow:
    """Decide the flow for one material at one plant.

    One query -- the MRP type for this material and plant. The 80-series test is
    pure and needs no database at all, so the query is only ever asked for the
    OAR half.

    ``i8_config`` is injectable because ``is_eighty_series`` takes it. The OAR
    vocabulary is deliberately **not** a parameter here: ``classify_material_scope``
    reads the process-wide settings itself, and threading a second copy through
    this function would create two ways to answer the same question. If that
    ever needs to be injectable it belongs on the shared policy, where every
    other caller would get it too.
    """
    i8_config = i8_config or get_i8_settings()

    # Normalised before anything compares it. An assistant opened against
    # '000000008000005632' and a register holding '8000005632' are about the
    # same part, and a router that cannot see that routes it to nothing.
    material_key = normalise(material_id) or ""
    plant_key = (plant or "").strip()

    eighty_series = is_eighty_series(material_key, i8_config)

    # Asked for with the NORMALISED number. Verified rather than assumed:
    # raw_marc holds material numbers with leading zeros already stripped (0 of
    # 45,409 rows begin with a zero, measured 21-Sep), so the normalised key is
    # the one that matches. Querying with the padded CPI form would miss every
    # row and route every material to NONE.
    scope_index = fetch_material_scope_index(db, material=material_key, plant=plant_key)
    mrp_type = scope_index.get((material_key, plant_key))
    material_scope = classify_material_scope(mrp_type)

    is_oar = material_scope is MaterialScope.OAR

    if eighty_series:
        flow = Flow.I08
        also_matched = Flow.I13 if is_oar else None
    elif is_oar:
        flow = Flow.I13
        also_matched = None
    else:
        flow = Flow.NONE
        also_matched = None

    return RoutedFlow(
        flow=flow,
        material_id=material_key,
        plant=plant_key,
        eighty_series=eighty_series,
        material_scope=material_scope,
        mrp_type=mrp_type,
        also_matched=also_matched,
    )
