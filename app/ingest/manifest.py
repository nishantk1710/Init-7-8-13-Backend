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
from app.integrations.sap.known_conditions import (
    NON_UNIQUE_DECLARED_KEYS,
    READ_BROKEN_SETS,
)

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

    # Evidence, where it does not come from filter_support.csv.
    #
    # The default bar is a HONOURED verdict in that file. Some filters are
    # proven elsewhere -- CDHDR's Udate is REJECTED alone and works alongside
    # the Objectclas predicate, which filter_support's one-property-at-a-time
    # probe cannot express, so the proof lives in operator_support.csv instead.
    #
    # A free-text citation rather than a boolean: "someone ticked a box" is not
    # evidence, and the next person needs to know where to look.
    verified: str | None = None

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
    # MKPF deliberately has NO delta on Budat, and it is not an oversight.
    #
    # Four probes of the 2026-09-25 sweep, all against ZMM_KPI02_ADD_SRV,
    # set total 40,651 throughout (operator_support.csv rows 9-11 and
    # filter_support.csv; the requests themselves are calls.csv lines 223-225
    # and 400, each answering HTTP 200 with a 5-byte $count body -- "40651"):
    #
    #   Budat ge datetime'2026-01-01T00:00:00'              -> 40,651
    #   Budat ge datetime'2013-01-01...' and lt '2014-01-01' -> 40,651
    #   Budat eq datetime'2013-09-27T00:00:00'              -> 40,651
    #       (the control: a real posting date sampled from this set, so it
    #        should have matched a subset and did not)
    #   Budat eq datetime'1900-01-01T00:00:00'              -> 40,651
    #       (the impossible value filter_support probes with; this is the
    #        row that reads IGNORED there)
    #
    # The control row is what settles it. A filter that only failed when it
    # matched nothing would already be unusable for a delta -- `Budat ge
    # <watermark>` matches nothing on any day when nothing was posted, and
    # the pipeline would then load all 40,651 rows as if they were new. Budat
    # is worse than that: it is dropped even when it WOULD have matched, so no
    # watermark value makes it safe. MKPF pulls in full, like ChangeDocItemSet.
    #
    # Do not resurrect this from the older backend/discovery/ snapshot, where
    # `ge 2026-01-01` returns 2 and `eq 2013-09-27` returns 919. That snapshot
    # is a different service (ZVZI_KPI02_SHARED_SRV) and is not what
    # contract.discovery_dir() reads. On the service we actually call, Budat
    # is IGNORED.
    #
    # It could never have run anyway: _read_direct sends a set's own window
    # with allow_unsupported_filter=False, and check_filter refuses an IGNORED
    # property, so this entry raised UnsupportedFilterError rather than
    # pulling anything.
    #
    # MSEG keeps its declaration. The shape is still the only right one -- its
    # own filters are HTTP 500, so its parent's keys are the only route to a
    # date-bounded read -- and this is what to revive if MKPF ever gains a
    # filterable date. Until then there is no parent window, and fetch_set
    # pulls MSEG in full and says so, rather than reading MKPF whole and
    # asking for its children 50 keys at a time: 814 requests for the set one
    # full pull already returns.
    "GoodsMovementItemSet": Delta(via="MaterialDocumentHeaderSet", via_key="Mblnr"),
    # CDHDR by change date. Only works alongside the Objectclas predicate this
    # set demands -- `Udate ge ...` on its own is HTTP 400, while
    # `Objectclas eq 'MATERIAL' and Udate ge ...` returned 3760 rows
    # (discovery 2026-09-21, operator_support.csv). combine() sends both.
    "ChangeDocHeaderSet": Delta(
        field="Udate",
        verified=(
            "operator_support.csv 2026-09-21: 'eq + date ge' -> 3760 rows "
            "(bare 'date ge on CDHDR' is REJECTED_HTTP_400)"
        ),
    ),
    # CDPOS deliberately has NO delta, and it is not an oversight.
    #
    # The obvious shape is `via ChangeDocHeaderSet on Changenr`, which builds
    # `Changenr eq 'a' or Changenr eq 'b' or ...`. That exact shape is measured
    # REJECTED_HTTP_400 on this set -- "or inside parentheses" in
    # operator_support.csv. One request per change number would be thousands of
    # requests for one run.
    #
    # So CDPOS stays a full pull under its Objectclas predicate, and anything
    # wanting only the changed documents filters after the fact. Honest and
    # slower beats an increment SAP cannot express.
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
        """The key SAP declares. Indexed after load; what downstream joins on."""
        return self.entity_set.keys

    @property
    def identity_keys(self) -> tuple[str, ...]:
        """The columns that actually address a row uniquely.

        Usually the declared key, but not always: CDPOS declares three columns
        and repeats them once per field changed in the same document. Duplicate
        counting is how a pull proves it lost nothing, so running it against a
        key that is not unique reports duplicates that are not there and a
        correct extract gets refused as corrupt.

        Used for the duplicate check and for the merge join. Indexes still
        follow the declared key -- that is what downstream tables join on, and
        indexing seven columns to serve a uniqueness check nobody queries would
        cost more than it returns.
        """
        return NON_UNIQUE_DECLARED_KEYS.get(self.entity_set.name, self.entity_set.keys)

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

    @property
    def blocked(self) -> str | None:
        """Why this set cannot be read at all right now, or None.

        A measured fact about SAP, from known_conditions.READ_BROKEN_SETS: a
        set whose every row read is a server error has no delta to run and no
        full pull to fall back to, and a sweep says so instead of trying.
        """
        return READ_BROKEN_SETS.get(self.entity_set.name)

    @property
    def runnable_delta(self) -> Delta | None:
        """The delta this set can run today, or None.

        Declared and runnable are different things. A derived delta reads its
        parent by date, so it needs the parent to have a date SAP filters on.
        GoodsMovementItemSet is declared through MaterialDocumentHeaderSet --
        the only correct shape -- but MKPF's Budat is IGNORED, so there is no
        window and the delta cannot run. A sweep that took ``delta`` at face
        value pulled MSEG in full every cycle while calling it an increment.

        And a set SAP cannot serve rows from (``blocked``) has nothing to run
        either, whatever it declares.
        """
        delta = self.delta
        if delta is None or self.blocked:
            return None
        if delta.field is not None:
            return delta
        parent = spec_for(delta.via or "").delta
        if parent is not None and parent.field is not None:
            return delta
        return None

    @property
    def why_not_runnable(self) -> str | None:
        """One phrase for a listing: why no increment runs for this set."""
        if self.runnable_delta is not None:
            return None
        if self.blocked:
            return f"blocked: {self.blocked}"
        if self.delta is None:
            return "full pull only"
        return f"via {self.delta.via} (no window: full)"


def check_delta_filters() -> list[str]:
    """Every declared delta whose filter is not measured HONOURED.

    Called by the CLI before an incremental run and by a test, so a delta added
    on an unverified property is caught by the suite rather than by a quiet
    full-set pull dressed up as an increment.
    """
    problems: list[str] = []
    for set_name, delta in DELTAS.items():
        if delta.verified:
            # Proven by a different probe, with the citation recorded. Not a
            # way around the check -- a way to record evidence filter_support
            # cannot hold, and the citation is what makes it reviewable.
            continue
        if delta.field is not None:
            target, prop = set_name, delta.field
        else:
            # The child is filtered by via_key, and the parent by its own field.
            target, prop = set_name, delta.via_key or ""
        verdict = verdict_for(target, prop)
        if verdict != HONOURED:
            problems.append(
                f"{target}.{prop}: {verdict or 'never probed'} "
                f"(a delta needs {HONOURED}, or a Delta(verified=...) citation)"
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
