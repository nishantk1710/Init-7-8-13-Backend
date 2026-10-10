"""Dataclass <-> compact JSON, for ``i13_snapshot_record.payload``.

An item is stored as a JSON array of its field values in declaration order --
no field names, so a WATCH row is a few hundred bytes rather than a thousand,
over a few million rows. Decoding reads the same field list back with the
dataclass's own type hints, so a ``Decimal`` comes back a ``Decimal``, a date a
``date``, an enum its member: the routes cannot tell a decoded item from the
one the in-memory snapshot would have held.

Because the layout is positional, a field added, removed or reordered changes
what a stored array means. :func:`schema_signature` hashes every stored type's
field list; a run records it, and the reader refuses a version written under a
different one (a rebuild replaces it) rather than misreading it.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import types
import typing
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from functools import cache
from typing import Any, TypeVar

T = TypeVar("T")


def _encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        # str() keeps the exact scale ("29.000"), which the API echoes.
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_encode_value(v) for v in value]
    if dataclasses.is_dataclass(value):
        return encode(value)
    if isinstance(value, float):
        return value
    raise TypeError(f"cannot store a {type(value).__name__} in an I13 snapshot record")


def encode(item: Any) -> list[Any]:
    """A dataclass instance as a positional list."""
    return [_encode_value(getattr(item, f.name)) for f in dataclasses.fields(item)]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


@cache
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _decode_value(raw: Any, hint: Any) -> Any:
    if raw is None:
        return None
    origin = typing.get_origin(hint)
    if origin in (typing.Union, types.UnionType):
        options = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(options) == 1:
            return _decode_value(raw, options[0])
        return raw
    if origin in (list, tuple):
        args = typing.get_args(hint)
        if origin is tuple and args and args[-1] is not Ellipsis:
            return tuple(_decode_value(v, a) for v, a in zip(raw, args))
        inner = args[0] if args else Any
        values = [_decode_value(v, inner) for v in raw]
        return tuple(values) if origin is tuple else values
    if hint is Any:
        return raw
    if isinstance(hint, type):
        if issubclass(hint, Enum):
            return hint(raw)
        if hint is Decimal:
            return Decimal(raw)
        if hint is datetime:
            return datetime.fromisoformat(raw)
        if hint is date:
            return date.fromisoformat(raw)
        if dataclasses.is_dataclass(hint):
            return decode(hint, raw)
    return raw


def decode(cls: type[T], values: list[Any]) -> T:
    """The inverse of :func:`encode`."""
    hints = _hints(cls)
    fields = [f for f in dataclasses.fields(cls) if f.init]
    kwargs = {f.name: _decode_value(v, hints[f.name]) for f, v in zip(fields, values)}
    return cls(**kwargs)


def schema_signature(classes: typing.Iterable[type]) -> str:
    """A short hash of every class's field names and types, in order."""
    parts = []
    for cls in classes:
        hints = _hints(cls)
        parts.append(cls.__qualname__ + ":" + ",".join(f"{f.name}={hints[f.name]}" for f in dataclasses.fields(cls)))
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]
