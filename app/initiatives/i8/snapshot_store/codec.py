"""The I08 snapshot to gzip-compressed JSON and back, exactly.

Self-describing: every value carries what it was, so decoding needs no schema
and gives back equal objects -- ``decode(encode(s)) == s`` for any snapshot.

    Decimal       {"$d": "1200.50"}         digits and exponent kept, so 1 != 1.000
    date          {"$date": "2026-10-01"}
    datetime      {"$dt": "2026-10-01T05:00:00+00:00"}
    tuple         {"$tuple": [...]}         a frozen dataclass's tuples stay tuples
    dict          {"$map": [[k, v], ...]}   keys may themselves be tuples
    dataclass     {"$t": "module:Class", "f": {field: value}}
    Enum          {"$enum": "module:Class", "v": value}

Only classes under ``app.initiatives.i8`` are ever constructed on decode. The
payload comes from our own table, but a codec that would import whatever a
row names is one bad row away from a problem.

``CODEC_VERSION`` is stored with each run: a version written by an older
layout is never decoded, the next build replaces it.
"""

from __future__ import annotations

import dataclasses
import gzip
import importlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

# 2 since 19-Oct-2026: RepairLine lost its receipt, delivery-flag and blocked
# fields and the Snapshot its vendor turnaround, so a version-1 payload no
# longer constructs. Raise it whenever a stored dataclass changes shape.
#
# 3 since 21-Oct-2026: RegisterStats gained excluded_rfq_lines. A version-2
# payload would still construct, the field has a default -- but it was built
# before RFQs were left out, and would go on serving the RFQ line as open until
# the source data next changed. Raising the version makes the deploy rebuild.
#
# 4 since 27-Oct-2026 (the I08 complete-flow branch; later items on it extend
# this note rather than raising the version again): the Snapshot gained
# repair_requests (requisitions.RepairRequest, the open repair PRs with no PO
# yet) and RegisterStats their counts. A version-3 payload would construct --
# both default -- but would serve no repair PRs until the source data next
# changed.
CODEC_VERSION = 4
_ALLOWED = "app.initiatives.i8."


def _name(cls: type) -> str:
    return f"{cls.__module__}:{cls.__qualname__}"


def _class(name: str) -> type:
    module, _, qualname = name.partition(":")
    if not module.startswith(_ALLOWED):
        raise ValueError(f"I08 snapshot codec: refusing to construct {name}")
    obj: Any = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _enc(value: Any) -> Any:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, Enum):
        return {"$enum": _name(type(value)), "v": _enc(value.value)}
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Decimal):
        return {"$d": str(value)}
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            "$t": _name(type(value)),
            "f": {f.name: _enc(getattr(value, f.name)) for f in dataclasses.fields(value) if f.init},
        }
    if isinstance(value, tuple):
        return {"$tuple": [_enc(v) for v in value]}
    if isinstance(value, list):
        return [_enc(v) for v in value]
    if isinstance(value, dict):
        return {"$map": [[_enc(k), _enc(v)] for k, v in value.items()]}
    raise TypeError(f"I08 snapshot codec: cannot store a {type(value).__name__}")


def _dec(value: Any) -> Any:
    if isinstance(value, list):
        return [_dec(v) for v in value]
    if not isinstance(value, dict):
        return value
    if "$d" in value:
        return Decimal(value["$d"])
    if "$dt" in value:
        return datetime.fromisoformat(value["$dt"])
    if "$date" in value:
        return date.fromisoformat(value["$date"])
    if "$tuple" in value:
        return tuple(_dec(v) for v in value["$tuple"])
    if "$map" in value:
        return {_hashable(_dec(k)): _dec(v) for k, v in value["$map"]}
    if "$enum" in value:
        return _class(value["$enum"])(_dec(value["v"]))
    if "$t" in value:
        return _class(value["$t"])(**{k: _dec(v) for k, v in value["f"].items()})
    raise ValueError(f"I08 snapshot codec: unknown value {sorted(value)}")


def _hashable(key: Any) -> Any:
    return tuple(key) if isinstance(key, list) else key


def encode(value: Any) -> bytes:
    """``value`` as gzip-compressed JSON."""
    raw = json.dumps(_enc(value), separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return gzip.compress(raw, compresslevel=6)


def decode(payload: bytes) -> Any:
    """What :func:`encode` was given."""
    return _dec(json.loads(gzip.decompress(payload)))
