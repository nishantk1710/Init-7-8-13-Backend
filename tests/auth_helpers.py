"""Test tokens for the sign-in layer (app/core/security.py).

Real Entra tokens are never needed in tests: the ``auth_enabled`` fixture
switches sign-in on and makes the API trust a key generated here, and
:func:`auth_headers` mints a token signed with it.

Typical use, in any test module::

    from tests.auth_helpers import auth_headers
    from app.core.security import Roles

    def test_stores_can_attest(auth_enabled, client):
        r = client.post("/api/i8/attestations", json=..., headers=auth_headers(roles=[Roles.STORES]))
        assert r.status_code == 201

    def test_viewer_cannot_attest(auth_enabled, client):
        r = client.post("/api/i8/attestations", json=..., headers=auth_headers(roles=[Roles.VIEWER]))
        assert r.status_code == 403

The fixtures are registered for every test by ``tests/conftest.py``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable, Iterator
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

TEST_TENANT = "11111111-1111-1111-1111-111111111111"
TEST_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
TEST_ISSUER = f"https://login.microsoftonline.com/{TEST_TENANT}/v2.0"
TEST_USER_OID = "33333333-3333-3333-3333-333333333333"
TEST_KID = "spares-test-key"

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = _PRIVATE_KEY.public_key()
#: A second key the API does NOT trust -- for "forged signature" tests.
OTHER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(
    roles: Iterable[str] = (),
    *,
    oid: str = TEST_USER_OID,
    name: str = "Test User",
    email: str = "test.user@example.com",
    scp: str | None = "access_as_user",
    aud: str = TEST_CLIENT_ID,
    iss: str = TEST_ISSUER,
    tid: str = TEST_TENANT,
    expires_in: int = 3600,
    application: bool = False,
    key: Any = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """An RS256 access token shaped like Entra's v2 tokens.

    ``application=True`` makes an app-only token (no ``scp``, no name) such as
    VZI's CPI would get with client credentials.
    """
    now = int(time.time())
    claims: dict[str, Any] = {
        "aud": aud,
        "iss": iss,
        "tid": tid,
        "oid": oid,
        "sub": oid,
        "iat": now,
        "nbf": now,
        "exp": now + expires_in,
        "ver": "2.0",
        "roles": list(roles),
        "azp": str(uuid.UUID(int=7)),
    }
    if application:
        claims["idtyp"] = "app"
    else:
        claims.update({"name": name, "preferred_username": email})
        if scp is not None:
            claims["scp"] = scp
    claims.update(extra or {})
    return jwt.encode(claims, key or _PRIVATE_KEY, algorithm="RS256", headers={"kid": TEST_KID})


def auth_headers(roles: Iterable[str] = (), **kwargs: Any) -> dict[str, str]:
    """``{"Authorization": "Bearer <token>"}`` for a user holding ``roles``."""
    return {"Authorization": f"Bearer {make_token(roles, **kwargs)}"}


def _configure(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    from app.core import security, user_directory
    from app.core.config import get_settings

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ENTRA_TENANT_ID", TEST_TENANT)
    monkeypatch.setenv("ENTRA_API_CLIENT_ID", TEST_CLIENT_ID)
    monkeypatch.setattr(security, "_signing_key", lambda token: PUBLIC_KEY)
    user_directory.forget_all()
    get_settings.cache_clear()


@pytest.fixture
def auth_enabled(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Sign-in switched on, trusting the test key. Settings restored afterwards."""
    from app.core.config import get_settings

    _configure(monkeypatch, AUTH_ENABLED="true")
    yield
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture
def sap_events_entra(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """SAP's event endpoints require an application token (SAP_EVENTS_AUTH=entra)."""
    from app.core.config import get_settings

    _configure(monkeypatch, SAP_EVENTS_AUTH="entra")
    yield
    monkeypatch.undo()
    get_settings.cache_clear()
