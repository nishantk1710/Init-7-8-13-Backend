"""Criticality from MARC-ZZCRITIC -- registered, but not available today.

**Status: the field exists in SAP; the CPI service does not expose it.**

What the repository actually proves, and nothing beyond it:

* ``data-generator/discovery/marc_changes_summary.txt`` records one MARC change
  document for field ``ZZCRITIC`` (change indicator ``U``, count 1). So a
  Z-append named ZZCRITIC is real and maintained on MARC in the live system,
  alongside ``ZZSTYPE`` (562 changes) and ``ZZPURCLASS`` (1).
* The OData metadata for both services -- ``ZVZI_KPI02_SHARED_SRV`` and
  ``ZMM_KPI02_SRV`` -- declares **no property whose name begins with Zz**.
  ``MaterialPlantSet`` exposes exactly twelve: Matnr, Werks, Lvorm, Dismm,
  Dispo, Plifz, Webaz, Minbe, Eisbe, Bstmi, Bstma, Mabst.
* No pulled ``MaterialPlantSet`` row carries a ZZ field, and the change-document
  snapshot in ``generated/sap/ChangeDocItemSet.csv`` contains no ZZCRITIC row.

What is therefore **unknown**: whether ZZCRITIC holds the criticality tier at
all, what vocabulary it uses, and whether it agrees with ZMM065. A one-count
change document proves the field exists; it says nothing about its contents.

What would make it primary, in order:

1. VZI adds ``Zzcritic`` to the ``MaterialPlant`` projection in the CPI generic
   consumption iFlow. This is an SAP/CPI change -- it cannot be done from here,
   and no filter or ``$select`` trick reaches an unprojected field.
2. Confirm the value domain against ZMM065 on overlapping material-plants.
   ``ChangeDocItemSet`` already exposes ``Fname``, ``Value_old`` and
   ``Value_new``, so the history is checkable once the field is projected.
3. Implement :meth:`ZzcriticCriticalitySource.get` against ``MaterialPlantSet``
   and map its vocabulary with ``parse_tier`` (or an explicit translation, if it
   turns out not to speak ZMM065's five tiers).

Until step 1 lands this source answers nothing, and the configured fallback
supplies every value -- visibly, via ``CriticalityResult.is_fallback``. That is
deliberate: selecting ``CRITICALITY_SOURCE=zzcritic`` today is safe, reports
honestly, and becomes correct the moment the field is exposed, with no change to
any consumer.
"""

from __future__ import annotations

from app.core.criticality import CriticalityResult, CriticalitySource
from app.core.logging import get_logger

logger = get_logger(__name__)

SOURCE_NAME = "zzcritic"

#: The MARC field this source is waiting on.
SAP_FIELD = "ZZCRITIC"

#: The entity set it would have to appear on, and what that set exposes today.
ENTITY_SET = "MaterialPlantSet"
EXPOSED_PROPERTIES = (
    "Matnr",
    "Werks",
    "Lvorm",
    "Dismm",
    "Dispo",
    "Plifz",
    "Webaz",
    "Minbe",
    "Eisbe",
    "Bstmi",
    "Bstma",
    "Mabst",
)

#: Whether the field is confirmed to carry criticality. Flip this only on
#: measured evidence from live SAP, never to make a test or a plan go green.
CONFIRMED = False

_UNAVAILABLE = (
    f"{SAP_FIELD} is not exposed by the CPI service: {ENTITY_SET} projects only "
    f"{len(EXPOSED_PROPERTIES)} properties and none is a Z-append. A MARC change "
    f"document proves the field exists in SAP, but its contents are unconfirmed."
)


class ZzcriticCriticalitySource(CriticalitySource):
    """The preferred source once SAP exposes it. Answers nothing until then.

    Returns an empty result rather than raising, so a caller that selects it
    without a fallback degrades to "no criticality known" instead of failing --
    the same shape as a material the source has simply never heard of.
    """

    name = SOURCE_NAME

    def get(self, sap_material_number: str, sap_plant_code: str | None = None) -> CriticalityResult:
        return CriticalityResult(
            sap_material_number=(sap_material_number or "").strip(),
            sap_plant_code=(sap_plant_code or "").strip() or None,
            tier=None,
            source=self.name,
            reason=_UNAVAILABLE,
        )

    def check_connection(self) -> None:
        """Always reports unavailable -- there is nothing reachable to check.

        Raising here is what makes ``FallbackCriticalitySource`` report healthy
        on the fallback alone, and what would make a readiness probe tell the
        truth about this source.
        """
        from app.core.criticality import CriticalityNotConfiguredError

        raise CriticalityNotConfiguredError(_UNAVAILABLE)
