"""Reading a whole entity set, correctly.

Two measured facts shape everything here, and both were discovered by running
against live CPI rather than by reasoning about OData.

**1. Unordered paging loses rows, silently.**
``discovery/paging_stability.txt`` recorded three full pulls of
``MaterialPlantSet`` (2,178 rows)::

    pull 1, no $orderby            rows=2178  distinct=1618  duplicates=560  missing=560
    pull 2, no $orderby            rows=2178  distinct=1742  duplicates=436  missing=436
    pull 3, $orderby=Matnr,Werks   rows=2178  distinct=2178  duplicates=0    missing=0

Two unordered pulls disagreed with each other *and* with the total, and each
returned a different subset. Row counts looked right; the data was not. So
``$orderby`` is not an option here, it is a precondition -- a set with no key
cannot be paged safely at all, and this module refuses rather than guessing.

**2. ``$count`` is not always available.**
Some sets answer ``$count`` with HTTP 500 while serving rows perfectly well. So
counting is attempted once and, on failure, paging demotes to reading until a
short page arrives. The demotion is reported in the result, never hidden: the
difference between "complete because the count agreed" and "complete because a
page came back short" matters to anyone deciding whether to trust a total.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.integrations.sap.contract import EntitySet
from app.integrations.sap.errors import RequestError, SapError, TransientError

logger = get_logger(__name__)

# Hard stop, so a paging bug or a set that keeps returning full pages cannot
# exhaust memory. Comfortably above the largest known set -- ChangeDocItemSet,
# about 929,000 rows.
SAFETY_ROW_LIMIT = 2_000_000

# Sets too large to read whole. Asking for all of one of these is almost always
# a mistake, and an expensive one.
FILTER_ONLY_SETS = frozenset({"ChangeDocItemSet", "ChangeDocHeaderSet"})


@dataclass
class Page:
    """What one page read returns. Mirrors the client's ReadResult."""

    rows: list[dict[str, Any]]
    unknown_properties: list[str] = field(default_factory=list)


@dataclass
class ExtractResult:
    """A complete read, and how honestly complete it is."""

    entity_set: str
    rows: list[dict[str, Any]]
    pages: int
    counted: bool
    """True if ``$count`` answered. False means paging fell back to short-page detection."""
    expected: int | None = None
    """What ``$count`` said, when it worked."""
    truncated: bool = False
    """True if SAFETY_ROW_LIMIT stopped the read before the data ran out."""
    order_by: tuple[str, ...] = ()
    """The ordering actually used, which may be shorter than the key -- see below."""

    order_by_degraded: bool = False
    """True when SAP refused the full key ordering and a prefix was used instead.

    Measured on live SAP 2026-09-11: MaterialPlantSet returns HTTP 500 for ANY
    two-field $orderby, while single-field works and two-field works fine on
    other sets. Refusing to read at all would be worse than reading with a
    weaker guarantee -- but the weaker guarantee has to be visible, which is
    what this flag and `stable` are for.
    """

    duplicate_keys: int = 0
    """Rows sharing a key. Above zero means rows were LOST: see `stable`."""

    unknown_properties: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether every row was read.

        In counted mode this is checkable against ``$count``. In fallback mode a
        short page is the only end-of-data signal there is, so 'complete' means
        'we reached the end as far as SAP would tell us' -- a weaker claim, and
        ``counted`` is what distinguishes the two.
        """
        if self.truncated:
            return False
        if self.expected is None:
            return True
        return len(self.rows) == self.expected

    @property
    def stable(self) -> bool:
        """Whether paging demonstrably did not lose rows.

        Not a claim about ordering -- a measurement of the result. $skip/$top
        over a non-total order returns some rows twice and others never, so
        duplicates in the output are direct evidence of rows missing from it.
        Checking costs one pass over the keys and removes the need to trust
        that the ordering was unique.
        """
        return self.duplicate_keys == 0

    def __len__(self) -> int:
        return len(self.rows)


PageReader = Callable[..., Page]
"""Reads one page: (skip, top, order_by) -> Page. Injected so this module has no transport."""


def order_by_for(entity_set: EntitySet) -> tuple[str, ...]:
    """The ordering that makes paging stable: the declared key, in SAP's order.

    Key order comes from ``$metadata``, not alphabetically -- see contract.py.
    Any total order would give stable paging, but the key is the one ordering
    guaranteed to exist and to be unique.
    """
    if not entity_set.keys:
        raise SapError(
            f"{entity_set.name} has no key in the contract, so it cannot be paged "
            "safely: $skip/$top over an unordered result silently drops and "
            "duplicates rows (see discovery/paging_stability.txt)."
        )
    return entity_set.keys


def read_first_page_negotiating_order(
    entity_set: EntitySet, read_page: PageReader, page_size: int
) -> tuple[Page, tuple[str, ...], bool]:
    """Read page one, settling on the longest ``$orderby`` SAP will accept.

    Negotiation happens on the first *real* page rather than a throwaway probe,
    so the common case -- SAP accepts the full key -- costs no extra request.

    This exists because of a measured, set-specific defect: on 2026-09-11
    ``MaterialPlantSet`` returned HTTP 500 with an empty body for ANY two-field
    ``$orderby`` -- ``Matnr,Werks`` and ``Werks,Matnr`` alike -- while every
    single-field ordering worked, and ``Ebeln,Ebelp`` worked fine on
    ``PurchaseOrderItemSet``. An empty-bodied 500 is a backend short dump rather
    than a rejected query: SAP's bug to fix, ours to survive.

    A shortened ordering is NOT equivalent. ``Matnr`` alone is not unique on
    ``MaterialPlantSet`` -- one material appears in several plants -- so rows
    tied on it may be returned in any order between pages, which is exactly the
    condition that loses rows. Hence the returned flag, and hence ``extract``
    measuring duplicate keys afterwards instead of trusting the ordering.
    """
    full = order_by_for(entity_set)

    for length in range(len(full), 0, -1):
        candidate = full[:length]
        try:
            page = read_page(skip=0, top=page_size, order_by=list(candidate))
        except (TransientError, RequestError) as exc:
            logger.warning(
                "%s: SAP rejected $orderby=%s (%s)", entity_set.name, ",".join(candidate), exc
            )
            continue

        degraded = length < len(full)
        if degraded:
            logger.error(
                "%s: SAP will not order by the full key %s; falling back to %s. "
                "That ordering is NOT unique, so paging stability is not "
                "guaranteed -- the result's duplicate_keys/stable fields say "
                "whether rows were actually lost. Raise the $orderby failure "
                "with SAP Basis.",
                entity_set.name,
                ",".join(full),
                ",".join(candidate),
            )
        return page, candidate, degraded

    raise SapError(
        f"{entity_set.name}: SAP rejected every $orderby from {list(full)} down to "
        f"[{full[0]}]. Paging without an ordering silently loses rows, so this set "
        "cannot be read whole until SAP accepts an ordering. Raise it with SAP "
        "Basis -- an empty-bodied HTTP 500 usually leaves an ST22 short dump."
    )


def check_readable_whole(
    entity_set: EntitySet,
    filter: str | None,
    *,
    allow_unfiltered_large_set: bool,
    known_count: str | None = None,
) -> None:
    """Refuse an unfiltered read of a set that is too large to pull whole."""
    if entity_set.name not in FILTER_ONLY_SETS:
        return
    if filter or allow_unfiltered_large_set:
        return
    raise SapError(
        f"{entity_set.name} is too large to read unfiltered "
        f"(about {known_count or 'hundreds of thousands of'} rows). Pass a "
        "$filter, or allow_unfiltered_large_set=True if that really is the intent."
    )


def extract(
    entity_set: EntitySet,
    read_page: PageReader,
    *,
    page_size: int,
    count: int | None,
    safety_limit: int = SAFETY_ROW_LIMIT,
) -> ExtractResult:
    """Page through an entity set in key order until it is exhausted.

    ``count`` is the result of a prior ``$count``, or None where SAP could not
    produce one. Passing it in rather than fetching it here keeps this module
    free of any transport.
    """
    counted = count is not None

    if not counted:
        logger.info(
            "%s: $count unavailable, paging until a short page instead", entity_set.name
        )

    rows: list[dict[str, Any]] = []
    unknown: set[str] = set()
    pages = 0
    truncated = False

    first, order_by, degraded = read_first_page_negotiating_order(
        entity_set, read_page, page_size
    )
    page = first

    while True:
        pages += 1
        rows.extend(page.rows)
        unknown.update(page.unknown_properties)

        # A short page is the end of the data, in either mode. Checked first
        # because it is the only signal fallback mode has.
        if len(page.rows) < page_size:
            break

        if counted and len(rows) >= (count or 0):
            break

        if len(rows) >= safety_limit:
            logger.error(
                "%s: stopped at the %d-row safety limit; this result is INCOMPLETE",
                entity_set.name,
                safety_limit,
            )
            truncated = True
            break

        # A page that returns nothing while claiming to be full would loop
        # forever. Cannot happen with a correct server, which is why it is worth
        # catching rather than trusting.
        if not page.rows:
            logger.error("%s: empty full-size page; stopping to avoid a loop", entity_set.name)
            truncated = True
            break

        page = read_page(skip=len(rows), top=page_size, order_by=list(order_by))

    # Direct evidence, not an assumption. $skip/$top over a non-total order
    # returns some rows twice and others never, so duplicates here mean rows are
    # missing -- whatever the ordering claimed to be.
    duplicates = count_duplicate_keys(rows, entity_set.keys)
    if duplicates:
        logger.error(
            "%s: %d duplicate key(s) in %d rows -- paging LOST ROWS. "
            "Ordering used: %s%s",
            entity_set.name,
            duplicates,
            len(rows),
            ",".join(order_by),
            " (degraded from the full key)" if degraded else "",
        )

    result = ExtractResult(
        entity_set=entity_set.name,
        rows=rows,
        pages=pages,
        counted=counted,
        expected=count,
        truncated=truncated,
        order_by=order_by,
        order_by_degraded=degraded,
        duplicate_keys=duplicates,
        unknown_properties=sorted(unknown),
    )

    if counted and not result.complete and not truncated:
        # Paged in key order and still disagreeing with $count. Either the set
        # changed mid-read or something is wrong -- surface it either way.
        logger.warning(
            "%s: read %d rows but $count said %d", entity_set.name, len(rows), count
        )

    logger.info(
        "%s: %d rows in %d page(s), ordered by %s, %s",
        entity_set.name,
        len(rows),
        pages,
        ",".join(order_by),
        "counted" if counted else "fallback paging",
    )
    return result


def count_duplicate_keys(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> int:
    """How many rows share a key with an earlier row.

    Zero proves the page sequence did not overlap, which is the property paging
    needs. A row missing a key field is counted as distinct rather than crashing:
    a $select that omitted a key is the caller's problem, not a reason to fail
    the whole extraction.
    """
    if not keys:
        return 0
    seen: set[tuple] = set()
    duplicates = 0
    for row in rows:
        identity = tuple(row.get(k) for k in keys)
        if None in identity:
            continue
        if identity in seen:
            duplicates += 1
        else:
            seen.add(identity)
    return duplicates


def iter_pages(
    entity_set: EntitySet,
    read_page: PageReader,
    *,
    page_size: int,
    safety_limit: int = SAFETY_ROW_LIMIT,
) -> Iterator[list[dict[str, Any]]]:
    """Yield pages instead of accumulating them.

    For callers that stream rows into Postgres rather than holding them: the
    seed loader's COPY path is the obvious consumer, and 929,000 change-document
    rows should never be a Python list.
    """
    order_by = list(order_by_for(entity_set))
    read = 0
    while True:
        page = read_page(skip=read, top=page_size, order_by=order_by)
        if page.rows:
            yield page.rows
        read += len(page.rows)
        if len(page.rows) < page_size:
            return
        if read >= safety_limit:
            logger.error(
                "%s: stopped at the %d-row safety limit", entity_set.name, safety_limit
            )
            return
