"""Sign-in and role checks: Microsoft Entra ID access tokens.

One shared place for both concerns, so no initiative reinvents them:

1. **People (web app -> API).** The web app signs the user in with Entra ID and
   sends the access token it got for this API (``spares-ai-api-<env>``) as
   ``Authorization: Bearer``. This module validates it -- signature against the
   tenant's published keys, issuer, audience, expiry, tenant -- and turns its
   claims into a :class:`CurrentUser`: object ID, name, e-mail and the app
   roles Entra put in the token (``Spares.*``, see :class:`Roles`).

2. **SAP's calls (CPI -> API).** ``/api/events/pr`` and ``/api/events/csv`` are
   switched separately (``SAP_EVENTS_AUTH``): how CPI authenticates is decision
   D2 in the RBAC plan. ``entra`` means an application token (client
   credentials) carrying the ``Sap.Events.Write`` role.

How a route declares its requirement
------------------------------------
Reads need no code: every route mounted in ``app.api.router`` -- except the
health checks, ``/api/me`` and SAP's events -- carries :func:`require_signed_in`,
which admits any signed-in person holding at least one ``Spares.*`` role.

Writes declare the roles that may perform them::

    @router.post("/attestations",
                 dependencies=[Depends(require_roles(Roles.STORES))])

or, when the route also needs to know who is acting::

    def attest(user: CurrentUser = Depends(require_roles(Roles.STORES))): ...

A user holding ANY of the listed roles passes. ``tests/test_write_roles.py``
fails if a write route declares nothing (apart from the ones still listed as
pending there), so a new endpoint cannot ship unprotected.

Switched off (the default)
--------------------------
With ``AUTH_ENABLED=false`` -- locally, in tests, and on any environment not yet
configured -- nothing is checked: :func:`current_user` returns the
``X-Actor-Id`` header as the user, holding every role, and every
``require_*`` dependency lets the request through. That is exactly the
behaviour before sign-in existed, which is what makes switching it back off the
rollback.

Never log a token. 401s and 403s are logged with the route and the user's
object ID only.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Roles:
    """The app roles defined on the API's app registration (``spares-ai-api-<env>``).

    The values are what Entra puts in the token and what the code checks, so
    they must never change once in use. Role catalogue: RBAC plan, section 4.
    """

    VIEWER = "Spares.Viewer"
    REQUESTER = "Spares.Requester"
    STORES = "Spares.Stores"
    HOD = "Spares.HOD"
    COMMERCIAL = "Spares.Commercial"
    INVENTORY_CONTROLLER = "Spares.InventoryController"
    # I07 approval stages -- one role per stage of either approval chain.
    END_USER = "Spares.EndUser"
    ENGINEERING_MANAGER = "Spares.EngineeringManager"
    COMMERCIAL_MANAGER = "Spares.CommercialManager"
    WAREHOUSE_SUPERVISOR = "Spares.WarehouseSupervisor"
    INVENTORY_CONTROLLER_APPROVER = "Spares.InventoryControllerApprover"
    COMMERCIAL_HEAD = "Spares.CommercialHead"
    ENGINEERING_HEAD = "Spares.EngineeringHead"
    PLANT_HEAD = "Spares.PlantHead"
    ADMIN = "Spares.Admin"
    # Granted to VZI's CPI application, never to a person.
    SAP_EVENTS_WRITE = "Sap.Events.Write"


#: The eight I07 approval-stage roles, in no particular order -- a route
#: (``app.initiatives.i7.recommendations``) defines a chain's order.
APPROVAL_STAGE_ROLES: frozenset[str] = frozenset(
    {
        Roles.END_USER,
        Roles.ENGINEERING_MANAGER,
        Roles.COMMERCIAL_MANAGER,
        Roles.WAREHOUSE_SUPERVISOR,
        Roles.INVENTORY_CONTROLLER_APPROVER,
        Roles.COMMERCIAL_HEAD,
        Roles.ENGINEERING_HEAD,
        Roles.PLANT_HEAD,
    }
)

#: Every role a person can hold. Holding any one of them grants read access.
USER_ROLES: frozenset[str] = frozenset(
    {
        Roles.VIEWER,
        Roles.REQUESTER,
        Roles.STORES,
        Roles.HOD,
        Roles.COMMERCIAL,
        Roles.INVENTORY_CONTROLLER,
        Roles.ADMIN,
    }
) | APPROVAL_STAGE_ROLES

#: Every role defined on the app registration.
ALL_ROLES: frozenset[str] = USER_ROLES | {Roles.SAP_EVENTS_WRITE}


@dataclass(frozen=True)
class CurrentUser:
    """Who is calling, as the API sees it.

    ``oid`` is the stable Entra object ID (36 characters) -- the value recorded
    as the author of anything this user writes. ``name`` and ``email`` are for
    display only; they can change, the object ID does not.

    ``authenticated`` is False only when sign-in is switched off. The user then
    holds every role, so :meth:`has_any` is always True and no rule blocks.
    """

    oid: str
    name: str = ""
    email: str = ""
    roles: frozenset[str] = frozenset()
    is_application: bool = False
    authenticated: bool = True

    def has_any(self, *roles: str) -> bool:
        """True when the user holds at least one of ``roles``; always True with sign-in off."""
        if not self.authenticated:
            return True
        return not self.roles.isdisjoint(roles)


# ---------------------------------------------------------------------------
# Token validation
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """A token was missing or did not validate. The message is logged, never returned."""


def _issuer(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/v2.0"


def _jwks_url(tenant: str) -> str:
    return f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"


_jwks_clients: dict[tuple[str, int], jwt.PyJWKClient] = {}
_jwks_lock = threading.Lock()


def _signing_key(token: str) -> Any:
    """The public key that should have signed ``token``, from the tenant's key set.

    ``PyJWKClient`` caches the key set and fetches it again when the token
    names a key ID it does not hold, so Entra's key rotation needs nothing from
    us. Tests replace this function with one returning their own key.
    """
    settings = get_settings()
    cache_key = (settings.azure_tenant_id, settings.auth_jwks_cache_seconds)
    with _jwks_lock:
        client = _jwks_clients.get(cache_key)
        if client is None:
            client = jwt.PyJWKClient(
                _jwks_url(settings.azure_tenant_id),
                cache_jwk_set=True,
                lifespan=max(settings.auth_jwks_cache_seconds, 60),
                timeout=10,
            )
            _jwks_clients[cache_key] = client
    try:
        return client.get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientError as exc:
        raise AuthError(f"signing key not found: {exc}") from exc


def configuration_problem() -> str | None:
    """Why token validation cannot run, or None. Checked on every validation."""
    settings = get_settings()
    missing = [
        name
        for name, value in (("AZURE_TENANT_ID", settings.azure_tenant_id), ("AZURE_CLIENT_ID", settings.azure_client_id))
        if not value.strip()
    ]
    return f"{' and '.join(missing)} not set" if missing else None


def accepted_audiences() -> list[str]:
    settings = get_settings()
    client_id = settings.azure_client_id.strip()
    extra = [a.strip() for a in settings.auth_audience.split(",") if a.strip()]
    return list(dict.fromkeys([client_id, f"api://{client_id}", *extra]))


def validate_token(token: str) -> dict[str, Any]:
    """The claims of a valid access token for this API; :class:`AuthError` otherwise.

    Checked: RS256 signature against the tenant's keys, issuer (the tenant's v2
    endpoint), audience (:func:`accepted_audiences`), ``exp``/``nbf``/``iat``
    with the configured clock skew, and that ``tid`` is the tenant.
    """
    settings = get_settings()
    tenant = settings.azure_tenant_id.strip()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError(f"not a JWT: {exc}") from exc
    if header.get("alg") != "RS256":
        raise AuthError(f"unexpected algorithm {header.get('alg')!r}")
    key = _signing_key(token)
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=accepted_audiences(),
            issuer=_issuer(tenant),
            leeway=settings.auth_clock_skew_seconds,
            options={"require": ["exp", "iat", "aud", "iss", "oid"]},
        )
    except jwt.PyJWTError as exc:
        raise AuthError(f"{type(exc).__name__}: {exc}") from exc
    if claims.get("tid") != tenant:
        raise AuthError("token issued for another tenant")
    return claims


def user_from_claims(claims: dict[str, Any]) -> CurrentUser:
    """A person (delegated token, has ``scp``) or an application (no ``scp``).

    A person's token must carry the API's delegated scope, so a token the user
    obtained for some other purpose cannot be replayed here.
    """
    settings = get_settings()
    roles = frozenset(r for r in claims.get("roles", []) if isinstance(r, str))
    scopes = claims.get("scp")
    if scopes is None:
        return CurrentUser(
            oid=str(claims["oid"]),
            name=str(claims.get("azp") or claims.get("appid") or ""),
            roles=roles,
            is_application=True,
        )
    if settings.auth_required_scope not in str(scopes).split():
        raise AuthError(f"token lacks scope {settings.auth_required_scope!r}")
    return CurrentUser(
        oid=str(claims["oid"]),
        name=str(claims.get("name") or ""),
        email=str(claims.get("preferred_username") or claims.get("email") or claims.get("upn") or ""),
        roles=roles,
    )


# ---------------------------------------------------------------------------
# Request-level helpers and FastAPI dependencies
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str | None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def _unauthorized(request: Request, reason: str) -> HTTPException:
    logger.warning("auth: 401 %s %s -- %s", request.method, request.url.path, reason)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Sign-in required: a valid Microsoft Entra ID access token was not provided.",
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


def _misconfigured(request: Request, problem: str) -> HTTPException:
    logger.error("auth: sign-in is switched on but %s -- refusing %s %s", problem, request.method, request.url.path)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"Sign-in is switched on but not configured ({problem}).",
    )


def _forbidden(request: Request, user: CurrentUser, needed: Iterable[str]) -> HTTPException:
    needed = sorted(needed)
    logger.warning("auth: 403 %s %s -- %s holds none of %s", request.method, request.url.path, user.oid, needed)
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"message": f"This action requires one of these roles: {', '.join(needed)}.", "requiredRoles": needed},
    )


def authenticate(request: Request) -> CurrentUser:
    """Validate the request's bearer token -- whatever ``AUTH_ENABLED`` says.

    Raises 503 when the tenant or client ID is missing and 401 when the token
    is absent or invalid. Cached on the request, so several dependencies on one
    route validate once.
    """
    cached = getattr(request.state, "spares_token_user", None)
    if cached is not None:
        return cached
    problem = configuration_problem()
    if problem:
        raise _misconfigured(request, problem)
    token = _bearer_token(request)
    if token is None:
        raise _unauthorized(request, "no bearer token")
    try:
        user = user_from_claims(validate_token(token))
    except AuthError as exc:
        raise _unauthorized(request, str(exc)) from exc
    if not user.is_application:
        from app.core import user_directory

        user_directory.remember(user)
    request.state.spares_token_user = user
    return user


def _signed_off_user(request: Request) -> CurrentUser:
    actor = (request.headers.get("x-actor-id") or "").strip() or "unknown-actor"
    return CurrentUser(oid=actor, name=actor, roles=USER_ROLES, authenticated=False)


def current_user(request: Request) -> CurrentUser:
    """FastAPI dependency: who is calling.

    Sign-in on: the validated token's user (401 without one). Sign-in off: the
    ``X-Actor-Id`` header, holding every role -- the behaviour before sign-in.
    """
    if not get_settings().auth_enabled:
        return _signed_off_user(request)
    return authenticate(request)


def require_roles(*roles: str) -> Callable[[Request], CurrentUser]:
    """FastAPI dependency factory: the caller must hold at least one of ``roles``.

    Returns the :class:`CurrentUser`, so a route can use it both as a guard and
    to learn who is acting. An unknown role name fails at import, so a typo
    cannot silently lock a route or open it.
    """
    if not roles:
        raise ValueError("require_roles() needs at least one role")
    unknown = sorted(set(roles) - ALL_ROLES)
    if unknown:
        raise ValueError(f"unknown role(s) {unknown}; see app.core.security.Roles")
    needed = frozenset(roles)

    def dependency(request: Request) -> CurrentUser:
        user = current_user(request)
        if user.authenticated and user.roles.isdisjoint(needed):
            raise _forbidden(request, user, needed)
        return user

    dependency.__required_roles__ = needed  # type: ignore[attr-defined]  # read by tests/test_write_roles.py
    dependency.__name__ = "require_" + "_or_".join(sorted(r.split(".")[-1] for r in needed))
    return dependency


def require_signed_in(request: Request) -> CurrentUser:
    """FastAPI dependency: any signed-in person holding at least one ``Spares.*`` role.

    The read rule, mounted on whole routers in ``app.api.router``. Application
    tokens do not pass -- CPI's token only opens SAP's events.
    """
    user = current_user(request)
    if user.authenticated and (user.is_application or user.roles.isdisjoint(USER_ROLES)):
        raise _forbidden(request, user, USER_ROLES)
    return user


require_signed_in.__signed_in_rule__ = True  # type: ignore[attr-defined]  # read by tests/test_write_roles.py


def require_sap_events(request: Request) -> CurrentUser | None:
    """FastAPI dependency for SAP's ``/api/events/*`` calls, switched by ``SAP_EVENTS_AUTH``.

    ``off``: open, as before (returns None). ``entra``: an application token with
    the ``Sap.Events.Write`` role -- independent of ``AUTH_ENABLED``, so SAP's
    side can be switched on before or after people's sign-in.
    """
    if get_settings().sap_events_auth != "entra":
        return None
    caller = authenticate(request)
    if not caller.is_application or Roles.SAP_EVENTS_WRITE not in caller.roles:
        raise _forbidden(request, caller, {Roles.SAP_EVENTS_WRITE})
    return caller


require_sap_events.__required_roles__ = frozenset({Roles.SAP_EVENTS_WRITE})  # type: ignore[attr-defined]


def actor_id(request: Request, header_value: str | None) -> str:
    """The author to record for a write: the signed-in user's object ID, or
    (sign-in off) the ``X-Actor-Id`` header as before. Used by both
    ``get_current_actor`` dependencies."""
    if get_settings().auth_enabled:
        return authenticate(request).oid
    return (header_value or "").strip() or "unknown-actor"
