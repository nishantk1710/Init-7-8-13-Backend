"""Lead-time source selection: Initiative 11 Z-program, or the MARC-PLIFZ interim.

The FRS and Formula Reference disagree on who computes lead time (Phase 0, R5):
the Formula Reference has I07 deriving it from PO and GR dates; the FRS names a
single Initiative 11 Z-program, shared by I07 and I11, as the source, and lists
building that program as out of scope for I07. This module does not resolve that
disagreement -- it only decides, for one material-plant, which already-existing
figure to surface, and tags the answer with where it came from.

    Initiative 11 Z-program output available?
        yes -> use it, source = I11_PROGRAM
        no  -> fall back to MARC-PLIFZ, source = PLANNED_DELIVERY_TIME

The I11 provider is a stub. No Initiative 11 output exists anywhere in this
repository or its staged data, so this module never fabricates one -- it reports
unavailability and the fallback runs. Wiring in the real output later is a matter
of implementing :class:`I11LeadTimeProvider.get`, not changing any caller.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.initiatives.i7.contracts.enums import LeadTimeSource
from app.initiatives.i7.contracts.identity import MaterialPlantKey


@dataclass(frozen=True)
class LeadTimeProviderResult:
    """One material-plant's lead time, and which source answered.

    Deliberately minimal: this is a *selection* result, not the full PO
    statistical trace that :mod:`app.initiatives.i7.inventory.lead_time`
    produces. Phase 5 keeps computing its own detailed ``LeadTimeResult``;
    this module only decides, and records, which input feeds it.
    """

    source: LeadTimeSource
    lead_time_days: Decimal | None
    detail: str


class LeadTimeSourceProvider(Protocol):
    """One candidate source of lead time for a material-plant."""

    def get(self, key: MaterialPlantKey) -> LeadTimeProviderResult | None:
        """Return a result if this source has an answer, else ``None``.

        Never raises for an ordinary "no data" case -- that is an expected
        state for every provider here, not a failure.
        """


class I11LeadTimeProvider:
    """The Initiative 11 Z-program output. Not yet available.

    No Initiative 11 output is exposed anywhere in this build -- there is no
    entity set, no staged table, no Z-program result to read. ``get`` therefore
    always returns ``None`` rather than inventing a figure. This class exists so
    the *shape* of the dependency is visible in code and the eventual wiring is a
    body change here, not a new caller elsewhere.
    """

    def get(self, key: MaterialPlantKey) -> LeadTimeProviderResult | None:
        return None


class MarcPlifzLeadTimeProvider:
    """The interim fallback: SAP's planned delivery time (MARC-PLIFZ).

    Reads the same ``planned_delivery_time_days`` value already staged in
    :class:`app.models.i7_staging.StagedMaterialPlant` and already consumed by
    :func:`app.initiatives.i7.inventory.lead_time.analyse` as its own 0-1 PO
    fallback. This provider does not duplicate that arithmetic -- it exists so a
    caller working at the feature-selection level can ask "which lead time, and
    from where?" without reaching into Phase 5's PO-statistics engine.
    """

    def get(
        self, key: MaterialPlantKey, planned_delivery_time_days: int | None
    ) -> LeadTimeProviderResult | None:
        if planned_delivery_time_days is None or planned_delivery_time_days <= 0:
            return None
        return LeadTimeProviderResult(
            source=LeadTimeSource.PLANNED_DELIVERY_TIME,
            lead_time_days=Decimal(planned_delivery_time_days),
            detail="MARC-PLIFZ interim fallback; no Initiative 11 output is available",
        )


def resolve_lead_time(
    key: MaterialPlantKey,
    planned_delivery_time_days: int | None,
    i11_provider: I11LeadTimeProvider | None = None,
    marc_plifz_provider: MarcPlifzLeadTimeProvider | None = None,
) -> LeadTimeProviderResult:
    """Pick a lead-time source for one material-plant: I11 first, MARC-PLIFZ next.

    Providers are injectable so tests can supply a fake I11 provider without
    waiting on the real integration, per the ``LeadTimeSourceProvider`` protocol.
    Returns a result even when neither source has an answer, so the caller
    always has a status to record rather than a missing value to special-case.
    """
    i11 = i11_provider or I11LeadTimeProvider()
    marc_plifz = marc_plifz_provider or MarcPlifzLeadTimeProvider()

    i11_result = i11.get(key)
    if i11_result is not None:
        return i11_result

    marc_result = marc_plifz.get(key, planned_delivery_time_days)
    if marc_result is not None:
        return marc_result

    return LeadTimeProviderResult(
        source=LeadTimeSource.PLANNED_DELIVERY_TIME,
        lead_time_days=None,
        detail=(
            "no Initiative 11 output and no usable MARC-PLIFZ value "
            "(missing or not positive)"
        ),
    )
