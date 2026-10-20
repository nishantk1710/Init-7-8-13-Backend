"""Newest-first ordering for the I13 lists, where it is done in Python.

The stored snapshot sorts in SQL (``SqlSnapshot._newest_first``); the
in-memory snapshot and the live paths sort here, the same way: dated rows
newest first, undated rows after them in the order they came.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from typing import TypeVar

T = TypeVar("T")


def newest_first(items: Iterable[T], date_of: Callable[[T], date | None]) -> list[T]:
    """``items`` by ``date_of`` descending, undated last. Stable, so equal
    dates keep their incoming order."""
    items = list(items)
    dated = [item for item in items if date_of(item) is not None]
    dated.sort(key=date_of, reverse=True)
    return dated + [item for item in items if date_of(item) is None]


def newest_requirement_first(items: Iterable[T], key: Callable[[T], object] = lambda item: item) -> list[T]:
    """Reservation ledger entries (or pairs holding one, via ``key``) by
    requirement date, newest first."""
    return newest_first(items, lambda item: key(item).requirement_date)
