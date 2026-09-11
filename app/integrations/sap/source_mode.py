"""Diagnostics shared by every SAP gateway response.

Every gateway call reports where its data actually came from -- callers
(and the ``/api/i13/data-sources`` endpoint) must be able to tell LIVE data
from MOCK fallback from a genuinely UNAVAILABLE source, never silently mix
them up as if they were equivalent.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar

RowT = TypeVar("RowT")


class SourceMode(str, Enum):
    """Where a gateway response's rows actually came from."""

    LIVE = "LIVE"
    MOCK = "MOCK"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class DataSourceStatus:
    """Diagnostics for one entity set, safe to expose over the API.

    Never carries tokens, secrets, or authorization headers -- only what's
    needed to judge freshness and trustworthiness of the data behind it.
    """

    entity_set: str
    mode: SourceMode
    row_count: int
    available: bool
    fetched_at: datetime


@dataclass(frozen=True)
class SapResult(Generic[RowT]):
    """Rows plus the diagnostics describing where they came from."""

    rows: list[RowT]
    status: DataSourceStatus


def make_status(entity_set: str, mode: SourceMode, row_count: int) -> DataSourceStatus:
    return DataSourceStatus(
        entity_set=entity_set,
        mode=mode,
        row_count=row_count,
        available=row_count > 0,
        fetched_at=datetime.now(timezone.utc),
    )
