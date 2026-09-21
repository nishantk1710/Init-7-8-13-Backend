"""Which entity set lands in which table.

Unlike the seed's manifest, this one is *derived* rather than written down, and
the difference is not laziness. The July delivery names its files four different
ways, so that mapping genuinely cannot be computed and had to be recorded by
hand. Here the mapping is mechanical -- ``MaterialPlantSet`` becomes
``odata_material_plant`` -- and deriving it means a set added to the discovery
snapshot appears here automatically instead of being silently skipped until
someone notices.

What is written down is only what cannot be derived: the handful of sets that
return no rows in this client, and the notes explaining why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.integrations.sap.contract import EntitySet, contract
from app.integrations.sap.filters import HONOURED, verdict_for

# Raw tables from the live service. The prefix keeps them apart from the seed's
# ``raw_`` tables, which hold the same SAP data under different column names.
TABLE_PREFIX = "odata_"

# Verified empty in this client during the 08-Sep discovery sweep. Still
# ingested -- an empty table is a fact worth landing, and the day SAP starts
# populating one of these we want the pipeline already pointed at it rather
# than discovering the omission months later.
KNOWN_EMPTY = frozenset(
    {
        "ReservationItemSet",
        "MaterialValuationSet",
        "MonthlyMovementStatisticSet",
    }
)

# ``$count`` returns HTTP 400 on these. Not fatal: the client falls back to
# page-until-short-page, so they read fine, it just cannot state a total up
# front and therefore cannot cross-check the row count against one.
NO_COUNT = frozenset(
    {
        "MaterialValuationSet",
        "ChangeDocHeaderSet",
        "ChangeDocItemSet",
        "BatchStockSet",
    }
)

# Sets SAP refuses to serve without a predicate. A bare read returns HTTP 400 --
# not because the set is empty, but because the service demands one.
#
# Measured, not guessed. From the 16-Sep sweep (discovery/probes.csv):
#
#   ChangeDocItemSet    $top=5                              -> HTTP 400
#   ChangeDocHeaderSet  $filter=Objectclas eq 'MATERIAL'    -> HTTP 200
#
# MATERIAL is the narrowest class that still carries everything these
# initiatives read -- MARA, MARC and MARD changes all sit under it. Deliberately
# NOT narrowed further to Tabname eq 'MARC': that would be a business decision
# about which changes matter, and the raw layer should not be making those.
#
# Consequence worth knowing: these two tables hold MATERIAL-class change
# documents, not every change document in the client. The run manifest records
# the filter so the table's provenance travels with it.
REQUIRED_FILTER: dict[str, str] = {
    "ChangeDocHeaderSet": "Objectclas eq 'MATERIAL'",
    "ChangeDocItemSet": "Objectclas eq 'MATERIAL'",
}


def table_name(entity_set_name: str) -> str:
    """``MaterialPlantSet`` -> ``material_plant``.

    The trailing ``Set`` is noise once the name is a table, and the boundary
    rule handles runs of capitals -- ``POHistorySet`` has to become
    ``po_history`` and not ``p_o_history``.
    """
    stem = re.sub(r"Set$", "", entity_set_name)
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", stem)
    return spaced.lower()


@dataclass(frozen=True)
class Delta:
    """How to ask SAP for only what changed.

    Two shapes, and which one a set gets is decided by measurement rather than
    preference -- see ``filter_support.csv``.

    **Direct.** The set carries a date SAP will filter on::

        Delta(field="Aedat")        ->  $filter=Aedat ge datetime'...'

    **Derived.** The set carries no date SAP will filter on, so its parent is
    read by date and the child is then fetched by the keys that came back::

        Delta(via="PurchaseOrderSet", via_key="Ebeln")

    The derived shape is not a design preference, it is forced. Filtering
    GoodsMovementItemSet by ``BudatMkpf`` returns HTTP 500, and by ``Ebeln``
    also returns HTTP 500, so going through MaterialDocumentHeaderSet is the
    only route to a date-bounded read of it.
    """

    field: str | None = None
    via: str | None = None
    via_key: str | None = None

    def __post_init__(self) -> None:
        direct = self.field is not None
        derived = self.via is not None and self.via_key is not None
        if direct == derived:
            raise ValueError(
                "A Delta is either direct (field=) or derived (via= and "
                "via_key=), never both and never neither."
            )


# Deltas, declared only where the filter is measured HONOURED against live SAP.
#
# The bar is deliberately higher than the client's own check_filter, which
# blocks properties measured IGNORED or REJECTED and lets an *unprobed* one
# through. That is the right call for an ad-hoc query, where a person is
# watching. It is the wrong one for a pipeline that runs unattended: an
# unprobed filter that turns out to be ignored returns HTTP 200 with the whole
# set, and a "delta" that silently pulls everything is both wrong and slow.
#
# So ChangeDocHeaderSet and ChangeDocItemSet have no delta here. CDHDR's Udate
# and CDPOS's Changenr were never probed, and until they are these two stay on
# full pulls -- correct, just more expensive.
DELTAS: dict[str, Delta] = {
    # EKKO by change date, then its children by the PO numbers that came back.
    "PurchaseOrderSet": Delta(field="Aedat"),
    "PurchaseOrderItemSet": Delta(via="PurchaseOrderSet", via_key="Ebeln"),
    "POScheduleLineSet": Delta(via="PurchaseOrderSet", via_key="Ebeln"),
    # EKBE: Budat is REJECTED here, so it rides on the PO numbers instead.
    "POHistorySet": Delta(via="PurchaseOrderSet", via_key="Ebeln"),
    # MKPF by posting date, then MSEG by the document numbers.
    "MaterialDocumentHeaderSet": Delta(field="Budat"),
    "GoodsMovementItemSet": Delta(via="MaterialDocumentHeaderSet", via_key="Mblnr"),
}


@dataclass(frozen=True)
class IngestSpec:
    """One entity set and the table it fills."""

    entity_set: EntitySet

    @property
    def name(self) -> str:
        return self.entity_set.name

    @property
    def service(self) -> str:
        return self.entity_set.service

    @property
    def table(self) -> str:
        return table_name(self.entity_set.name)

    @property
    def raw_table(self) -> str:
        return f"{TABLE_PREFIX}{self.table}"

    @property
    def keys(self) -> tuple[str, ...]:
        """Entity key properties. Indexed after load, and used to spot duplicates."""
        return self.entity_set.keys

    @property
    def expects_rows(self) -> bool:
        return self.entity_set.name not in KNOWN_EMPTY

    @property
    def countable(self) -> bool:
        return self.entity_set.name not in NO_COUNT

    @property
    def delta(self) -> Delta | None:
        """How to pull incrementally, or None if only a full pull is safe."""
        return DELTAS.get(self.entity_set.name)

    @property
    def required_filter(self) -> str | None:
        """A predicate SAP will not serve this set without."""
        return REQUIRED_FILTER.get(self.entity_set.name)


def check_delta_filters() -> list[str]:
    """Every declared delta whose filter is not measured HONOURED.

    Called by the CLI before an incremental run and by a test, so a delta added
    on an unverified property is caught by the suite rather than by a quiet
    full-set pull dressed up as an increment.
    """
    problems: list[str] = []
    for set_name, delta in DELTAS.items():
        if delta.field is not None:
            target, prop = set_name, delta.field
        else:
            # The child is filtered by via_key, and the parent by its own field.
            target, prop = set_name, delta.via_key or ""
        verdict = verdict_for(target, prop)
        if verdict != HONOURED:
            problems.append(
                f"{target}.{prop}: {verdict or 'never probed'} "
                f"(a delta needs {HONOURED})"
            )
    return problems


def specs() -> tuple[IngestSpec, ...]:
    """Every entity set in the discovery snapshot, in a stable order."""
    return tuple(
        IngestSpec(entity_set=es)
        for _, es in sorted(contract().items(), key=lambda item: item[0])
    )


def spec_for(name: str) -> IngestSpec:
    """One spec by entity-set name, case-insensitively.

    Case-insensitive because the set names are CamelCase and nobody types
    ``MaterialPlantSet`` correctly from memory on the first attempt.
    """
    wanted = name.strip().lower()
    for spec in specs():
        if spec.name.lower() == wanted or spec.table == wanted:
            return spec
    known = ", ".join(sorted(s.name for s in specs()))
    raise KeyError(f"Unknown entity set {name!r}. Known sets: {known}")
