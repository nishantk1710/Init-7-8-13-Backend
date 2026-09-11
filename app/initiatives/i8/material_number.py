"""SAP material numbers: one canonical form, and the 80-series test.

This module is ruling 5.1 of the task plan, and it is deliberately tiny and
dependency-free so it can be unit-tested without a database, an HTTP client or
a SAP connection.

The problem it solves
---------------------
SAP stores MATNR internally as an 18-character zero-padded string, and every
channel hands it over differently:

    live CPI OData (MaterialSet)   000000008000000000   18 chars
    live CPI, 62 of 2,035 rows     unpadded             3-14 chars
    July Excel extract (raw_mara)  8000005632           10 chars
    July extract (raw_ekpo/mard)   some are 8 chars     8-10 chars

So ``WHERE material LIKE '80%'`` is correct against the extract and returns
ZERO rows against live CPI. Nothing errors; the register is simply empty.

The rule: never compare two raw material numbers. Normalise both, then compare.

Why the test is a predicate and not a lookup
--------------------------------------------
The obvious implementation is ``JOIN raw_mara WHERE material LIKE '80%'``. It is
wrong here. The MARA extract holds 8000005632..8000006059 while purchasing
references repair materials from 8000000007 up: **321 of the 371 materials on
repair PO lines have no MARA row at all**. Gating on the material master throws
away 87% of the register before it is built.

A rule on the number holds whatever source it is reading and however incomplete
that source is. A lookup silently shrinks to whatever the extract happened to
contain. See the task plan, sections 3 and 7.3.
"""

from __future__ import annotations

from app.initiatives.i8.config import I8Settings, get_i8_settings


def normalise(matnr: str | None) -> str | None:
    """SAP material number -> one canonical form, or None.

    A no-op on the July extract, which carries no leading zeros, and
    load-bearing the moment data arrives from CPI. It runs at the boundary
    either way, so the same code works before and after cutover -- which is the
    whole point of building it now, while it costs nothing.

    None-safe on purpose: ``raw_mseg`` has 34,975 rows with no material number
    at all, and a normalisation that throws on those turns a data gap into an
    outage.

    >>> normalise("000000008000000000")
    '8000000000'
    >>> normalise("8000005632")
    '8000005632'
    >>> normalise("   ")
    >>> normalise(None)
    """
    if matnr is None:
        return None
    stripped = matnr.strip().lstrip("0")
    return stripped or None


def is_eighty_series(matnr: str | None, cfg: I8Settings | None = None) -> bool:
    """True when this material number is a repairable 80-series part.

    Three guards, each earning its place:

    * ``len == material_number_length`` rejects short numbers that happen to
      begin '80' -- the bare string '80' is not a material.
    * ``isdigit()`` rejects non-numeric material codes.
    * ``startswith``, **not** ``contains``: 5000000800 contains '800' and is an
      ordinary consumable. A ``contains`` test matches it, silently, and adds a
      non-repairable material to the universe with nothing to indicate it.

    >>> is_eighty_series("8000005632")
    True
    >>> is_eighty_series("000000008000000000")
    True
    >>> is_eighty_series("5000000800")
    False
    """
    cfg = cfg or get_i8_settings()
    normalised = normalise(matnr)
    if normalised is None:
        return False
    return (
        len(normalised) == cfg.material_number_length
        and normalised.isdigit()
        and any(normalised.startswith(prefix) for prefix in cfg.series_prefix_list)
    )


def same_material(left: str | None, right: str | None) -> bool:
    """Whether two material numbers refer to the same material.

    The comparison every join needs. Two NULLs are not a match -- an absent
    material number is not evidence that two rows are about the same part.
    """
    normalised_left = normalise(left)
    return normalised_left is not None and normalised_left == normalise(right)


def series_like_patterns(cfg: I8Settings | None = None) -> list[str]:
    """LIKE patterns for a database PREFILTER. Not the test itself.

    Reading every MARD, MARC, EKPO and ZMM065 row into Python to apply
    :func:`is_eighty_series` would move roughly half a million rows per request
    to answer a question about a few thousand. So the database narrows first and
    the predicate above decides.

    The contract that makes this safe: **the prefilter is deliberately looser
    than the predicate.** It carries the prefix only -- no length guard, no
    digit guard -- so it can never exclude a row the real test would have
    accepted. It can only let too much through, which the predicate then
    rejects. ``tests/test_i8_material_number.py`` asserts that relationship
    against every material in the database.

    The patterns are generated from configuration, so there is still no literal
    '80' anywhere in a query.
    """
    cfg = cfg or get_i8_settings()
    return [f"{prefix}%" for prefix in cfg.series_prefix_list]
