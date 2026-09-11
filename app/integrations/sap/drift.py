"""Comparing two pictures of SAP, and saying precisely how they differ.

Drift is not hypothetical here. Between two sweeps a day apart, live SAP changed
a property's type and an entity set's key. Neither raises an error at the time;
both change what the data means.

So the contract is not trusted because it was once correct. It is compared --
against the committed ``$metadata`` offline, and against live ``$metadata`` when
a machine can reach CPI.

Differences are returned, not raised. A caller decides whether a change is
tolerable: a new property is usually fine, a changed key usually is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.integrations.sap.contract import EntitySet


class Severity(str, Enum):
    """How much a difference matters.

    ``BREAKING`` means code that worked will now be wrong rather than merely
    incomplete -- a changed key or type silently changes results.
    """

    BREAKING = "breaking"
    ADDITIVE = "additive"
    INFO = "info"


@dataclass(frozen=True)
class Difference:
    entity_set: str
    kind: str
    detail: str
    severity: Severity

    def __str__(self) -> str:
        return f"[{self.severity.value}] {self.entity_set}: {self.detail}"


def compare_contracts(
    expected: dict[str, EntitySet],
    actual: dict[str, EntitySet],
    *,
    only: set[str] | None = None,
) -> list[Difference]:
    """Differences between two contracts, ``expected`` being ours.

    ``only`` restricts the comparison to named sets, for when one service is
    reachable and another is not -- comparing against a service that answered
    nothing would report every one of its sets as missing.
    """
    differences: list[Difference] = []

    expected_names = set(expected) & only if only else set(expected)
    actual_names = set(actual) & only if only else set(actual)

    for name in sorted(expected_names - actual_names):
        differences.append(
            Difference(
                entity_set=name,
                kind="set_removed",
                detail="in the contract but not in $metadata -- the set was withdrawn",
                severity=Severity.BREAKING,
            )
        )

    for name in sorted(actual_names - expected_names):
        differences.append(
            Difference(
                entity_set=name,
                kind="set_added",
                detail="in $metadata but not in the contract -- re-run cpi_discovery.py",
                severity=Severity.ADDITIVE,
            )
        )

    for name in sorted(expected_names & actual_names):
        differences.extend(_compare_one(expected[name], actual[name]))

    return differences


def _compare_one(expected: EntitySet, actual: EntitySet) -> list[Difference]:
    differences: list[Difference] = []

    if expected.service != actual.service:
        differences.append(
            Difference(
                entity_set=expected.name,
                kind="service_changed",
                detail=f"service {expected.service} -> {actual.service}",
                severity=Severity.BREAKING,
            )
        )

    # Key order is part of the key: an OData key predicate is positional.
    if tuple(expected.keys) != tuple(actual.keys):
        differences.append(
            Difference(
                entity_set=expected.name,
                kind="key_changed",
                detail=f"key {list(expected.keys)} -> {list(actual.keys)}",
                severity=Severity.BREAKING,
            )
        )

    expected_properties = {p.name: p for p in expected.properties}
    actual_properties = {p.name: p for p in actual.properties}

    for property_name in sorted(set(expected_properties) - set(actual_properties)):
        differences.append(
            Difference(
                entity_set=expected.name,
                kind="property_removed",
                detail=f"property {property_name} is gone from $metadata",
                severity=Severity.BREAKING,
            )
        )

    for property_name in sorted(set(actual_properties) - set(expected_properties)):
        differences.append(
            Difference(
                entity_set=expected.name,
                kind="property_added",
                detail=(
                    f"property {property_name} "
                    f"({actual_properties[property_name].type}) is new in $metadata"
                ),
                severity=Severity.ADDITIVE,
            )
        )

    for property_name in sorted(set(expected_properties) & set(actual_properties)):
        before, after = expected_properties[property_name], actual_properties[property_name]
        if before.type != after.type:
            differences.append(
                Difference(
                    entity_set=expected.name,
                    kind="type_changed",
                    detail=(
                        f"{property_name}: {before.type} -> {after.type}. "
                        "Decode by the DECLARED type; never infer it from the value."
                    ),
                    severity=Severity.BREAKING,
                )
            )
        if before.nullable != after.nullable:
            differences.append(
                Difference(
                    entity_set=expected.name,
                    kind="nullability_changed",
                    detail=f"{property_name}: nullable {before.nullable} -> {after.nullable}",
                    severity=Severity.INFO,
                )
            )

    return differences


def breaking(differences: list[Difference]) -> list[Difference]:
    return [d for d in differences if d.severity is Severity.BREAKING]


def describe(differences: list[Difference]) -> str:
    """A readable report. Breaking differences first, since those are the ones to act on."""
    if not differences:
        return "No differences."
    ordered = sorted(
        differences,
        key=lambda d: (
            {Severity.BREAKING: 0, Severity.ADDITIVE: 1, Severity.INFO: 2}[d.severity],
            d.entity_set,
        ),
    )
    return "\n".join(str(d) for d in ordered)
