"""Per-repository-instance memoization for read-only Postgres fetches.

Every ``Postgres*Repository`` in this package is constructed fresh per API
request (see the route handlers in ``app/api/i13/*.py`` -- each does
``PostgresProcurementRepository(db)`` etc. inline), so caching on the
instance is naturally scoped to exactly one request's lifetime: never shared
across requests, never stale beyond one request, no separate cache-lifetime
bookkeeping needed.

This exists because I13's composed services call the same repository method
with the same arguments multiple times within a single request. Measured
before this existed: a single ``GET /api/i13/summary`` called
``compute_all_movement_metrics`` (and therefore
``PostgresMovementRepository.get_movement_history``) 3 times and
``build_reservation_ledger`` (and therefore every
``PostgresProcurementRepository``/``PostgresReservationRepository`` method)
2 times, each a full-tenant fetch -- see ``build_summary``'s call graph:
it calls ``compute_all_movement_metrics`` directly, then
``build_exception_queue`` (which calls ``build_reservation_ledger`` directly
AND via ``compute_watch_metrics``, which *also* calls
``compute_all_movement_metrics`` again), then
``build_reclassification_candidates`` (another ``compute_all_movement_metrics``
call). None of that duplication was ever intentional -- it fell out of each
function being written to be independently callable, which is still true;
this cache just stops the independent callers from each re-paying the cost
when they're actually composed together in one request.
"""

import functools
from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def memoize_per_instance(method: F) -> F:
    """Cache ``method``'s return value on ``self._cache``, keyed by its
    arguments. Requires the instance to expose a ``_cache: dict`` attribute
    (every repository in this package does -- see each dataclass's
    ``_cache`` field). Every argument passed to a cached method here is a
    plain ``str | None`` filter, always hashable.
    """

    @functools.wraps(method)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        key = (method.__name__, args, tuple(sorted(kwargs.items())))
        cache = self._cache
        if key not in cache:
            cache[key] = method(self, *args, **kwargs)
        return cache[key]

    return wrapper  # type: ignore[return-value]
