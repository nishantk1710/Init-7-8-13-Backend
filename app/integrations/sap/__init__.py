"""SAP integration, through the CPI generic OData consumption endpoint.

One adapter for the whole application. Initiative code calls ``SapClient``; it
never builds a URL, holds a token, or parses an OData envelope.

    from app.integrations.sap import SapClient

    client = SapClient()
    result = client.read_all("MaterialPlantSet", filter="Dismm eq 'ND'")

Layers, bottom up:

    errors.py      the taxonomy: what is retryable, what is drift
    token.py       OAuth client-credentials, cached, refreshed on 401
    transport.py   the one HTTP door -- APIPath/APIQuery, retry, Accept
    envelope.py    OData v2 unwrapping, decoding BY DECLARED TYPE
    contract.py    entity set -> service, keys, property types
    filters.py     refuses filters SAP silently ignores
    paging.py      ordered extraction, with the $count fallback
    edmx.py        parses $metadata into the contract's shape
    drift.py       compares two contracts and says how they differ
    known_conditions.py  what we have measured, so a change fails loudly
    client.py      the public face: read, count, read_all, metadata

Read-only by design. P1 forbids SAP write-back programme-wide, and nothing in
this package issues anything but GET.
"""

from app.integrations.sap.client import ReadResult, SapClient
from app.integrations.sap.contract import EntitySet, Property, contract, entity_set
from app.integrations.sap.drift import Difference, Severity, compare_contracts, describe
from app.integrations.sap.edmx import parse_metadata, parse_snapshot
from app.integrations.sap.paging import ExtractResult
from app.integrations.sap.errors import (
    AuthError,
    ContractError,
    NotFoundError,
    RequestError,
    SapError,
    TransientError,
    UnsupportedFilterError,
)

__all__ = [
    "AuthError",
    "ContractError",
    "Difference",
    "EntitySet",
    "ExtractResult",
    "NotFoundError",
    "Property",
    "ReadResult",
    "RequestError",
    "SapClient",
    "SapError",
    "TransientError",
    "UnsupportedFilterError",
    "Severity",
    "compare_contracts",
    "contract",
    "describe",
    "entity_set",
    "parse_metadata",
    "parse_snapshot",
]
