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
            "/api/i8/snapshot",
            # W5.3
            "/api/i8/attestations",
            "/api/i8/declarations",
            "/api/i8/exceptions",
            # A paged queue opens a linked item by id, not from its rows.
            "/api/i8/exceptions/{exception_id}",
        } <= paths

    def test_vendor_turnaround_is_gone(self, openapi) -> None:
        """Removed 19-Oct-2026: it was dispatch to goods receipt over closed
        repairs, and the register serves neither receipts nor closed lines."""
        assert "/api/i8/vendors/turnaround" not in openapi["paths"]

    # --- The read-only guarantee, as it stands after FR-11 ----------------
    #
    # This test once read "the module is read-only: anything other than GET is
    # a mistake, not a feature." W5.3 made that false and the test was CHANGED
    # rather than deleted, because deleting it would have quietly dropped the
    # guarantee instead of restating it. FR-11 loosens it one more step, and
    # the same rule applies: restate, do not delete.
    #
    # The promise is now in three parts, one test each:
    #
    #   1. the write paths are a closed, named set -- nothing else writes
    #   2. nothing is ever updated or deleted: no PUT, PATCH or DELETE
    #   3. nothing in I08 writes to SAP -- unchanged, and the one that matters
    #
    # Point 3 is the real guarantee. The platform reads SAP and records its own
    # findings beside it; it has never had, and must never acquire, a write path
    # into SAP. That is what makes "we cannot enforce this control, only observe
    # it" an honest statement rather than an excuse.

    def test_the_module_writes_only_what_it_owns(self, openapi) -> None:
        """A closed set of write paths, each against a table I08 owns.

        The attestation (W5.3) records a person's judgement. The exception
        event (FR-11) records who is working a finding and what they said about
        it -- it cannot change the finding itself, which is recomputed from SAP
        data on every read and has no stored form to edit.
        """
        i8_paths = {p: v for p, v in openapi["paths"].items() if p.startswith("/api/i8")}
        assert i8_paths, "no /api/i8 paths in the spec -- is the router mounted?"

        writes = {
            (path, verb)
            for path, operations in i8_paths.items()
            for verb in operations
            if verb != "get"
        }
        assert writes == {
            ("/api/i8/attestations", "post"),
            ("/api/i8/exceptions/{exception_id}/events", "post"),
        }, f"I08's write paths are a closed set. Found: {sorted(writes)}"

    def test_nothing_updates_or_deletes(self, openapi) -> None:
        """Every table I08 owns is append-only, and the API says so.

        PUT, PATCH and DELETE must not exist anywhere in the module -- not on
        the attestation, and not on the exception queue either. An amendment to
        an attestation is a new row pointing at the one it supersedes; an
        exception's status is not a column but the last history entry that set
        one. Neither has anything to edit.

        This was briefly written the other way, allowing PATCH on the queue's
        workflow state. ``tests/test_write_paths.py`` holds the whole
        application to append-only and caught it, which is what that file is
        for -- the I08-scoped copy here is kept honest so the two cannot drift.
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
            "declarationStatus",
            "daysOpen",
            "agingBucket",
            "raisedAt",
            "poIssuedAt",
            "sentToVendorAt",
            "expectedReturn",
            "daysRemainingInRepair",
            # The Declaration Queue and Justifications screens, folded into
            # the register on 08-Oct-2026.
            "declaredBy",
            "declaredAt",
            "condition",
            "nextAction",
            "requester",
            "justification",
        }
        assert expected <= wire, f"missing: {sorted(expected - wire)}"

    def test_a_row_carries_nothing_about_receipts_or_closure(self) -> None:
        """Since 19-Oct-2026 every row is open in SAP and goods receipts are not
        read, so the fields that described a receipt or a closed line are gone
        rather than served empty."""
        from app.api.i8.schemas import RepairChain

        wire = {field.alias or name for name, field in RepairChain.model_fields.items()}
        assert not wire & {
            "receiptStatus",
            "receivedAt",
            "receivedQty",
            "reversals",
            "deliveryCompleted",
            "poBlocked",
            "daysAtVendor",
        }

    def test_status_values_are_the_frontend_unions(self) -> None:
        from app.initiatives.i8 import aging
        from app.initiatives.i8 import register as reg
        from app.initiatives.i8 import requisitions

        assert {reg.PO_ISSUED, reg.AT_VENDOR, requisitions.PR_RAISED} <= {
            "PR Raised",
            "PO Issued",
            "At Vendor",
            "Received",
            "Closed",
        }
        # NOT_ORDERED (27-Oct-2026) is a PR row's; PO rows never carry it.
        assert {*aging.OVERDUE_STATES, requisitions.NOT_ORDERED} <= {
            "ON_TIME",
            "OVERDUE",
            "NO_DUE_DATE",
            "RECEIVED",
            "NOT_ORDERED",
        }
        assert requisitions.NOT_ORDERED not in aging.OVERDUE_STATES

    def test_repair_pr_fields_are_defaulted_for_an_older_reader(self) -> None:
        """Added 27-Oct-2026 for the open repair PRs with no PO yet. Every one
        has a default, so a PO row reads as it did and a frontend that predates
        them is not broken by their absence or presence."""
        from app.api.i8.schemas import RegisterMeta, RegisterViewCounts, RepairChain

        chain = {field.alias or name: field for name, field in RepairChain.model_fields.items()}
        assert chain["lineKind"].default == "PO"
        assert chain["requesterName"].default is None

        meta = {field.alias or name: field for name, field in RegisterMeta.model_fields.items()}
        for name in (
            "openRepairRequests",
            "requisitionsExcludedDeleted",
            "requisitionsExcludedLivePo",
            "requisitionsExcludedReservationClosed",
        ):
            assert meta[name].default == 0, name

        counts = {
            field.alias or name: field for name, field in RegisterViewCounts.model_fields.items()
        }
        assert counts["notOrdered"].default == 0

    def test_the_repair_pr_switch_is_off_by_default(self) -> None:
        from app.initiatives.i8.config import I8Settings

        assert I8Settings.model_fields["repair_requests_enabled"].default is False

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
    def test_serves_the_open_register_and_counts_the_rest(self) -> None:
        """The 1,225 repair lines, split the way SAP reads them: open, or left
        out as deleted, delivery completed or blocked -- each line once."""
        body = client.get(f"{REGISTER}?pageSize=1").json()
        meta = body["meta"]
        # The PO lines alone: the open repair PRs are counted apart, and listed
        # only when I8_REPAIR_REQUESTS_ENABLED is on (off by default).
        served_requests = body["viewCounts"].get("notOrdered", 0)
        assert body["total"] - served_requests == meta["totalLines"] == meta["openLines"]
        assert meta["linesOnEightySeries"] == meta["totalLines"]
        assert meta["excludedDeletedLines"] == 44
        assert (
            meta["totalLines"]
            + meta["excludedDeletedLines"]
            + meta["excludedDeliveryCompletedLines"]
            + meta["excludedBlockedLines"]
        ) == 1225

    def test_rows_carry_criticality_and_no_blocked_flag(self) -> None:
        """Blocked lines are left out since 19-Oct-2026, so no row needs a flag."""
        rows = client.get(f"{REGISTER}?pageSize=500").json()["items"]
        assert all("criticality" in r and "poBlocked" not in r for r in rows)

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
        gamsberg = client.get(f"{REGISTER}?plant=1500&pageSize=50").json()
        assert 0 < overdue["total"] <= total
        assert gamsberg["total"] <= total
        assert all(i["plant"]["plantId"] == "1500" for i in gamsberg["items"])
        assert overdue["items"][0]["overdueStatus"] == "OVERDUE"

    def test_no_due_date_lines_are_reachable_not_hidden(self) -> None:
        meta = client.get(f"{REGISTER}?pageSize=1").json()["meta"]
        undated = client.get(f"{REGISTER}?overdueStatus=NO_DUE_DATE&pageSize=1").json()
        assert undated["total"] == meta["noDueDateLines"]

    def test_detail_returns_the_full_lifecycle_timeline(self) -> None:
        first = client.get(f"{REGISTER}?pageSize=1").json()["items"][0]
        document, _, item = first["id"].rpartition("-")
        body = client.get(f"{REGISTER}/{document}/{item}").json()
        stages = [stage["stage"] for stage in body["timeline"]]
        # No "received": every line served is open in SAP. No "removed"
        # either since 27-Oct-2026: SAP has no record of removal.
        assert stages == [
            "attested",
            "po_raised",
            "dispatched",
            "at_vendor",
        ]
        # The PO stage names the repair PO it was built from.
        po_raised = body["timeline"][1]
        assert f"Repair PO {document}/" in po_raised["evidence"]

    def test_an_unknown_line_is_404(self) -> None:
        assert client.get(f"{REGISTER}/9999999999/10").status_code == 404


@needs_views
class TestSnapshotEndpoint:
    def test_reports_the_rules_actually_in_force(self) -> None:
        body = client.get(SNAPSHOT).json()
        assert body["rules"]["repairItemCategory"] == "3"
        assert body["rules"]["repairDocType"] == "ZREP"
        assert body["rules"]["rfqDocType"] == "AN"
        assert body["rules"]["seriesPrefixes"] == "80"
        assert "ELIKZ not 'X'" in body["rules"]["openLineRule"]
        # A real boolean, not 1 or "true": the switch for serving repair PRs.
        assert isinstance(body["rules"]["repairRequestsEnabled"], bool)
        assert "RESB.BWART '543'" in body["rules"]["openRepairRequestRule"]
        assert body["repairRegister"]["totalLines"] == body["repairRegister"]["openLines"]
        # 3,605 before the two-plant scope ruling of 21-Sep-2026.
        assert body["universe"]["totalMaterials"] == 3145

    def test_the_snapshot_is_reused_not_rebuilt(self) -> None:
        """The register reads ~400,000 rows. Rebuilding per request would make
        the UI unusable -- and the source is a static July extract, so reuse is
        safe until cutover."""
        first = client.get(SNAPSHOT).json()
        second = client.get(SNAPSHOT).json()
        assert first["builtAt"] == second["builtAt"]
