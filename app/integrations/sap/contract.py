"""What SAP exposes, read from the discovery snapshot.

This is what lets a caller say ``MaterialPlantSet`` instead of building
``sap/opu/odata/sap/ZMM_KPI02_ADD_SRV/MaterialPlantSet``: the mapping from
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

ONE SNAPSHOT PER SAP SYSTEM

DEV and QA are different systems behind different iFlow paths, and nothing
promises they expose the same keys, types or filter behaviour -- QA's
$metadata answered HTTP 500 the last time it was swept. So each snapshot
records the ``CPI_PATH`` it was captured from (``snapshot.json``, written by
cpi_discovery.py), and the contract used is the one captured from the path
this process is configured for. Change ``CPI_PATH`` and the contract follows.
No snapshot for that path is an error naming the command that makes one --
never a silent fall back to DEV's, which would page, decode and gate deltas
against a system that is not the one being called.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.core.config import BACKEND_ROOT, get_settings, normalise_cpi_path
from app.integrations.sap.errors import ContractError

# backend/data-generator: every discovery*/ folder under it is a snapshot.
SNAPSHOT_ROOT = BACKEND_ROOT / "data-generator"

# What each snapshot says about itself -- above all, the CPI_PATH it was
# captured from. Written by cpi_discovery.py.
SNAPSHOT_FILE = "snapshot.json"


@dataclass(frozen=True)
class Property:
    name: str
    type: str
    nullable: bool
    is_key: bool

    # --- Detail from $metadata, absent from the CSVs -------------------------
    # All optional: the CSVs remain the source for service ownership and keys,
    # and these enrich a property when the XML is available. A missing value is
    # "not declared", never "zero".

    max_length: int | None = None
    """Declared maximum length. 137 of 229 properties have one."""

    precision: int | None = None
    """Declared precision, on the 10 numeric properties that carry it."""

    label: str | None = None
    """SAP's business label, e.g. Dismm -> "MRP Type".

    Worth more than it looks: these are the same words the July extract uses as
    its column headers, so they bridge the extract's vocabulary to SAP field
    names -- see README, "Seeding".
    """

    filterable: bool | None = None
    sortable: bool | None = None
    creatable: bool | None = None
    updatable: bool | None = None
    """SAP's declared capability flags.

    RECORDED, NOT TRUSTED. Every one of the 229 properties declares false for
    all four, while 124 are measured as filterable and several sort fine. They
    are an untouched SEGW default carrying no information about behaviour.
    `filter_support.csv` and `operator_support.csv` -- which probe the live
    service -- are the source of truth. Captured because W2.5 asks for the
    annotations and because their uniformity is itself worth asserting.
    """


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


def snapshot_info(folder: Path) -> dict:
    """A snapshot folder's ``snapshot.json``, or {} if it has none."""
    try:
        return json.loads((folder / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def snapshots() -> dict[Path, str]:
    """Every snapshot folder that says which CPI_PATH it came from."""
    found: dict[Path, str] = {}
    for folder in sorted(SNAPSHOT_ROOT.glob("discovery*")):
        recorded = snapshot_info(folder).get("cpi_path")
        if folder.is_dir() and recorded:
            found[folder] = normalise_cpi_path(recorded)
    return found


@lru_cache
def discovery_dir() -> Path:
    """The snapshot for the SAP system this process talks to.

    ``SAP_DISCOVERY_DIR`` when it is set; otherwise the one folder whose
    ``snapshot.json`` records the configured ``CPI_PATH``.
    """
    settings = get_settings()
    if settings.sap_discovery_dir:
        forced = Path(settings.sap_discovery_dir)
        return forced if forced.is_absolute() else BACKEND_ROOT / forced

    wanted = settings.cpi_path
    matches = [folder for folder, path in snapshots().items() if path == wanted]
    if len(matches) == 1:
        return matches[0]
    known = ", ".join(f"{folder.name} <- {path}" for folder, path in snapshots().items()) or "none"
    if matches:
        raise ContractError(
            f"{len(matches)} discovery snapshots claim CPI_PATH={wanted} "
            f"({', '.join(m.name for m in matches)}). Keep one, or set SAP_DISCOVERY_DIR."
        )
    raise ContractError(
        f"No discovery snapshot was captured from CPI_PATH={wanted} (captured: {known}). "
        "This process would otherwise decode, page and gate deltas by another "
        "system's contract. Capture one from this system, then measure its deltas:\n"
        f"  CPI_PATH={wanted} python data-generator/cpi_discovery.py --skip-probes "
        "--skip-profiles --env-file .env --out data-generator/discovery_<system>\n"
        "  python -m app.ingest.delta_probe\n"
        "or point SAP_DISCOVERY_DIR at a folder on purpose."
    )


def _read_csv(name: str) -> list[dict[str, str]]:
    path = discovery_dir() / name
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
        props = _enriched(name, props)
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


def _enriched(set_name: str, props: tuple[Property, ...]) -> tuple[Property, ...]:
    """Add lengths, labels and flags from $metadata where the XML supplies them.

    Deliberately additive and forgiving. The CSVs stay authoritative for which
    service owns a set and what its key is; this only fills in detail they do
    not carry. If the XML is missing or a property is absent from it, the
    property is returned unchanged rather than dropped -- a partial snapshot
    must degrade, not delete.
    """
    detail = _metadata_detail().get(set_name)
    if not detail:
        return props
    enriched = []
    for prop in props:
        extra = detail.get(prop.name)
        if not extra:
            enriched.append(prop)
            continue
        enriched.append(
            Property(
                name=prop.name,
                type=prop.type,
                nullable=prop.nullable,
                is_key=prop.is_key,
                max_length=extra.get("max_length"),
                precision=extra.get("precision"),
                label=extra.get("label"),
                filterable=extra.get("filterable"),
                sortable=extra.get("sortable"),
                creatable=extra.get("creatable"),
                updatable=extra.get("updatable"),
            )
        )
    return tuple(enriched)


@lru_cache
def _metadata_detail() -> dict[str, dict[str, dict]]:
    """Per-set, per-property detail scraped from every committed metadata_*.xml.

    Imported here rather than at module scope: edmx.py imports Property and
    EntitySet from this module, so a top-level import would be circular.
    """
    import xml.etree.ElementTree as ET

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def flag(value: str | None) -> bool | None:
        return None if value is None else value.strip().lower() == "true"

    def number(value: str | None) -> int | None:
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

    detail: dict[str, dict[str, dict]] = {}
    for path in sorted(discovery_dir().glob("metadata_*.xml")):
        if path.stat().st_size == 0:
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError:
            # A corrupt capture must not take the whole contract down; the
            # snapshot-consistency test reports it properly.
            continue

        types: dict[str, dict[str, dict]] = {}
        for element in root.iter():
            if local(element.tag) != "EntityType":
                continue
            props: dict[str, dict] = {}
            for child in element:
                if local(child.tag) != "Property" or not child.get("Name"):
                    continue
                attrs = {local(k): v for k, v in child.attrib.items()}
                props[child.get("Name", "")] = {
                    "max_length": number(attrs.get("MaxLength")),
                    "precision": number(attrs.get("Precision")),
                    "label": attrs.get("label"),
                    "filterable": flag(attrs.get("filterable")),
                    "sortable": flag(attrs.get("sortable")),
                    "creatable": flag(attrs.get("creatable")),
                    "updatable": flag(attrs.get("updatable")),
                }
            types[element.get("Name", "")] = props

        for element in root.iter():
            if local(element.tag) == "EntitySet" and element.get("Name"):
                type_name = element.get("EntityType", "").rsplit(".", 1)[-1]
                detail[element.get("Name", "")] = types.get(type_name, {})
    return detail


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
    discovery_dir.cache_clear()
    contract.cache_clear()
    counts.cache_clear()
    _metadata_detail.cache_clear()
