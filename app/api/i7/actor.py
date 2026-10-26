"""Who is making a change to an I07 recommendation.

Isolated here so the Entra ID work replaces ONE function. Today the identity is the shared
``app.api.deps.get_current_actor`` -- the ``X-Actor-Id`` header the frontend sets from
``src/lib/api/actor.ts`` -- and **that is not authentication**: the header is whatever the caller sends.
It is better than a field in the request body only in that the browser sets it in one place and the
backend never reads a user id out of a payload.

What this means for the donor-exclusion endpoints: ``created_by`` / ``restored_by`` hold an identity the
client asserted, not one the server verified, and no role is checked (see
``recommendations.oar_exclusions.authorize_exclusion``). Do not describe those rows as an authenticated
audit trail until ``get_i7_actor`` validates a token.

When ``Principal`` exists (``feat/KP/Entra_id``), change ``get_i7_actor`` to build the :class:`Actor`
from the validated claims and call the permission check in ``authorize_exclusion``; no route changes.
"""

from fastapi import Depends

from app.api.deps import Actor, get_current_actor

__all__ = ["Actor", "get_i7_actor"]


def get_i7_actor(actor: Actor = Depends(get_current_actor)) -> Actor:
    """The acting user for an I07 write. See the module docstring for what this does and does not prove."""
    return actor
