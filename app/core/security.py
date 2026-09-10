"""Authentication and authorization placeholder.

Nothing is implemented here yet, deliberately. This module exists so that when
auth does land it lands in one shared place rather than being reinvented inside
I07, I08 and I13.

Two distinct concerns will live here:

1. **Application users (frontend -> backend).**
   Expected to be Azure Entra ID: the frontend acquires a token, the backend
   validates it (issuer, audience, signature via the tenant JWKS) and maps
   claims to roles. Configuration placeholders already exist as
   ``AZURE_TENANT_ID`` / ``AZURE_CLIENT_ID`` in ``app.core.config``.

2. **Inbound SAP callbacks (SAP -> backend).**
   The mechanism is *not decided*. It could be OAuth client credentials, a
   shared API key, mTLS, or private-network trust with no application-level
   credential at all. Do not implement or assume one until the SAP/VZI team
   confirms it -- each choice implies a different validation path, a different
   secret-rotation story and different infrastructure.

Until then ``POST /api/events/pr`` is intentionally unauthenticated: it is a
connectivity stub and must not encode an auth assumption that later turns out
to be wrong.

When implementing, expose FastAPI dependencies from this module, e.g.::

    async def require_user(...) -> User: ...
    async def require_sap_caller(...) -> None: ...

so routes declare their requirement and the mechanism stays swappable.
"""
