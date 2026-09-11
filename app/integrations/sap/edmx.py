"""Parsing OData v2 ``$metadata`` into the same shape as the contract.

This is what makes drift detectable. ``contract.py`` reads the committed
discovery snapshot; this reads ``$metadata`` -- either the committed XML or a
live fetch -- and produces the identical structure, so the two can be compared
field by field.

Why that matters is a matter of record rather than theory. Between two sweeps a
day apart, live SAP:

* changed ``PurchaseOrderItemSet.Netpr`` and ``.Netwr`` from ``Edm.Decimal`` to
  ``Edm.String``, and
* added ``Bnfpo`` to ``PurchaseRequisitionSet``'s key.

Both are silent from a caller's point of view until something does arithmetic on
a string or addresses a requisition by an incomplete key.

EDMX shape, for orientation::

    <edmx:Edmx><edmx:DataServices><Schema>
      <EntityType Name="MaterialPlant">
        <Key><PropertyRef Name="Matnr"/><PropertyRef Name="Werks"/></Key>
        <Property Name="Matnr" Type="Edm.String" Nullable="false"/>
      </EntityType>
      <EntityContainer>
        <EntitySet Name="MaterialPlantSet" EntityType="SRV.MaterialPlant"/>
      </EntityContainer>
    </Schema></edmx:DataServices></edmx:Edmx>

Entity *types* carry the shape; entity *sets* are what callers name. The link is
``EntitySet/@EntityType``, namespace-qualified, so the last dotted segment is the
type name.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from app.integrations.sap.contract import EntitySet, Property
from app.integrations.sap.errors import ContractError


def _local(tag: str) -> str:
    """Strip the XML namespace. EDMX uses several and they vary by SAP release."""
    return tag.rsplit("}", 1)[-1]


def _find_all(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _local(e.tag) == name]


def parse_metadata(xml: str, service: str) -> dict[str, EntitySet]:
    """Entity sets declared by one service's ``$metadata``.

    ``service`` is recorded on each set; the document itself does not reliably
    name the service, and callers always know which one they asked for.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        preview = xml[:200].replace("\n", " ")
        raise ContractError(
            f"{service}: $metadata is not valid XML ({exc}). Got: {preview!r}"
        ) from exc

    # Entity types, by type name.
    types: dict[str, tuple[tuple[str, ...], tuple[Property, ...]]] = {}
    for element in _find_all(root, "EntityType"):
        type_name = element.get("Name")
        if not type_name:
            continue

        keys: list[str] = []
        for key_element in element:
            if _local(key_element.tag) != "Key":
                continue
            for ref in key_element:
                if _local(ref.tag) == "PropertyRef" and ref.get("Name"):
                    keys.append(ref.get("Name", ""))

        properties = [
            Property(
                name=child.get("Name", ""),
                type=child.get("Type", ""),
                # EDMX defaults Nullable to true when the attribute is absent.
                nullable=(child.get("Nullable", "true").lower() == "true"),
                is_key=child.get("Name", "") in keys,
            )
            for child in element
            if _local(child.tag) == "Property" and child.get("Name")
        ]
        types[type_name] = (tuple(keys), tuple(properties))

    # Entity sets, resolved to their type.
    sets: dict[str, EntitySet] = {}
    for element in _find_all(root, "EntitySet"):
        set_name = element.get("Name")
        qualified = element.get("EntityType", "")
        if not set_name or not qualified:
            continue
        type_name = qualified.rsplit(".", 1)[-1]
        keys, properties = types.get(type_name, ((), ()))
        if not properties:
            raise ContractError(
                f"{service}: EntitySet {set_name!r} references EntityType "
                f"{type_name!r}, which the document does not define."
            )
        sets[set_name] = EntitySet(
            name=set_name, service=service, keys=keys, properties=properties
        )

    if not sets:
        raise ContractError(
            f"{service}: $metadata declared no entity sets. The service is "
            "probably not activated."
        )
    return sets


def parse_metadata_file(path: Path, service: str) -> dict[str, EntitySet]:
    """Parse a committed ``$metadata`` file."""
    if not path.exists():
        raise ContractError(f"No captured $metadata at {path}")
    return parse_metadata(path.read_text(encoding="utf-8"), service)


def parse_snapshot() -> dict[str, EntitySet]:
    """Every entity set across every committed ``metadata_*.xml``.

    The offline half of drift detection: this is what ``$metadata`` said when the
    snapshot was taken, which the CSVs are supposed to agree with.
    """
    from app.integrations.sap.contract import discovery_dir

    parsed: dict[str, EntitySet] = {}
    files = sorted(discovery_dir().glob("metadata_*.xml"))
    if not files:
        raise ContractError(
            f"No metadata_*.xml in {discovery_dir()}. Re-run cpi_discovery.py."
        )
    for path in files:
        service = path.stem.removeprefix("metadata_")
        if path.stat().st_size == 0:
            # A zero-byte capture means the service was unreachable at sweep
            # time. Recorded rather than treated as "no sets".
            continue
        parsed.update(parse_metadata_file(path, service))
    return parsed
