"""The SAP client. The only thing application code should import.

Callers name an entity set and pass OData options. Everything else -- which
service owns the set, the CPI iFlow, the token, the double envelope, typed
decoding, paging, the filter guard -- is below this line.

    client = SapClient()
    page = client.read("MaterialPlantSet", filter="Dismm eq 'ND'", top=100)
    rows = client.read_all("MaterialPlantSet")          # ordered, paged, complete

Two behaviours worth knowing, both measured rather than assumed:

**Paging is always ordered.** ``paging_stability.txt`` recorded an unordered
full pull of ``MaterialPlantSet`` returning 2,178 rows but only 1,618 distinct
keys -- 560 duplicated, 560 missing, and a *different* 560 on the next attempt.
Ordering by the key made it exact. So ``read_all`` refuses to page without an
``$orderby``, rather than offering it as an option.

**$count is not trusted.** It returns HTTP 500 on some sets. ``read_all`` asks
once, and on failure demotes itself to paging until a short page arrives. The
demotion is reported, not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.integrations.sap.contract import EntitySet, entity_set
from app.integrations.sap.envelope import DecodedRows, decode_count, decode_rows
from app.integrations.sap.errors import SapError, TransientError
from app.integrations.sap.filters import check_filter
from app.integrations.sap.known_conditions import COUNT_CAPPED_SETS
from app.integrations.sap.paging import (
    ExtractResult,
    Page,
    check_readable_whole,
    extract,
)
from app.integrations.sap.transport import CpiTransport

logger = get_logger(__name__)

@dataclass
class ReadResult:
    """One page of rows."""

    entity_set: str
    rows: list[dict[str, Any]]
    unknown_properties: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """No rows. Distinct from a failure -- two live sets legitimately have none."""
        return not self.rows

    def __len__(self) -> int:
        return len(self.rows)


def _query(
    *,
    filter: str | None = None,
    select: list[str] | None = None,
    order_by: list[str] | None = None,
    top: int | None = None,
    skip: int | None = None,
    json_format: bool = True,
) -> str:
    """Assemble an OData query string. The only place one is built."""
    parts: list[str] = []
    if filter:
        parts.append(f"$filter={filter}")
    if select:
        parts.append(f"$select={','.join(select)}")
    if order_by:
        parts.append(f"$orderby={','.join(order_by)}")
    if top is not None:
        parts.append(f"$top={top}")
    if skip is not None:
        parts.append(f"$skip={skip}")
    if json_format:
        parts.append("$format=json")
    return "&".join(parts)


class SapClient:
    """Read-only access to SAP. Nothing here writes: P1 forbids write-back."""

    def __init__(
        self,
        settings: Settings | None = None,
        transport: CpiTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._transport = transport or CpiTransport(self._settings)

    # --- Single reads -----------------------------------------------------

    def read(
        self,
        name: str,
        *,
        filter: str | None = None,
        select: list[str] | None = None,
        order_by: list[str] | None = None,
        top: int | None = None,
        skip: int | None = None,
        allow_unsupported_filter: bool = False,
    ) -> ReadResult:
        """One page of rows, decoded against the contract."""
        target = entity_set(name)
        if not allow_unsupported_filter:
            check_filter(name, filter)

        body = self._transport.get(
            target.api_path,
            _query(filter=filter, select=select, order_by=order_by, top=top, skip=skip),
            context=name,
        )
        decoded: DecodedRows = decode_rows(body, target)

        if decoded.unknown_properties:
            # Drift: SAP is returning a property the snapshot does not know.
            # Loud, but not fatal -- the data is kept.
            logger.warning(
                "%s returned properties not in the contract: %s. "
                "Re-run cpi_discovery.py.",
                name,
                ", ".join(decoded.unknown_properties),
            )

        return ReadResult(
            entity_set=name,
            rows=decoded.rows,
            unknown_properties=decoded.unknown_properties,
        )

    def count(
        self,
        name: str,
        *,
        filter: str | None = None,
        allow_unsupported_filter: bool = False,
    ) -> int | None:
        """``$count``, or None where SAP cannot produce a trustworthy one.

        None is a real answer, not an error: several sets return HTTP 500 for
        ``$count`` while serving rows perfectly well.

        One set is not asked at all. ReservationItemSet answers 1000 when paging
        returns 7088 -- a cap rather than a total. Paging stops once it has read
        as many rows as the total claims, so believing that number hands the
        caller a seventh of the set and calls it complete. Declining to ask
        demotes the read to page-until-short-page, which is exact.
        See known_conditions.COUNT_CAPPED_SETS.
        """
        target = entity_set(name)
        if name in COUNT_CAPPED_SETS:
            logger.info(
                "%s: not asking for $count -- it is capped on this set and would "
                "stop paging early. Reading until a short page instead.",
                name,
            )
            return None
        if not allow_unsupported_filter:
            check_filter(name, filter)
        try:
            body = self._transport.get(
                f"{target.api_path}/$count",
                _query(filter=filter, json_format=False),
                context=f"{name}/$count",
            )
        except (TransientError, SapError) as exc:
            logger.info("%s: $count unavailable (%s)", name, exc)
            return None
        return decode_count(body, name)

    def metadata(self, service: str) -> str:
        """Raw ``$metadata`` XML for a service. Used by the contract tests."""
        return self._transport.get(
            f"sap/opu/odata/sap/{service}/$metadata",
            "",
            context=f"$metadata for {service}",
        )

    # --- Full reads -------------------------------------------------------

    def read_all(
        self,
        name: str,
        *,
        filter: str | None = None,
        select: list[str] | None = None,
        page_size: int | None = None,
        allow_unsupported_filter: bool = False,
        allow_unfiltered_large_set: bool = False,
    ) -> ExtractResult:
        """Every row, paged in key order.

        Ordering is not optional. See the module docstring: an unordered pull
        loses roughly a quarter of the rows and cannot tell you it did.
        """
        target: EntitySet = entity_set(name)
        if not allow_unsupported_filter:
            check_filter(name, filter)

        check_readable_whole(
            target,
            filter,
            allow_unfiltered_large_set=allow_unfiltered_large_set,
            known_count=_known_count(name),
        )

        size = page_size or self._settings.cpi_page_size

        # Counted once, up front. allow_unsupported_filter is set because the
        # filter was already checked above -- checking twice would raise the
        # same error from a confusing place.
        count = self.count(name, filter=filter, allow_unsupported_filter=True)

        def read_page(*, skip: int, top: int, order_by: list[str]) -> Page:
            page = self.read(
                name,
                filter=filter,
                select=select,
                order_by=order_by,
                top=top,
                skip=skip,
                allow_unsupported_filter=True,
            )
            return Page(rows=page.rows, unknown_properties=page.unknown_properties)

        return extract(target, read_page, page_size=size, count=count)


def _known_count(name: str) -> str:
    from app.integrations.sap.contract import counts

    return counts().get(name, "unknown")
