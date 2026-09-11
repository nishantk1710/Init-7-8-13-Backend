"""Contract tests -- does SAP still look the way we believe it does?

Three layers, and the distinction matters:

1. **Snapshot consistency** (offline, always runs). The CSVs and the committed
   ``$metadata`` XML are two recordings of the same sweep; they must agree. This
   catches a half-updated snapshot, which is a real failure mode -- a
   discovery folder was once replaced with an older sweep and the disagreement
   was invisible until something broke.

2. **Known conditions** (offline, always runs). The snapshot still says what we
   reasoned about: 21 sets, the empty ones, the OAR value domain, the filter
   verdicts. These fail when the snapshot is regenerated and SAP has moved.

3. **Live drift** (marked ``live``, excluded by default). Live ``$metadata``
   against our contract, and the filter verdicts actually re-probed. This is the
   only layer that can tell you SAP changed *today*.

Layers 1 and 2 test that we are self-consistent. Only layer 3 tests that we are
right. Both are worth having, and conflating them would be dishonest.
"""

from __future__ import annotations

import csv

import pytest

from app.core.config import get_settings
from app.integrations.sap import known_conditions as known
from app.integrations.sap.client import SapClient
from app.integrations.sap.contract import contract, counts, discovery_dir, entity_set
from app.integrations.sap.drift import Severity, breaking, compare_contracts, describe
from app.integrations.sap.edmx import parse_metadata, parse_snapshot
from app.integrations.sap.errors import ContractError
from app.integrations.sap.filters import filter_support

live = pytest.mark.live
needs_cpi = pytest.mark.skipif(
    not get_settings().cpi_configured, reason="CPI not configured"
)


def _read_csv(name: str) -> list[dict[str, str]]:
    with (discovery_dir() / name).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


# --- 1. Snapshot consistency ----------------------------------------------


class TestSnapshotIsSelfConsistent:
    """The CSVs and the XML record the same sweep. They must not disagree."""

    def test_metadata_and_csvs_describe_the_same_sets(self) -> None:
        assert set(parse_snapshot()) == set(contract())

    def test_no_differences_between_metadata_and_the_contract(self) -> None:
        """The whole point: a half-updated snapshot must fail here, not in production."""
        differences = compare_contracts(contract(), parse_snapshot())
        assert differences == [], describe(differences)

    def test_counts_cover_every_set(self) -> None:
        assert set(counts()) == set(contract())

    def test_filter_support_only_mentions_real_properties(self) -> None:
        for (set_name, property_name) in filter_support():
            spec = contract().get(set_name)
            assert spec is not None, f"filter_support names unknown set {set_name}"
            assert spec.find(property_name), f"{set_name}.{property_name} is not a property"

    def test_value_domains_only_mention_real_properties(self) -> None:
        for row in _read_csv("value_domains.csv"):
            spec = contract().get(row["entity_set"])
            assert spec is not None, f"value_domains names unknown set {row['entity_set']}"
            assert spec.find(row["field"]), f"{row['entity_set']}.{row['field']} missing"


# --- 2. Known conditions --------------------------------------------------


class TestContractShape:
    def test_expected_number_of_sets(self) -> None:
        assert len(contract()) == known.EXPECTED_ENTITY_SET_COUNT

    def test_only_the_two_known_services(self) -> None:
        assert {s.service for s in contract().values()} == set(known.SERVICES)

    def test_every_set_has_properties_and_a_key(self) -> None:
        for name, spec in contract().items():
            assert spec.properties, f"{name} has no properties"
            assert spec.keys, f"{name} has no key, so it cannot be paged safely"

    def test_every_key_is_a_real_property(self) -> None:
        for name, spec in contract().items():
            for key in spec.keys:
                assert spec.find(key), f"{name}: key {key} is not among its properties"

    def test_key_order_is_sap_order_not_alphabetical(self) -> None:
        """An OData key predicate is positional, so order is part of the key."""
        assert entity_set("StorageLocationStockSet").keys == ("Matnr", "Werks", "Lgort")

    def test_purchase_requisition_key_still_includes_bnfpo(self) -> None:
        """SAP added Bnfpo. A failure here means the snapshot went backwards."""
        assert entity_set("PurchaseRequisitionSet").keys == known.PURCHASE_REQUISITION_KEY

    @pytest.mark.parametrize(("set_name", "property_name"), sorted(known.DRIFTED_TO_STRING))
    def test_drifted_properties_are_still_strings(
        self, set_name: str, property_name: str
    ) -> None:
        """If these go back to Edm.Decimal, decoding assumptions need rechecking."""
        prop = entity_set(set_name).find(property_name)
        assert prop is not None and prop.type == "Edm.String"

    def test_api_paths_are_built_from_the_owning_service(self) -> None:
        for name, spec in contract().items():
            assert spec.api_path == f"sap/opu/odata/sap/{spec.service}/{name}"


class TestKnownCounts:
    def test_empty_sets_are_still_empty(self) -> None:
        """Registered, responding, zero rows. The seed covers these from the extract."""
        for name in known.EMPTY_SETS:
            assert counts()[name] == "0", f"{name} now has rows -- good news, update the seed"

    def test_high_volume_sets_are_still_huge(self) -> None:
        for name in known.HIGH_VOLUME_SETS:
            value = counts()[name]
            assert value.isdigit() and int(value) > 100_000

    def test_paging_proof_set_total_is_unchanged(self) -> None:
        assert counts()[known.PAGING_PROOF_SET] == str(known.PAGING_PROOF_TOTAL)


class TestValueDomains:
    def test_dismm_holds_only_values_already_reasoned_about(self) -> None:
        """A NEW MRP type must fail here.

        Do not "fix" this by adding the value. The MRP type decides OAR scope,
        so an unexplained code silently changes which materials three
        initiatives act on. Take it to the team lead, then add it.
        """
        observed = {
            ("" if row["value"] == "(blank)" else row["value"])
            for row in _read_csv("value_domains.csv")
            if row["entity_set"] == "MaterialPlantSet" and row["field"] == "Dismm"
        }
        unexplained = observed - set(known.DISMM_VALUE_DOMAIN)
        assert not unexplained, (
            f"MRP type(s) {sorted(unexplained)} appear in the snapshot but have never "
            "been ruled on. They decide OAR scope -- get a ruling before adding them."
        )

    def test_oar_types_are_present_in_the_domain(self) -> None:
        assert known.OAR_MRP_TYPES <= set(known.DISMM_VALUE_DOMAIN)

    def test_planned_type_is_never_treated_as_oar(self) -> None:
        """VB is Min-Max managed -- the opposite of OAR. Including it inverts the rule."""
        assert known.PLANNED_MRP_TYPE not in known.OAR_MRP_TYPES

    def test_dismm_counts_sum_to_the_set_total(self) -> None:
        """A domain that does not add up means a partial scan was recorded."""
        total = sum(
            int(row["count"])
            for row in _read_csv("value_domains.csv")
            if row["entity_set"] == "MaterialPlantSet" and row["field"] == "Dismm"
        )
        assert total == known.PAGING_PROOF_TOTAL


class TestFilterVerdicts:
    def test_every_property_was_probed(self) -> None:
        assert len(filter_support()) == known.FILTER_SUPPORT_ROW_COUNT

    def test_verdict_distribution_is_unchanged(self) -> None:
        counted: dict[str, int] = {}
        for verdict in filter_support().values():
            counted[verdict] = counted.get(verdict, 0) + 1
        assert counted == known.FILTER_VERDICT_COUNTS

    def test_only_known_verdicts_appear(self) -> None:
        assert set(filter_support().values()) <= known.FILTER_VERDICTS

    @pytest.mark.parametrize(("set_name", "property_name"), sorted(known.KNOWN_IGNORED_FILTERS))
    def test_known_ignored_filters_are_still_ignored(
        self, set_name: str, property_name: str
    ) -> None:
        """If SAP starts honouring Pstyp, the client-side workaround can be dropped."""
        assert filter_support()[(set_name, property_name)] == "IGNORED"

    @pytest.mark.parametrize(("set_name", "property_name"), sorted(known.KNOWN_HONOURED_FILTERS))
    def test_known_honoured_filters_are_still_honoured(
        self, set_name: str, property_name: str
    ) -> None:
        """The OAR scope is selected with Dismm. If it stops being honoured, that breaks."""
        assert filter_support()[(set_name, property_name)] == "HONOURED"


# --- EDMX parsing ---------------------------------------------------------


class TestEdmxParsing:
    def test_parses_keys_in_declaration_order(self) -> None:
        parsed = parse_snapshot()
        assert parsed["StorageLocationStockSet"].keys == ("Matnr", "Werks", "Lgort")

    def test_marks_key_properties(self) -> None:
        spec = parse_snapshot()["MaterialPlantSet"]
        assert {p.name for p in spec.properties if p.is_key} == {"Matnr", "Werks"}

    def test_absent_nullable_attribute_defaults_to_true(self) -> None:
        """EDMX says Nullable defaults to true; assuming false would be wrong."""
        xml = """<?xml version="1.0"?>
        <edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx"
                   xmlns="http://schemas.microsoft.com/ado/2008/09/edm">
          <edmx:DataServices><Schema Namespace="X">
            <EntityType Name="T">
              <Key><PropertyRef Name="A"/></Key>
              <Property Name="A" Type="Edm.String" Nullable="false"/>
              <Property Name="B" Type="Edm.String"/>
            </EntityType>
            <EntityContainer><EntitySet Name="TSet" EntityType="X.T"/></EntityContainer>
          </Schema></edmx:DataServices>
        </edmx:Edmx>"""
        spec = parse_metadata(xml, "X")["TSet"]
        assert spec.find("A").nullable is False
        assert spec.find("B").nullable is True

    def test_malformed_xml_is_a_contract_error(self) -> None:
        with pytest.raises(ContractError, match="not valid XML"):
            parse_metadata("<edmx:Edmx>", "X")

    def test_metadata_with_no_sets_is_a_contract_error(self) -> None:
        """An inactive service answers with a document that declares nothing."""
        xml = """<?xml version="1.0"?>
        <edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx">
          <edmx:DataServices/></edmx:Edmx>"""
        with pytest.raises(ContractError, match="no entity sets"):
            parse_metadata(xml, "X")


# --- Drift comparison -----------------------------------------------------


class TestDriftDetection:
    def _sets(self):
        return {name: spec for name, spec in list(contract().items())[:2]}

    def test_identical_contracts_have_no_differences(self) -> None:
        assert compare_contracts(self._sets(), self._sets()) == []

    def test_a_removed_set_is_breaking(self) -> None:
        ours = self._sets()
        theirs = dict(list(ours.items())[:1])
        differences = compare_contracts(ours, theirs)
        assert len(breaking(differences)) == 1
        assert differences[0].kind == "set_removed"

    def test_a_new_set_is_additive_not_breaking(self) -> None:
        ours = dict(list(self._sets().items())[:1])
        differences = compare_contracts(ours, self._sets())
        assert breaking(differences) == []
        assert differences[0].severity is Severity.ADDITIVE

    def test_a_changed_key_is_breaking(self) -> None:
        """The PurchaseRequisitionSet case, generalised."""
        from dataclasses import replace

        ours = {"S": entity_set("MaterialPlantSet")}
        theirs = {"S": replace(entity_set("MaterialPlantSet"), keys=("Matnr",))}
        differences = compare_contracts(ours, theirs)
        assert [d.kind for d in breaking(differences)] == ["key_changed"]

    def test_a_changed_type_is_breaking(self) -> None:
        """The Edm.Decimal -> Edm.String case, generalised."""
        from dataclasses import replace

        original = entity_set("PurchaseOrderItemSet")
        changed = replace(
            original,
            properties=tuple(
                replace(p, type="Edm.Decimal") if p.name == "Netpr" else p
                for p in original.properties
            ),
        )
        differences = compare_contracts({"S": original}, {"S": changed})
        assert [d.kind for d in breaking(differences)] == ["type_changed"]

    def test_only_limits_the_comparison(self) -> None:
        """One unreachable service must not report every one of its sets as removed."""
        ours = self._sets()
        first = next(iter(ours))
        assert compare_contracts(ours, {first: ours[first]}, only={first}) == []

    def test_describe_puts_breaking_first(self) -> None:
        from dataclasses import replace

        original = entity_set("MaterialPlantSet")
        changed = replace(original, keys=("Matnr",))
        report = describe(compare_contracts({"S": original}, {"S": changed}))
        assert report.startswith("[breaking]")


# --- 3. Live drift --------------------------------------------------------


@live
@needs_cpi
class TestLiveDrift:
    """The only layer that proves we are right rather than merely consistent."""

    def test_live_metadata_matches_the_contract(self) -> None:
        client = SapClient()
        ours = contract()
        for service in sorted(known.SERVICES):
            live_sets = parse_metadata(client.metadata(service), service)
            mine = {n for n, s in ours.items() if s.service == service}
            differences = compare_contracts(ours, live_sets, only=mine | set(live_sets))
            assert not breaking(differences), (
                f"{service} has drifted since the snapshot:\n{describe(differences)}\n"
                "Re-run data-generator/cpi_discovery.py and review the change."
            )

    def test_counts_still_match_the_snapshot(self) -> None:
        client = SapClient()
        for name, recorded in counts().items():
            if not recorded.isdigit() or name in known.HIGH_VOLUME_SETS:
                continue  # $count is unreliable on some sets and slow on the big ones
            if name in known.UNREADABLE_SETS:
                continue  # answers HTTP 400 to everything -- see known_conditions
            live_count = client.count(name)
            assert live_count is not None, (
                f"{name}: $count stopped working. If the set is now unreadable "
                "entirely, add it to known_conditions.UNREADABLE_SETS with the date "
                "and the status code."
            )
            # Live data grows; a large swing is what matters, not a few rows.
            drift = abs(live_count - int(recorded))
            assert drift <= max(100, int(recorded) * 0.05), (
                f"{name}: $count moved {recorded} -> {live_count}"
            )

    def test_pstyp_filter_behaviour_is_what_the_snapshot_records(self) -> None:
        """Re-probe the Pstyp case, distinguishing all three possible outcomes.

        The previous version asserted `filtered == total` and, on failure, said
        "SAP now honours a filter on Pstyp". On 2026-09-11 that message was
        actively wrong: `filtered` was None because the request returned HTTP
        500, not because SAP had started filtering. A test that misdiagnoses its
        own failure is worse than no test -- someone acts on the wrong finding.

        Three outcomes, three different consequences:

          filtered == total  IGNORED   -- the recorded behaviour; filter client-side
          filtered == 0      HONOURED  -- good news; the workaround can be dropped
          filtered is None   ERROR     -- SAP now rejects it outright (current state)
        """
        client = SapClient()
        total = client.count("PurchaseOrderItemSet")
        assert total is not None, "$count on PurchaseOrderItemSet stopped working"

        filtered = client.count(
            "PurchaseOrderItemSet",
            filter="Pstyp eq 'ZZZZ'",
            allow_unsupported_filter=True,
        )

        if filtered is None:
            observed = "REJECTED_HTTP_500"
        elif filtered == total:
            observed = "IGNORED"
        elif filtered == 0:
            observed = "HONOURED"
        else:
            observed = f"PARTIAL ({filtered} of {total})"

        recorded = filter_support().get(("PurchaseOrderItemSet", "Pstyp"))
        assert observed == recorded, "\n".join(
            [
                f"Pstyp filter behaviour changed: snapshot says {recorded}, "
                f"live says {observed}.",
                "  IGNORED           -> filter client-side (the original workaround)",
                "  HONOURED          -> the workaround can be dropped",
                "  REJECTED_HTTP_500 -> the filter now throws. The client's guard",
                "                       already refuses it, so normal callers are",
                "                       safe; anything passing",
                "                       allow_unsupported_filter=True will now fail.",
                "Re-run data-generator/cpi_discovery.py to refresh filter_support.csv.",
            ]
        )

    def test_an_honoured_filter_is_still_honoured(self) -> None:
        """Dismm selects OAR scope. An impossible value must return zero."""
        client = SapClient()
        assert client.count("MaterialPlantSet", filter="Dismm eq 'ZZZZ'") == 0
