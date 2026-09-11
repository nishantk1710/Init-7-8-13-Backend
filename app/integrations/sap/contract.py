"""What SAP exposes, read from the discovery snapshot.

This is what lets a caller say ``MaterialPlantSet`` instead of building
``sap/opu/odata/sap/ZVZI_KPI02_SHARED_SRV/MaterialPlantSet``: the mapping from
entity set to owning service, its key fields and its property types all come
from ``data-generator/discovery/``, captured from live ``$metadata``.

Reading the CSVs at runtime rather than generating a Python module from them is
deliberate. The frontend generates ``generated-contract.ts`` and has had to
regenerate it repeatedly as SAP drifted -- ``Edm.Decimal`` -> ``Edm.String`` on
``Netpr``/``Netwr``, and ``PurchaseRequisitionSet``'s key gaining ``Bnfpo``. A
generated file is a second copy that can silently fall behind the snapshot it
came from. Here there is one copy, and ``contract_tests`` compares it against
live SAP.

The snapshot lives in this repository, so unlike the frontend there is no
path-resolution problem: it is simply a directory next to the application.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.integrations.sap.errors import ContractError

# backend/app/integrations/sap/contract.py -> backend/data-generator/discovery
_DISCOVERY = Path(__file__).resolve().parents[3] / "data-generator" / "discovery"


@dataclass(frozen=True)
class Property:
    name: str
    type: str
    nullable: bool
    is_key: bool


@dataclass(frozen=True)
class EntitySet:
    name: str
    service: str
    keys: tuple[str, ...]
    properties: tuple[Property, ...]

    def find(self, name: str) -> Property | None:
        """One property by name, or None.

        Named ``find`` rather than ``property``: a method called ``property``
        shadows the builtin decorator for every attribute defined after it,
        which is a genuinely confusing failure to debug.
        """
        return next((p for p in self.properties if p.name == name), None)

    @property
    def api_path(self) -> str:
        """The OData path CPI's APIPath parameter expects."""
        return f"sap/opu/odata/sap/{self.service}/{self.name}"


def discovery_dir() -> Path:
    return _DISCOVERY


def _read_csv(name: str) -> list[dict[str, str]]:
    path = _DISCOVERY / name
    if not path.exists():
        raise ContractError(
            f"Discovery snapshot missing: {path}. It is produced by "
            "data-generator/cpi_discovery.py and committed to this repository."
        )
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


@lru_cache
def contract() -> dict[str, EntitySet]:
    """Every exposed entity set, keyed by name."""
    properties: dict[str, list[Property]] = {}
    for row in _read_csv("properties.csv"):
        properties.setdefault(row["entity_set"], []).append(
            Property(
                name=row["property"],
                type=row["type"],
                nullable=row.get("nullable", "").strip().lower() == "true",
                # The snapshot marks a key column with "K", not a boolean word.
                is_key=row.get("is_key", "").strip().upper() == "K",
            )
        )

    sets: dict[str, EntitySet] = {}
    for row in _read_csv("entity_sets.csv"):
        name = row["entity_set"]
        props = tuple(properties.get(name, ()))
        # Key ORDER comes from properties.csv, not from entity_sets.csv.
        #
        # Both list the same key fields, but entity_sets.csv sorts them
        # alphabetically while properties.csv preserves the order SAP declared.
        # That matters: an OData key predicate is positional, so
        # StorageLocationStockSet's real key is (Matnr, Werks, Lgort), not the
        # alphabetical (Lgort, Matnr, Werks). Nine of the 21 sets differ.
        marked = tuple(p.name for p in props if p.is_key)
        declared = tuple(k.strip() for k in (row.get("keys") or "").split(";") if k.strip())
        if marked and declared and set(marked) != set(declared):
            raise ContractError(
                f"{name}: discovery snapshot disagrees on the key. "
                f"entity_sets.csv says {declared}, properties.csv says {marked}. "
                "Re-run cpi_discovery.py; do not guess which is right."
            )
        keys = marked or declared
        sets[name] = EntitySet(name=name, service=row["service"], keys=keys, properties=props)
    return sets


def entity_set(name: str) -> EntitySet:
    """One entity set, or a ContractError naming the alternatives."""
    found = contract().get(name)
    if found is None:
        known = ", ".join(sorted(contract()))
        raise ContractError(f"Unknown entity set {name!r}. Exposed sets: {known}")
    return found


@lru_cache
def counts() -> dict[str, str]:
    """Entity set -> last observed ``$count``, as text.

    Text, not int: the snapshot records ``HTTP 500`` for the sets whose ``$count``
    fails, and that is information worth keeping rather than coercing away.
    """
    return {row["entity_set"]: row["count"] for row in _read_csv("counts.csv")}


def reset_cache() -> None:
    """Forget the parsed snapshot. For tests that write a different one."""
    contract.cache_clear()
    counts.cache_clear()
