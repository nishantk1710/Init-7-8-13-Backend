"""Unwrapping OData v2 responses, and decoding values by their declared type.

Two jobs, both easy to get subtly wrong.

**The double envelope.** OData v2 wraps rows twice::

    {"d": {"results": [ {...}, {...} ]}}

and a single entity omits ``results``::

    {"d": {...}}

Callers should never see either form.

**Decoding by declared type, never by shape.** This is the rule the frontend
learned the hard way: ``PurchaseOrderItemSet.Netpr`` and ``.Netwr`` changed from
``Edm.Decimal`` to ``Edm.String`` between two sweeps. Code that decided "it looks
like a number, so parse it" kept working and silently changed behaviour. Code
that asks the contract what the type IS did not.

So every value here is decoded against ``contract.py``. An unknown property is
reported rather than dropped -- a new SAP field appearing is drift worth
knowing about, not something to silently ignore.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.integrations.sap.contract import EntitySet
from app.integrations.sap.errors import ContractError

# SAP serialises Edm.DateTime as /Date(<epoch millis>)/, optionally with a
# trailing offset: /Date(1379030400000+0000)/
_SAP_DATE = re.compile(r"^/Date\((?P<millis>-?\d+)(?P<offset>[+-]\d{4})?\)/$")

# Edm.Time as an ISO 8601 duration since midnight: PT14H30M00S
_SAP_TIME = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$")


@dataclass
class DecodedRows:
    """Rows plus what the response said that the contract did not expect."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    unknown_properties: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.rows)


def _decode_datetime(raw: str) -> datetime | str:
    match = _SAP_DATE.match(raw)
    if not match:
        # Some projections return ISO already. Try it before giving up.
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return raw
    moment = datetime.fromtimestamp(int(match.group("millis")) / 1000, tz=timezone.utc)
    offset = match.group("offset")
    if offset and offset != "+0000":
        sign = 1 if offset[0] == "+" else -1
        moment += sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[3:5]))
    return moment


def _decode_time(raw: str) -> str:
    match = _SAP_TIME.match(raw)
    if not match:
        return raw
    hours, minutes, seconds = (match.group(1) or "0", match.group(2) or "0", match.group(3) or "0")
    return f"{int(hours):02d}:{int(minutes):02d}:{int(float(seconds)):02d}"


def decode_value(raw: Any, edm_type: str) -> Any:
    """One value, decoded according to its DECLARED type.

    Never inspects the value to guess a type. ``None`` passes through: SAP's
    null and a missing property are the same absence.
    """
    if raw is None:
        return None

    if edm_type in ("Edm.Decimal", "Edm.Double", "Edm.Single"):
        # Arrives as a string more often than not, and Decimal(str) is exact --
        # float() on a price is a rounding bug waiting for a big enough number.
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise ContractError(
                f"Value {raw!r} is not decodable as {edm_type}"
            ) from None

    if edm_type in ("Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte"):
        try:
            return int(str(raw).strip() or 0)
        except ValueError:
            raise ContractError(f"Value {raw!r} is not decodable as {edm_type}") from None

    if edm_type == "Edm.Boolean":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().upper() in ("X", "TRUE", "1")

    if edm_type in ("Edm.DateTime", "Edm.DateTimeOffset"):
        return _decode_datetime(str(raw))

    if edm_type == "Edm.Time":
        return _decode_time(str(raw))

    if edm_type == "Edm.Guid":
        return str(raw)

    # Edm.String and anything unrecognised: keep it exactly as sent. SAP keys
    # are zero-padded digit strings and any coercion destroys them.
    return raw if isinstance(raw, str) else str(raw)


def _rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or "d" not in payload:
        raise ContractError(
            "Response is not an OData v2 envelope: no top-level 'd'. "
            "Check the Accept header and that APIQuery asked for $format=json."
        )
    inner = payload["d"]
    if isinstance(inner, dict) and "results" in inner:
        results = inner["results"]
        if not isinstance(results, list):
            raise ContractError("OData envelope 'd.results' is not a list")
        return [row for row in results if isinstance(row, dict)]
    if isinstance(inner, dict):
        return [inner]  # single-entity read
    raise ContractError("OData envelope 'd' is neither an object nor a result set")


def decode_rows(body: str, entity_set: EntitySet) -> DecodedRows:
    """Parse a JSON feed and decode every value against the contract."""
    try:
        payload = json.loads(body)
    except ValueError as exc:
        preview = body[:200].replace("\n", " ")
        raise ContractError(f"Response was not JSON: {preview!r}") from exc

    known = {p.name: p.type for p in entity_set.properties}
    decoded: list[dict[str, Any]] = []
    unknown: set[str] = set()

    for raw_row in _rows_from_payload(payload):
        row: dict[str, Any] = {}
        for name, value in raw_row.items():
            if name == "__metadata":
                # OData bookkeeping, not data.
                continue
            edm_type = known.get(name)
            if edm_type is None:
                # A property the contract does not know about. Keep the value --
                # discarding data is worse -- but report it as drift.
                unknown.add(name)
                row[name] = value
                continue
            row[name] = decode_value(value, edm_type)
        decoded.append(row)

    return DecodedRows(rows=decoded, unknown_properties=sorted(unknown))


def decode_count(body: str, context: str) -> int:
    """A ``$count`` response, which is a bare number as text."""
    text = body.strip().strip('"')
    try:
        return int(text)
    except ValueError:
        raise ContractError(
            f"{context}: $count did not return a number, got {text[:120]!r}"
        ) from None


__all__ = [
    "DecodedRows",
    "decode_count",
    "decode_rows",
    "decode_value",
    "date",  # re-exported for callers typing decoded values
]
