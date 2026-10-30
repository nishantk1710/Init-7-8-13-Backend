"""Shared FastAPI dependencies for the Initiative 13 routes."""

import functools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from fastapi import Depends, Header, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from app.core import security
from app.core.config import get_settings
from app.core.db import get_db
from app.initiatives.i13.snapshot import I13Snapshot, SnapshotBuilding, SnapshotFailed, get_i13_snapshot
from app.initiatives.i13.snapshot_store import lifecycle as store
from app.initiatives.i13.snapshot_store.reader import SqlSnapshot
from app.initiatives.i13.expected_delivery import attach_expected_delivery
from app.shared.material_names import attach_material_names

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
    db: Session = Depends(get_db),
) -> I13Snapshot | SqlSnapshot | None:
    """The I13 snapshot to serve from, or ``None`` to take the live path.

    ``None`` when ``?live=true`` or when ``I13_SNAPSHOT_ENABLED`` is false.
    While the first build is still running this answers **503** with
    ``Retry-After`` -- never a slow live recompute, which would compete with
    the build for the same CPU (decision D15).
    """
    if live or not get_settings().i13_snapshot_enabled:
        return None
    return require_snapshot(db)


def require_snapshot(db: Session = Depends(get_db)) -> I13Snapshot | SqlSnapshot:
    """The snapshot, for routes that have no live path at all (GRNI, usage
    patterns). Same 503s as :func:`snapshot_or_live` while it is unavailable.

    With ``I13_SNAPSHOT_STORE=sql`` this is a :class:`SqlSnapshot` over the
    request's own session: routes check which they were given.
    """
    try:
        if store.enabled():
            return store.current(db)
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


def get_current_actor(
    request: Request, x_actor_id: str | None = Header(default=None, alias="X-Actor-Id")
) -> Actor:
    """Who is acting on an ACT exception, a plan or a suggestion.

    Sign-in on (``AUTH_ENABLED``): the signed-in user's Entra object ID from the
    validated token; the header is ignored. Sign-in off: the ``X-Actor-Id``
    header as before, never a request-body field. Every caller of ``Actor``
    (the W6.6 routes and application services) stays the same. See
    ``app.core.security.actor_id``.
    """
    return Actor(id=security.actor_id(request, x_actor_id))


def with_expected_delivery() -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Fill ``expected_delivery_date`` (EKET-EINDT) on a ledger route's rows
    from their PO line. Reads the in-memory list only (see
    ``app.initiatives.i13.expected_delivery``): no query on the route."""

    def decorate(route: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(route)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = route(*args, **kwargs)
            attach_expected_delivery(result if isinstance(result, list) else [result])
            return result

        return wrapper

    return decorate


def with_material_names(
    row_of: Callable[[Any], Any] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Fill ``material_description`` on whatever a list route returns.

    Placed under ``@router.get``. Reads the in-memory name list only (see
    ``app.shared.material_names``), so it adds no query to the route.
    ``row_of`` picks the object carrying ``material`` when a response row
    nests it (a confirmation and its exception).
    """

    def decorate(route: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(route)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            result = route(*args, **kwargs)
            rows = result if isinstance(result, list) else [result]
            attach_material_names([row_of(r) for r in rows] if row_of else rows)
            return result

        return wrapper

    return decorate
