"""The I13 store's rebuild gap: automatic builds no more often than configured.

Pure-function tests; the gated check itself is exercised in
test_snapshot_store.py against a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.initiatives.i13.snapshot_store.lifecycle import too_soon

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)


def test_a_version_built_within_the_gap_holds_the_next_build() -> None:
    remaining = too_soon(NOW - timedelta(minutes=30), NOW, 120)
    assert remaining == timedelta(minutes=90)


def test_a_version_older_than_the_gap_lets_the_build_start() -> None:
    assert too_soon(NOW - timedelta(minutes=120), NOW, 120) is None
    assert too_soon(NOW - timedelta(hours=5), NOW, 120) is None


def test_nothing_served_means_no_gap() -> None:
    """The first build must never wait for a version that does not exist."""
    assert too_soon(None, NOW, 120) is None


def test_a_zero_gap_disables_the_hold() -> None:
    assert too_soon(NOW - timedelta(seconds=1), NOW, 0) is None


def test_a_naive_built_at_is_read_as_utc() -> None:
    """SQL Server hands datetimes back without a zone; the column holds UTC."""
    assert too_soon(NOW.replace(tzinfo=None) - timedelta(minutes=10), NOW, 120) == timedelta(minutes=110)
