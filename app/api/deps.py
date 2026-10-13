"""Dependencies shared by routes that belong to no single initiative."""

from dataclasses import dataclass

from fastapi import Header


@dataclass(frozen=True)
class Actor:
    """Who is acting. See :func:`get_current_actor`."""

    id: str
    type: str = "USER"


def get_current_actor(x_actor_id: str | None = Header(default=None, alias="X-Actor-Id")) -> Actor:
    """Placeholder identity resolution -- no authentication is implemented on
    this branch (see ``app/core/security.py``). Reads a client-supplied header
    rather than trusting a request-body field, so identity is not simply
    whatever an arbitrary JSON payload claims.

    The same behaviour as ``app.api.i13.deps.get_current_actor``, kept here so a
    shared route does not depend on an initiative's package. When Entra ID is
    wired in, this is the one function that changes: it validates a token and
    builds the same ``Actor`` from its claims.
    """
    return Actor(id=(x_actor_id or "").strip() or "unknown-actor")
