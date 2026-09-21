"""Prove every dependency from wherever this is running.

    python -m app.checkup

Checks the database, object storage, Key Vault, CPI and the model, and
reports each one separately. Written to be run from the App Service SSH console
the moment a deployment lands -- which is precisely what W2.2 asks for: a live
connectivity check from inside the VZI environment rather than from a laptop.

**Where it runs is the whole point.** Every dependency except the model sits
behind a private endpoint, so this command passing on a developer machine and
failing in the App Service -- or the reverse -- is the expected outcome, not a
contradiction. A laptop reaches CPI and Foundry and cannot reach SQL, storage or
Key Vault. The App Service is the only place all five can be green at once, and
until this has been run there, nothing has been proven end to end.

It prints no secret. Keys are reported by length and last four characters, and
the database URL by host and database name only, so the output is safe to paste
into a status update.
"""

from __future__ import annotations

import sys
import time
from typing import Callable

from app.core.config import get_settings

# Tried in order until one answers. The first is the smallest set that has been
# consistently readable; the others are fallbacks so a single entity set going
# unavailable does not make the whole connectivity check look like a failure.
CPI_PROBE_SETS = ("VendorSet", "MaterialSet", "MaterialPlantSet")


def _mask(value: str) -> str:
    if not value:
        return "(not set)"
    return f"set, {len(value)} chars, ending {value[-4:]}"


def _safe_database_url(url: str) -> str:
    """Host and database only. The URL carries a password."""
    if not url:
        return "(not set)"
    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(url)
        return f"{parsed.get_backend_name()}://{parsed.host}/{parsed.database}"
    except Exception:
        return "(unparseable)"


def _run(label: str, check: Callable[[], str]) -> bool:
    """Run one check, print its outcome and timing, and never raise."""
    started = time.monotonic()
    try:
        detail = check()
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        message = " ".join(str(exc).split())[:200]
        print(f"  {label:10} FAILED  ({elapsed}ms)  {type(exc).__name__}: {message}")
        return False

    elapsed = int((time.monotonic() - started) * 1000)
    print(f"  {label:10} ok      ({elapsed}ms)  {detail}")
    return True


# --- The checks -----------------------------------------------------------


def _check_database() -> str:
    from sqlalchemy import text

    from app.core.db import get_engine

    with get_engine().connect() as connection:
        version = connection.execute(text("SELECT @@VERSION")).scalar() or ""
        tables = connection.execute(
            text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_NAME LIKE 'raw_%'"
            )
        ).scalar()
    edition = version.split("\n")[0].strip()[:60]
    return f"{tables} raw table(s); {edition}"


def _check_storage() -> str:
    from app.core.storage import get_storage

    storage = get_storage()
    storage.check_connection()
    # Cheap, and answers the question that actually matters on day one: are the
    # extracts in the container yet, or is it reachable but empty?
    sample = []
    for key in storage.list():
        sample.append(key)
        if len(sample) >= 3:
            break
    if not sample:
        return "reachable, but no objects found -- extracts not uploaded yet"
    return f"reachable; e.g. {', '.join(sample)}"


def _check_cpi() -> str:
    from app.integrations.sap.client import SapClient

    client = SapClient()
    errors = []
    for name in CPI_PROBE_SETS:
        try:
            page = client.read(name, top=1)
            return f"token acquired, {name} returned {len(page)} row(s)"
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    raise RuntimeError(f"no probe set answered ({'; '.join(errors)})")


def _unresolved_key_vault_references() -> list[str]:
    """Settings whose value is still a literal @Microsoft.KeyVault(...) reference.

    This is the failure mode that actually happens. App Service resolves a Key
    Vault reference before the process starts; when it cannot -- no managed
    identity, no 'Key Vault Secrets User' role, or no network path to the vault
    -- it does not fail the start. It hands the app the reference text itself.
    The app then tries to connect to a database whose password is the literal
    string "@Microsoft.KeyVault(SecretUri=...)", and the error says nothing
    about Key Vault.
    """
    settings = get_settings()
    return [
        name.upper()
        for name in type(settings).model_fields
        if isinstance(getattr(settings, name, None), str)
        and getattr(settings, name).startswith("@Microsoft.KeyVault")
    ]


def _check_key_vault() -> str:
    """Two questions, in the order they actually bite.

    First: did App Service resolve its Key Vault references? That is the failure
    that happens silently and is worth catching even when the vault itself is
    fine. Second, and only if KEY_VAULT_URL is set: is the vault reachable from
    here at all?
    """
    unresolved = _unresolved_key_vault_references()
    if unresolved:
        raise RuntimeError(
            "App Service did not resolve Key Vault references for: "
            f"{', '.join(unresolved)}. The app is running with the reference "
            "text as the value. Check the managed identity has 'Key Vault "
            "Secrets User' on the vault."
        )

    url = get_settings().key_vault_url
    if not url:
        return "no reference left unresolved (KEY_VAULT_URL unset, so not probed directly)"

    # Imported here, not at module scope: the cloud SDK lives behind an adapter
    # and this module must not depend on one being installed to start.
    from app.integrations.azure.keyvault import probe

    return probe(url)


def _check_ai() -> str:
    from app.core.ai import Message, get_llm

    result = get_llm().complete([Message("user", "Reply with exactly: ok")], max_tokens=16)
    return (
        f"{result.model} answered "
        f"({result.usage.input_tokens} in / {result.usage.output_tokens} out)"
    )


def main() -> int:
    settings = get_settings()

    print("Configuration")
    print(f"  database   : {_safe_database_url(settings.database_url)}")
    print(f"  storage    : {settings.storage_url or '(not set)'}")
    print(f"  cpi        : {settings.cpi_base_url or '(not set)'}")
    print(f"  cpi secret : {_mask(settings.cpi_client_secret)}")
    print(f"  key vault  : {settings.key_vault_url or '(not probed directly)'}")
    print(f"  llm        : {settings.llm_provider} -> {settings.foundry_deployment or '(none)'}")
    print(f"  llm key    : {_mask(settings.foundry_api_key)}")

    print("\nChecks")
    results = {
        "database": _run("database", _check_database),
        "storage": _run("storage", _check_storage),
        "key vault": _run("key vault", _check_key_vault),
        "cpi": _run("cpi", _check_cpi),
        "ai": _run("ai", _check_ai),
    }

    failed = [name for name, ok in results.items() if not ok]

    print()
    if not failed:
        print("All five dependencies answered.")
        print(
            "If this ran inside app-vzi-aicom-nonprod-san, that is W2.2: a live "
            "pull from within the VZI environment."
        )
        return 0

    print(f"{len(failed)} of 5 failed: {', '.join(failed)}")
    print(
        "Outside the VNet, database, storage and key vault are EXPECTED to fail "
        "-- all three sit behind private endpoints. Run this from the App "
        "Service SSH console before treating any of them as a real fault."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
