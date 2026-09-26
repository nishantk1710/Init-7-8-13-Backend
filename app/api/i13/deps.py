"""Shared FastAPI dependencies for the Initiative 13 routes."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from fastapi import Header, HTTPException, Query, Response, status

from app.core.config import get_settings
from app.initiatives.i13.snapshot import I13Snapshot, SnapshotBuilding, SnapshotFailed, get_i13_snapshot

T = TypeVar("T")

#: Seconds a client should wait before retrying while the snapshot builds.
SNAPSHOT_RETRY_AFTER_SECONDS = 10


def snapshot_or_live(
    live: bool = Query(
        False,
        description=(
            "Recompute from the raw tables instead of serving the I13 snapshot. "
            "For parity checks; slow on unfiltered calls."
        ),
    ),
) -> I13Snapshot | None:
    """The I13 snapshot to serve from, or ``None`` to take the live path.

    ``None`` when ``?live=true`` or when ``I13_SNAPSHOT_ENABLED`` is false.
    While the first build is still running this answers **503** with
    ``Retry-After`` -- never a slow live recompute, which would compete with
    the build for the same CPU (decision D15).
    """
    if live or not get_settings().i13_snapshot_enabled:
        return None
    return require_snapshot()


def require_snapshot() -> I13Snapshot:
    """The snapshot, for routes that have no live path at all (GRNI, usage
    patterns). Same 503s as :func:`snapshot_or_live` while it is unavailable."""
    try:
        return get_i13_snapshot()
    except SnapshotBuilding as building:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "building",
                "message": "I13 data is being prepared. Retry shortly.",
                "startedAt": building.started_at.isoformat() if building.started_at else None,
            },
            headers={"Retry-After": str(SNAPSHOT_RETRY_AFTER_SECONDS)},
        ) from None
    except SnapshotFailed as failed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "failed", "message": f"I13 data could not be prepared: {failed}"},
            headers={"Retry-After": str(SNAPSHOT_RETRY_AFTER_SECONDS * 6)},
        ) from None


def page(items: Sequence[T], response: Response, *, limit: int | None, offset: int = 0) -> Sequence[T]:
    """Slice ``items`` and report the unsliced total in ``X-Total-Count``.

    Bare-array bodies are kept (decision D7), so no existing caller's response
    shape changes; a caller that wants the total reads the header.
    """
    response.headers["X-Total-Count"] = str(len(items))
    if limit is None:
        return items[offset:]
    return items[offset : offset + limit]


def get_data_dir() -> Path:
    """The CSV-backed SAP/platform dataset root (see ``Settings.i13_data_dir``)."""
    return Path(get_settings().i13_data_dir)


@dataclass(frozen=True)
class Actor:
    """W6.6: who is acting on an ACT exception. See ``get_current_actor``."""

    id: str
    type: str = "USER"


def get_current_actor(x_actor_id: str | None = Header(default=None, alias="X-Actor-Id")) -> Actor:
    """Placeholder identity resolution -- no authentication is implemented
    anywhere in this codebase yet (see ``app/core/security.py``). Reads a
    client-supplied header rather than trusting a request-body field, so
    identity isn't simply whatever an arbitrary JSON payload claims.

    When Entra ID authentication is wired in, this dependency is the one
    thing that changes -- it would validate a JWT and build the same
    ``Actor`` from its claims instead of a header. Every caller of
    ``Actor`` (the W6.6 routes and application services) stays the same.
    """
    return Actor(id=x_actor_id or "unknown-actor")
