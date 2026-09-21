"""The /api/i8 endpoints.

Two groups, deliberately:

* Contract tests that run anywhere -- the routes exist, they are read-only, and
  the response shapes match what the frontend already defines.
* Behaviour tests that need the seeded extracts and skip without them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.i8_support import needs_views

client = TestClient(app)

UNIVERSE = "/api/i8/universe"
REGISTER = "/api/i8/register"
VENDORS = "/api/i8/vendors/turnaround"
SNAPSHOT = "/api/i8/snapshot"


# --- Contract, no data required -------------------------------------------


@pytest.fixture(scope="module")
def openapi() -> dict:
    """The served spec.

    Read from OpenAPI rather than app.routes: this FastAPI version wraps
    included routers in objects with no .path, so walking app.routes silently
    finds nothing and a "read-only" assertion over it passes vacuously.
    """
    return client.get("/openapi.json").json()


class TestRoutesAreMounted:
    def test_every_i8_route_is_registered(self, openapi) -> None:
        paths = set(openapi["paths"])
        assert {
            "/api/i8/universe",
            "/api/i8/universe/{material_id}",
            "/api/i8/register",
            "/api/i8/register/{document}/{item}",
            "/api/i8/vendors/turnaround",
            "/api/i8/snapshot",
            # W5.3
            "/api/i8/attestations",
            "/api/i8/declarations",
            "/api/i8/exceptions",
        } <= paths

    # --- The read-only guarantee, as it stands after W5.3 -----------------
    #
    # This test used to read "the module is read-only: anything other than GET
    # is a mistake, not a feature." W5.3 makes that false, so the test was
    # CHANGED rather than deleted -- deleting it would have quietly dropped the
    # guarantee instead of restating it.
    #
    # The loosening is exactly one step, and the three tests below pin each part
    # of the new, narrower promise:
    #
    #   1. exactly ONE write path exists, and it is the attestation
    #   2. it writes to one table WE own, and never updates or deletes
    #   3. nothing in I08 writes to SAP -- unchanged, and the one that matters
    #
    # Point 3 is the real guarantee. The platform reads SAP and records its own
    # findings beside it; it has never had, and must never acquire, a write path
    # into SAP. That is what makes "we cannot enforce this control, only observe
    # it" an honest statement rather than an excuse.

    def test_the_module_is_read_only_except_for_attestations(self, openapi) -> None:
        """Exactly one write path, and it is the one W5.3 added."""
        i8_paths = {p: v for p, v in openapi["paths"].items() if p.startswith("/api/i8")}
        assert i8_paths, "no /api/i8 paths in the spec -- is the router mounted?"

        writes = {
            (path, verb)
            for path, operations in i8_paths.items()
            for verb in operations
            if verb != "get"
        }
        assert writes == {("/api/i8/attestations", "post")}, (
            f"I08 has exactly one write path -- the attestation. Found: {sorted(writes)}"
        )

    def test_nothing_updates_or_deletes(self, openapi) -> None:
        """Attestations are audit records: append-only, no exceptions.

        An amendment is a new row pointing at the one it supersedes. So PUT,
        PATCH and DELETE must not exist anywhere in the module -- not even on
        the attestation route.
        """
        i8_paths = {p: v for p, v in openapi["paths"].items() if p.startswith("/api/i8")}
        for path, operations in i8_paths.items():
            forbidden = {"put", "patch", "delete"} & set(operations)
            assert not forbidden, f"{path} exposes {sorted(forbidden)}"

    def test_no_route_writes_to_sap(self) -> None:
        """The guarantee that actually matters, and the one that did not change.

        Checked structurally rather than by inspecting prose: the I08 packages
        must not import the SAP client at all. Nothing in the initiative reaches
        SAP directly -- it reads the seeded extract through views -- so an
        import of app.integrations.sap here is the first step of a write path
        into SAP and should fail before it becomes one.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[1]
        offenders = []
        for package in ("app/initiatives/i8", "app/api/i8"):
            for file in (root / package).rglob("*.py"):
                text = file.read_text(encoding="utf-8")
                # The filters module is referenced from a docstring and a test,
                # never imported at runtime; an actual import statement is what
                # this is looking for.
                for line in text.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("import ", "from ")) and (
                        "app.integrations.sap" in stripped
                    ):
                        offenders.append(f"{file.relative_to(root)}: {stripped}")
        assert offenders == [], (
            "I08 must not import the SAP client -- it reads the seeded extract. "
            f"Found: {offenders}"
        )

    def test_mounting_i8_did_not_break_the_existing_endpoints(self) -> None:
        assert client.get("/api/health").status_code == 200
        assert client.get("/").status_code == 200

    def test_openapi_documents_the_module(self, openapi) -> None:
        assert "/api/i8/register" in openapi["paths"]


class TestResponseShapeMatchesTheFrontend:
    """Field names are checked against
    Init-7-8-13-Frontend/src/features/initiative-8/types/repair.ts.

    W5.4 is rendering against this contract on the same critical path, so a
    rename here is a merge conflict there.
    """

    def test_repair_chain_carries_the_frontend_field_names(self) -> None:
        from app.api.i8.schemas import RepairChain

        wire = {
            field.alias or name
            for name, field in RepairChain.model_fields.items()
        }
        expected = {
            "id",
            "material",
            "plant",
            "stockOnHand",
            "reorderPoint",
            "qtyUnderRepair",
            "repairPr",
            "repairPo",
            "vendor",
            "repairStatus",
            "receiptStatus",
            "declarationStatus",
            "daysOpen",
            "agingBucket",
            "raisedAt",
            "poIssuedAt",
            "sentToVendorAt",
            "expectedReturn",
            "daysRemainingInRepair",
            "receivedAt",
        }
        assert expected <= wire, f"missing: {sorted(expected - wire)}"

    def test_status_values_are_the_frontend_unions(self) -> None:
        from app.initiatives.i8 import register as reg

        assert {reg.PO_ISSUED, reg.AT_VENDOR, reg.RECEIVED_STATUS, reg.CLOSED} <= {
            "PR Raised",
            "PO Issued",
            "At Vendor",
            "In Transit Return",
            "Received",
            "Closed",
        }
        assert {
            reg.NOT_YET_SHIPPED,
            reg.AWAITING_RECEIPT,
            reg.PARTIALLY_RECEIVED,
            reg.FULLY_RECEIVED,
        } <= {
            "Not Yet Shipped",
            "Awaiting Receipt",
            "Partially Received",
            "Received",
        }

    def test_declaration_status_is_the_w53_hook(self) -> None:
        from app.api.i8.schemas import RepairChain

        assert RepairChain.model_fields["declaration_status"].default == "Required"


# --- Behaviour, needs the seeded extracts ---------------------------------


@needs_views
class TestUniverseEndpoint:
    def test_returns_the_repairable_universe_with_its_sources(self) -> None:
        body = client.get(f"{UNIVERSE}?pageSize=5").json()
        # 3,802 before the two-plant scope ruling of 21-Sep-2026.
        assert body["total"] == 3242
        assert len(body["items"]) == 5
        # Every figure travels with the extract it came from.
        assert body["meta"]["bySource"]["mara"] == 362
        assert body["meta"]["totalMaterials"] == 3145

    def test_the_mara_slice_is_reproducible(self) -> None:
        """The plan's "362 materials" figure, still obtainable -- as a filter on
        one source rather than as the size of the universe."""
        body = client.get(f"{UNIVERSE}?inMaterialMaster=true&pageSize=1").json()
        assert body["total"] == 367  # 362 materials across 367 material+plant rows
        assert body["items"][0]["inMaterialMaster"] is True

    def test_filters_narrow_the_result(self) -> None:
        everything = client.get(f"{UNIVERSE}?pageSize=1").json()["total"]
        plant = client.get(f"{UNIVERSE}?plant=1300&pageSize=1").json()["total"]
        critical = client.get(f"{UNIVERSE}?criticality=CRITICAL&pageSize=1").json()
        assert 0 < plant < everything
        assert 0 < critical["total"] < everything
        assert critical["items"][0]["criticality"] == "CRITICAL"

    def test_paging_is_stable_and_does_not_overlap(self) -> None:
        first = client.get(f"{UNIVERSE}?pageSize=10&page=1").json()
        second = client.get(f"{UNIVERSE}?pageSize=10&page=2").json()
        assert first["total"] == second["total"]
        assert {i["id"] for i in first["items"]}.isdisjoint(
            {i["id"] for i in second["items"]}
        )

    def test_page_size_is_capped(self) -> None:
        body = client.get(f"{UNIVERSE}?pageSize=100000").json()
        assert body["pageSize"] == 500

    def test_null_is_served_not_a_default(self) -> None:
        body = client.get(f"{UNIVERSE}?plant=1500&pageSize=50").json()
        assert any(item["reorderPoint"] is None for item in body["items"]), (
            "MARC has no Gamsberg rows -- reorderPoint must be null, not 0"
        )

    @pytest.mark.parametrize(
        "material", ["8000005632", "000000008000005632", "00008000005632"]
    )
    def test_any_material_number_shape_resolves_to_the_same_material(
        self, material: str
    ) -> None:
        """Ruling 5.1, end to end through HTTP.

        The zero-padded form is what live CPI sends. If these ever stop
        agreeing, every lookup from the UI breaks at cutover.
        """
        body = client.get(f"{UNIVERSE}/{material}").json()
        assert body["material"]["materialId"] == "8000005632"

    def test_a_non_repairable_material_is_404_not_an_empty_page(self) -> None:
        response = client.get(f"{UNIVERSE}/5000000800")
        assert response.status_code == 404
        assert "80-series" in response.json()["detail"]


@needs_views
class TestRegisterEndpoint:
    def test_serves_the_whole_register(self) -> None:
        body = client.get(f"{REGISTER}?pageSize=1").json()
        assert body["total"] == 1225
        assert body["meta"]["linesOnEightySeries"] == 1225
        assert body["meta"]["linesWithPoHeader"] == 770

    def test_a_row_is_at_po_line_grain(self) -> None:
        item = client.get(f"{REGISTER}?pageSize=1").json()["items"][0]
        assert item["repairPo"]["documentNumber"]
        assert item["repairPo"]["line"]
        assert item["id"] == (
            f"{item['repairPo']['documentNumber']}-{item['repairPo']['line']}"
        )

    def test_every_row_carries_a_repair_requisition(self) -> None:
        """EKPO carries one on all 1,225 lines, so repairPR can be the
        non-optional reference the frontend already expects."""
        body = client.get(f"{REGISTER}?pageSize=50").json()
        for item in body["items"]:
            assert item["repairPr"]["documentNumber"]
            assert item["repairPr"]["type"] == "PR"

    def test_filters(self) -> None:
        total = client.get(f"{REGISTER}?pageSize=1").json()["total"]
        overdue = client.get(f"{REGISTER}?overdueOnly=true&pageSize=1").json()
        open_only = client.get(f"{REGISTER}?openOnly=true&pageSize=1").json()
        gamsberg = client.get(f"{REGISTER}?plant=1500&pageSize=1").json()
        assert 0 < overdue["total"] < total
        assert 0 < open_only["total"] < total
        assert 0 < gamsberg["total"] < total
        assert overdue["items"][0]["overdueStatus"] == "OVERDUE"

    def test_no_due_date_lines_are_reachable_not_hidden(self) -> None:
        meta = client.get(f"{REGISTER}?pageSize=1").json()["meta"]
        assert meta["linesWithoutDueDate"] == 63
        assert meta["noDueDateLines"] == 61

    def test_detail_returns_the_full_lifecycle_timeline(self) -> None:
        first = client.get(f"{REGISTER}?pageSize=1").json()["items"][0]
        document, _, item = first["id"].rpartition("-")
        body = client.get(f"{REGISTER}/{document}/{item}").json()
        stages = [stage["stage"] for stage in body["timeline"]]
        assert stages == [
            "removed",
            "attested",
            "po_raised",
            "dispatched",
            "at_vendor",
            "received",
        ]
        # The stages the data cannot support say so, rather than being absent.
        removed = body["timeline"][0]
        assert removed["occurredAt"] is None
        assert "no PO reference" in removed["evidence"]

    def test_an_unknown_line_is_404(self) -> None:
        assert client.get(f"{REGISTER}/9999999999/10").status_code == 404


@needs_views
class TestVendorEndpoint:
    def test_serves_vendor_analytics_with_its_caveats(self) -> None:
        body = client.get(VENDORS).json()
        assert body["total"] > 1
        assert "COMPLETED repairs only" in body["note"]

    def test_header_less_lines_are_grouped_under_unknown(self) -> None:
        items = client.get(VENDORS).json()["items"]
        unknown = next(v for v in items if v["vendor"] == "UNKNOWN")
        assert unknown["totalLines"] == 455

    def test_an_average_always_publishes_its_sample_size(self) -> None:
        for vendor in client.get(VENDORS).json()["items"]:
            if vendor["avgTurnaroundDays"] is not None:
                assert vendor["turnaroundSample"] > 0


@needs_views
class TestSnapshotEndpoint:
    def test_reports_the_rules_actually_in_force(self) -> None:
        body = client.get(SNAPSHOT).json()
        assert body["rules"]["repairItemCategory"] == "3"
        assert body["rules"]["repairDocType"] == "ZREP"
        assert body["rules"]["seriesPrefixes"] == "80"
        assert body["repairRegister"]["totalLines"] == 1225
        # 3,605 before the two-plant scope ruling of 21-Sep-2026.
        assert body["universe"]["totalMaterials"] == 3145

    def test_the_snapshot_is_reused_not_rebuilt(self) -> None:
        """The register reads ~400,000 rows. Rebuilding per request would make
        the UI unusable -- and the source is a static July extract, so reuse is
        safe until cutover."""
        first = client.get(SNAPSHOT).json()
        second = client.get(SNAPSHOT).json()
        assert first["builtAt"] == second["builtAt"]
