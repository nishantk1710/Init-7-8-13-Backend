"""Refusing filters SAP is known to ignore.

This is the failure mode that has no error to catch.

``filter_support.csv`` probed all 208 filterable properties by sending an
impossible filter and counting what came back. If an impossible filter returns
zero rows, SAP applied it. If it returns the whole set, SAP threw it away and
answered HTTP 200 anyway. The result:

    HONOURED            124    the filter works
    IGNORED              62    SILENTLY dropped -- you get everything, status 200
    REJECTED_HTTP_500    11    SAP errors out
    NOT_TESTED           10    the probe could not decide
    PARTIAL_OR_ODD        1

Nearly a third of filterable properties either lie or fail. That is why Anish
told the I08 work to apply the ``Pstyp`` filter client-side rather than in the
query.

So a filter on an IGNORED property must never be sent as if it worked. There are
only two honest options, and both are offered here:

* refuse the call (default), or
* send it unfiltered and re-apply the predicate client-side, paying the cost of
  reading the whole set.

Silently sending it and trusting the answer is the one thing that must not
happen -- it produces confident, wrong numbers.
"""

from __future__ import annotations

import csv
import re
from functools import lru_cache

from app.integrations.sap.contract import discovery_dir
from app.integrations.sap.errors import ContractError, UnsupportedFilterError

HONOURED = "HONOURED"
IGNORED = "IGNORED"
REJECTED = "REJECTED_HTTP_500"
NOT_TESTED = "NOT_TESTED"

# Property names in an OData $filter: bare identifiers, and inside functions
# like substringof('x',Matnr) or startswith(Matnr,'8').
_IDENTIFIER = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\b")

# Words that appear in a filter but are never property names.
_KEYWORDS = frozenset(
    {
        "and", "or", "not", "eq", "ne", "gt", "ge", "lt", "le",
        "true", "false", "null",
        "startswith", "endswith", "substringof", "datetime", "guid",
    }
)


@lru_cache
def filter_support() -> dict[tuple[str, str], str]:
    """(entity set, property) -> verdict, from the discovery probe."""
    path = discovery_dir() / "filter_support.csv"
    if not path.exists():
        # Not fatal. Without the probe we cannot warn, and refusing every filter
        # would be worse than allowing them -- but say so loudly at the call
        # site rather than pretending we checked.
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {
            (row["entity_set"], row["property"]): row["verdict"]
            for row in csv.DictReader(handle)
        }


def verdict_for(entity_set: str, property_name: str) -> str | None:
    """What the probe said about filtering this property, or None if untested."""
    return filter_support().get((entity_set, property_name))


def properties_in(filter_expression: str) -> list[str]:
    """Property names referenced by an OData filter expression.

    Deliberately crude -- an identifier scan, not a parser. It over-reports
    rather than under-reports, and over-reporting only costs a spurious warning
    while under-reporting costs silently wrong data.
    """
    # Drop quoted literals first, so a value like 'Matnr' is not read as a field.
    without_literals = re.sub(r"'[^']*'", "''", filter_expression)
    names = []
    for match in _IDENTIFIER.finditer(without_literals):
        name = match.group(1)
        if name.lower() in _KEYWORDS:
            continue
        if name not in names:
            names.append(name)
    return names


def unsupported_properties(entity_set: str, filter_expression: str) -> dict[str, str]:
    """Properties in this filter that SAP will ignore or reject, with the verdict."""
    problems: dict[str, str] = {}
    for name in properties_in(filter_expression):
        verdict = verdict_for(entity_set, name)
        if verdict in (IGNORED, REJECTED):
            problems[name] = verdict
    return problems


def check_filter(entity_set: str, filter_expression: str | None) -> None:
    """Raise if this filter cannot be trusted. Called before the request.

    Raises ``UnsupportedFilterError`` -- pointedly not one of the HTTP-derived
    errors, because no HTTP call has happened and none would tell us anything.
    """
    if not filter_expression:
        return
    problems = unsupported_properties(entity_set, filter_expression)
    if not problems:
        return

    ignored = [n for n, v in problems.items() if v == IGNORED]
    rejected = [n for n, v in problems.items() if v == REJECTED]

    detail = []
    if ignored:
        detail.append(
            f"SAP SILENTLY IGNORES a filter on {', '.join(ignored)} -- it returns "
            "HTTP 200 with the whole set, so the result would look correct and "
            "be wrong"
        )
    if rejected:
        detail.append(f"SAP returns HTTP 500 for a filter on {', '.join(rejected)}")

    raise UnsupportedFilterError(
        f"{entity_set}: {'; '.join(detail)}. "
        "Read the set without this predicate and filter client-side, or pass "
        "allow_unsupported_filter=True if you have re-verified it against live "
        "SAP. Evidence: data-generator/discovery/filter_support.csv."
    )


def assert_probe_available() -> None:
    """Raise if the filter probe is missing, for callers that require the guard."""
    if not filter_support():
        raise ContractError(
            "filter_support.csv is missing from the discovery snapshot, so "
            "filters cannot be validated. Re-run data-generator/cpi_discovery.py."
        )


def reset_cache() -> None:
    filter_support.cache_clear()
