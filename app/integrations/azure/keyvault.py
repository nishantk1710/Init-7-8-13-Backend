"""Is Key Vault reachable, and may this identity read it?

This is a *diagnostic*, not a secrets client. Nothing in this codebase reads a
secret from Key Vault, and that is deliberate: secrets arrive as App Service
Key Vault references, which App Service resolves into ordinary environment
variables before the process starts. ``Settings`` then reads them like any other
variable, so no SDK, no caching and no refresh logic are needed.

The cost of that design is the reason this file exists. Because the app never
talks to the vault, it cannot report whether the vault is reachable -- and
"confirm the app can reach storage, SQL and Key Vault" is one of the three
things the infrastructure handover asked us to verify. So this probes it
directly, with the same managed identity, purely to answer that question.

It uses ``azure-identity`` (already required for Data Lake) plus one REST call
rather than adding ``azure-keyvault-secrets`` for a check that runs by hand.
"""

from __future__ import annotations

import requests
from azure.identity import DefaultAzureCredential

# The vault data-plane audience. Correct for the public cloud, which is where
# kv-vzi-aicom-nonprod lives; a sovereign cloud would need a different one.
VAULT_SCOPE = "https://vault.azure.net/.default"
API_VERSION = "7.4"


class KeyVaultUnreachable(RuntimeError):
    """The vault did not answer, or would not let this identity read it."""


def probe(vault_url: str, *, timeout: int = 15) -> str:
    """List secret names. Returns a one-line summary; raises on failure.

    Listing rather than reading a specific secret: it needs only the "list"
    permission, names no secret this code has any business knowing, and still
    exercises DNS, the private endpoint, the credential and the role assignment
    -- the four things that are actually in question on a first deployment.
    """
    url = vault_url.rstrip("/")
    token = DefaultAzureCredential().get_token(VAULT_SCOPE)

    response = requests.get(
        f"{url}/secrets?api-version={API_VERSION}",
        headers={"Authorization": f"Bearer {token.token}"},
        timeout=timeout,
    )

    if response.status_code == 403:
        raise KeyVaultUnreachable(
            "reachable, but this identity is not authorised. Grant it "
            "'Key Vault Secrets User' on kv-vzi-aicom-nonprod."
        )
    if response.status_code == 401:
        raise KeyVaultUnreachable(
            "the vault rejected the token. On App Service this usually means no "
            "managed identity is enabled."
        )

    response.raise_for_status()
    count = len(response.json().get("value", []))
    return f"reachable and readable; {count} secret(s) visible"
