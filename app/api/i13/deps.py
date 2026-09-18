"""Shared FastAPI dependencies for the Initiative 13 routes."""

from dataclasses import dataclass
from pathlib import Path

from fastapi import Header

from app.core.config import get_settings


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
