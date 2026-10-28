"""Dependencies shared by routes that belong to no single initiative."""

from dataclasses import dataclass

from fastapi import Header, Request

from app.core import security


@dataclass(frozen=True)
class Actor:
    """Who is acting. See :func:`get_current_actor`."""

    id: str
    type: str = "USER"


def get_current_actor(
    request: Request, x_actor_id: str | None = Header(default=None, alias="X-Actor-Id")
) -> Actor:
    """Who is acting, for the author column of a write.

    Sign-in on (``AUTH_ENABLED``): the signed-in user's Entra object ID, from
    the validated token -- the header is ignored, so a browser cannot claim to
    be someone else. Sign-in off: the ``X-Actor-Id`` header as before, never a
    request-body field. See ``app.core.security.actor_id``.

    The same behaviour as ``app.api.i13.deps.get_current_actor``, kept here so a
    shared route does not depend on an initiative's package.
    """
    return Actor(id=security.actor_id(request, x_actor_id))
