"""Which entity set lands in which table.

Unlike the seed's manifest, this one is *derived* rather than written down, and
the difference is not laziness. The July delivery names its files four different
ways, so that mapping genuinely cannot be computed and had to be recorded by
hand. Here the mapping is mechanical -- ``MaterialPlantSet`` becomes
``odata_material_plant`` -- and deriving it means a set added to the discovery
snapshot appears here automatically instead of being silently skipped until
someone notices.

What is written down is only what cannot be derived: which sets can be read
incrementally and how, what SAP demands before it serves a set, and the notes
explaining why.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from functools import lru_cache

from app.integrations.sap.contract import EntitySet, contract, discovery_dir
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
#
# Empty since 2026-10-07: all three sets that were here now hold rows
# (counts.csv) -- ReservationItemSet 7,088, MaterialValuationSet 2,116,
# MonthlyMovementStatisticSet 1,429.
KNOWN_EMPTY: frozenset[str] = frozenset()

# ``$count`` returns HTTP 400 on these. Not fatal: the client falls back to
# page-until-short-page, so they read fine, it just cannot state a total up
# front and therefore cannot cross-check the row count against one.
#
# Empty since 2026-10-07: $count answers a number on all 21 sets (counts.csv),
# including the four that were here (MaterialValuationSet, ChangeDocHeaderSet,
# ChangeDocItemSet, BatchStockSet).
NO_COUNT: frozenset[str] = frozenset()

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


# How a delta field's date is written in a $filter. Measured per field on
# 2026-10-07 (delta_support.csv), NOT read off the declared type: a DATS field
# SAP re-typed to Edm.String takes one of two text shapes, and the wrong one is
# not an error -- on GoodsMovementItemSet `CpudtMkpf ge '20250101'` answers
# HTTP 200 with all 68,618 rows, while `ge '01.01.2025'` answers the 6 that
# match. A datetime literal on a re-typed field is HTTP 400.
LITERAL_DATETIME = "datetime"  # Aedat ge datetime'2026-09-15T00:00:00'
LITERAL_DATS = "dats"          # Cpudt ge '20260915'
LITERAL_DOTTED = "dotted"      # CpudtMkpf ge '15.09.2026'
LITERALS = frozenset({LITERAL_DATETIME, LITERAL_DATS, LITERAL_DOTTED})


@dataclass(frozen=True)
class Delta:
    """How to ask SAP for only what changed.

    Two parts, and a set has one or both:

    **Its own window.** The set carries a date SAP filters on, and that date
    moves whenever a row is created or changed::

        Delta(field="Cpudt", literal=LITERAL_DATS)  ->  Cpudt ge '20260915'

    **Its parents' keys.** Something that changes this set's rows is recorded
    on another set instead, so the parents are read by THEIR windows and this
    set is re-read for every key they hand back::

        Delta(via=("PurchaseOrderItemSet", "POHistorySet"), via_key="Ebeln")

    Both together is the common case in the purchase-order family: EKPO's own
    Aedat moves on every item change, but a goods receipt that marks the item
    delivery-complete moves nothing on EKPO -- it writes an EKBE row. So EKPO
    reads its own window AND every purchase order with new history.

    Which shape a set gets is decided by measurement, and each choice below
    cites what was measured.
    """

    field: str | None = None
    literal: str = LITERAL_DATETIME
    via: tuple[str, ...] = ()
    via_key: str | None = None

    # Evidence, where it does not come from delta_support.csv.
    #
    # The default bar is a HONOURED verdict in that file for this field and
    # this literal shape. A free-text citation rather than a boolean: "someone
    # ticked a box" is not evidence, and the next person needs to know where
    # to look.
    verified: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.via, str):
            # One parent, written the short way.
            object.__setattr__(self, "via", (self.via,))
        if bool(self.via) != (self.via_key is not None):
            raise ValueError("A Delta's via= and via_key= go together: parents need a key to hand over.")
        if self.field is None and not self.via:
            raise ValueError("A Delta needs a field of its own, parents to read through, or both.")
        if self.literal not in LITERALS:
            raise ValueError(f"Unknown literal shape {self.literal!r}; one of {sorted(LITERALS)}.")

    @property
    def direct(self) -> bool:
        """Whether this set reads a window of its own."""
        return self.field is not None

    @property
    def derived(self) -> bool:
        """Whether this set is re-read for keys its parents hand over."""
        return bool(self.via)


# Deltas, declared only where the filter is measured HONOURED against live SAP,
# with the literal shape that was measured. Re-designed 2026-10-07 from three
# measurements against DEV, all through the client (scratch evidence in the
# 2026-10-07 commit message; the filter probes are delta_support.csv):
#
#   1. Every EKPO change and every EKET change on record moves EKPO.Aedat.
#      CDPOS (EINKBELEG) holds 7,605 EKPO updates and 384 EKET updates
#      (MENGE, EINDT, SLFDT, ...); not one is dated after its item's Aedat.
#      EKKO.Aedat does NOT move: it is the creation date (98% equal to Bedat),
#      and 2,786 of 8,724 header changes are dated after it.
#   2. A goods receipt moves nothing on EKPO. 117 of 624 delivery-complete
#      items (ELIKZ) took their last EKBE entry after their Aedat -- the
#      receipt set the flag. Receipts also move EKET.WEMNG.
#   3. Of 75 header changes to the fields EKKO projects (EKGRP 58, WAERS 17),
#      72 sit on a purchase order with an item change on or after them.
#
# Before this, the whole family hung off EKKO.Aedat. That read new purchase
# orders and nothing else: every change to an existing one -- quantity, price,
# deletion flag, delivery date, every goods receipt -- was invisible to the
# delta and waited for the next full CSV pull.
#
# The bar is deliberately higher than the client's own check_filter, which
# blocks properties measured IGNORED or REJECTED and lets an *unprobed* one
# through. That is the right call for an ad-hoc query, where a person is
# watching. It is the wrong one for a pipeline that runs unattended: an
# unprobed filter that turns out to be ignored returns HTTP 200 with the whole
# set, and a "delta" that silently pulls everything is both wrong and slow.
DELTAS: dict[str, Delta] = {
    # EKPO by its own change date (finding 1), plus every purchase order with
    # new history (finding 2).
    "PurchaseOrderItemSet": Delta(field="Aedat", via=("POHistorySet",), via_key="Ebeln"),
    # EKKO by its creation date -- new purchase orders -- plus every purchase
    # order whose items changed (finding 3). A header-only change with no item
    # change (3 of 75 measured) waits for the CSV refresh; reading
    # EINKBELEG change documents to catch those is the next step if it matters.
    "PurchaseOrderSet": Delta(field="Aedat", via=("PurchaseOrderItemSet",), via_key="Ebeln"),
    # EKET has no date of its own that means "changed" (Eindt is the delivery
    # date). Its changes ride on EKPO.Aedat (finding 1) and its received
    # quantity on EKBE (finding 2), so it is re-read for both sets of keys.
    "POScheduleLineSet": Delta(via=("PurchaseOrderItemSet", "POHistorySet"), via_key="Ebeln"),
    # EKBE by entry date. History rows are written once and never changed --
    # a reversal is a new row -- so the entry date is a complete increment.
    # Text-typed since 2026-10 ('20260916'); a datetime literal is HTTP 400.
    # Budat would be wrong even if it worked: a posting date can be backdated.
    "POHistorySet": Delta(field="Cpudt", literal=LITERAL_DATS),
    # MKPF by entry date. Budat was IGNORED through September and is HTTP 400
    # since 2026-10-05; Cpudt is honoured, and is the right field anyway --
    # posting dates are backdated, entry dates are not.
    "MaterialDocumentHeaderSet": Delta(field="Cpudt"),
    # MSEG by its header's entry date, which the set now carries itself and
    # filters on -- only in SAP's display shape. Material document lines are
    # never changed after posting, so this is a complete increment, read
    # directly rather than fifty MKPF keys at a time.
    "GoodsMovementItemSet": Delta(field="CpudtMkpf", literal=LITERAL_DOTTED),
    # CDHDR by change date, alongside the Objectclas predicate the set
    # demands (combine() sends both). Text-typed since 2026-10; the datetime
    # literal this used to send is now HTTP 400.
    "ChangeDocHeaderSet": Delta(field="Udate", literal=LITERAL_DATS),
    # CDPOS deliberately has NO delta, and it is not an oversight.
    #
    # The obvious shape is `via ChangeDocHeaderSet on Changenr`, which builds
    # `Changenr eq 'a' or Changenr eq 'b' or ...` inside parentheses next to
    # the Objectclas predicate. That exact shape was measured REJECTED_HTTP_400
    # on this set -- "or inside parentheses" in operator_support.csv. One
    # request per change number would be thousands of requests for one run.
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

        Declared and runnable are different things. A parent's keys come from
        the parent's own window, so every parent needs a date SAP filters on;
        one without is no route to an increment, and a sweep that took the
        declaration at face value would read that parent whole and call it an
        increment. And a set SAP cannot serve rows from (``blocked``) has
        nothing to run either, whatever it declares -- nor does a set reading
        through a blocked parent, which has no keys to hand over.
        """
        delta = self.delta
        if delta is None or self.blocked:
            return None
        for parent_name in delta.via:
            parent = spec_for(parent_name)
            if parent.blocked or parent.delta is None or not parent.delta.direct:
                return None
        return delta

    @property
    def why_not_runnable(self) -> str | None:
        """One phrase for a listing: why no increment runs for this set."""
        if self.runnable_delta is not None:
            return None
        if self.blocked:
            return f"blocked: {self.blocked}"
        if self.delta is None:
            return "full pull only"
        return f"via {', '.join(self.delta.via)} (no window: full)"


# --- Evidence ----------------------------------------------------------------

DELTA_SUPPORT_FILE = "delta_support.csv"

# What delta_support.csv records for a derived read: the child filtered by a
# chain of `via_key eq 'k1' or via_key eq 'k2' ...`, exactly as fetch builds it.
KEY_CHAIN = "keys"


@lru_cache
def delta_support() -> dict[tuple[str, str, str], str]:
    """(entity set, property, literal shape) -> verdict, from the delta probe.

    The probe (``python -m app.ingest.delta_probe``) is the four-step test the
    impossible-value sweep cannot stand in for: total, an impossible day, one
    real day (a proper subset), and a window whose every row is checked to
    fall inside it. filter_support.csv cannot say which TEXT shape a re-typed
    date takes; this file can, because it sends the literal fetch sends.
    """
    path = discovery_dir() / DELTA_SUPPORT_FILE
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            (row["entity_set"], row["property"], row["literal"]): row["verdict"]
            for row in csv.DictReader(handle)
        }


def check_delta_filters() -> list[str]:
    """Every declared delta whose filter is not measured HONOURED.

    Called by the CLI and the scheduler before an incremental run, and by a
    test, so a delta added on an unverified property is caught by the suite
    rather than by a quiet full-set pull dressed up as an increment.

    Two questions per delta. Its own window: is ``field`` honoured in exactly
    the literal shape declared (delta_support.csv)? Its parents: is
    ``via_key`` honoured on this set as an `or` chain (delta_support.csv, or
    a HONOURED impossible-value verdict in filter_support.csv), and does every
    parent have a window of its own to hand keys over from?
    """
    measured = delta_support()
    problems: list[str] = []
    for set_name, delta in DELTAS.items():
        if delta.verified:
            # Proven by a different probe, with the citation recorded. Not a
            # way around the check -- a way to record evidence the probe
            # cannot hold, and the citation is what makes it reviewable.
            continue
        if delta.direct:
            verdict = measured.get((set_name, delta.field or "", delta.literal))
            if verdict != HONOURED:
                problems.append(
                    f"{set_name}.{delta.field} as a {delta.literal} literal: "
                    f"{verdict or 'never probed'} (a delta needs {HONOURED} in "
                    f"{DELTA_SUPPORT_FILE}, or a Delta(verified=...) citation)"
                )
        if delta.derived:
            key = delta.via_key or ""
            verdict = measured.get((set_name, key, KEY_CHAIN)) or verdict_for(set_name, key)
            if verdict != HONOURED:
                problems.append(
                    f"{set_name}.{key}: {verdict or 'never probed'} "
                    f"(a delta needs {HONOURED}, or a Delta(verified=...) citation)"
                )
            for parent in delta.via:
                parent_delta = DELTAS.get(parent)
                if parent_delta is None or not parent_delta.direct:
                    problems.append(
                        f"{set_name} reads through {parent}, which has no window "
                        "of its own to hand keys over from"
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
