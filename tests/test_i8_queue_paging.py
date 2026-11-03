"""The register and the declaration queue, paged on the server.

(The exception queue paged here too until 3-Nov-2026, when it was removed with
its screen; so did POST /attestations, now that the assistant records a
declaration. Their tests went with them.)

The three screens fetched every row -- 5,063, 5,153 and 5,978 of them -- and
filtered in the browser, so each page weighed 17 to 35 MB. They now ask for one
page, which moves here what the browser used to do over the rows it held:

* the search box (``q``), and the filters that had no server side yet
  (``vendor``, ``repairStatus``, ``actionableOnly``, the register's
  ``overdueStatus``, ``declarationStatus`` and ``withExceptions``);
* the dropdown values (``facets``), which one page cannot supply;
* dropping a check the exception screen does not show (``excludeType``), which
  has to come off the counts as well as the rows;
* the register's Exceptions column, which joined the whole queue in the
  browser.

Since 19-Oct-2026 every register line is open in SAP, so the register no
longer holds lines back from its own listing (``excludeHeld`` is gone): what
SAP has closed, deleted or blocked is counted in ``meta`` and never served.

``GET /exceptions/{id}`` is new for the same reason: a link to one item can no
longer be opened by finding it among the rows on screen.

The snapshot and the attestation view are replaced with a few hand-built rows,
and the one table the exception routes read and write -- the history -- is
in-memory SQLite, so this runs without the Azure database or the extracts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i8.router import attestation_view_dependency, snapshot_dependency
from app.core.db import get_db
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.declarations import DeclarationRow
from app.initiatives.i8.register import RegisterStats, RepairLine
from app.initiatives.i8.requisitions import RepairRequest

DECLARATIONS = "/api/i8/declarations"
REGISTER = "/api/i8/register"


def repair_line(n: int, **overrides) -> RepairLine:
    """Repair line ``45000000{n}-10`` of material ``80000000{n}`` at 1300, out at
    the vendor and on time."""
    fields = dict(
        purchasing_document=f"45000000{n:02d}",
        item="10",
        material_id=f"80000000{n:02d}",
        plant="1300",
        description=f"Pump seal {n}",
        ordered_qty=Decimal(1),
        unit="EA",
        net_price=None,
        item_category="3",
        pr_number=f"100000{n:02d}",
        pr_item="10",
        requisitioner="10316",
        doc_type="ZREP",
        corroborated_by_doc_type=True,
        has_po_header=True,
        vendor="100234",
        vendor_name="Acme Rewinds",
        raised_at=date(2026, 9, 1),
        po_date=date(2026, 9, 1),
        due_date=date(2026, 11, 1),
        schedule_lines=1,
        dispatched_at=None,
        dispatched_qty=Decimal(0),
        lead_time_days=None,
        repair_status="At Vendor",
        overdue_status="ON_TIME",
        lead_time_status="NO_LEAD_TIME",
        qty_under_repair=Decimal(1),
        days_open=47,
        days_elapsed=47,
        days_over_lead_time=None,
        days_at_vendor=None,
        days_in_current_stage=47,
        days_remaining=14,
        aging_bucket="31-60",
    )
    fields.update(overrides)
    return RepairLine(**fields)


def register_stats(lines: tuple[RepairLine, ...]) -> RegisterStats:
    """The counts the register's caption reads, over ``lines`` -- every one of
    them open; the rest stay zero, as an empty register's would."""
    return RegisterStats(total_lines=len(lines), open_lines=len(lines))


def rating(material_id: str, plant: str, criticality: str | None, **overrides) -> SimpleNamespace:
    """A universe row: the material's position at one plant."""
    fields = dict(
        material_id=material_id,
        plant=plant,
        criticality=criticality,
        stock_on_hand=None,
        reorder_point=None,
        planned_delivery_days=None,
        usable_stock=None,
        damaged_stock=None,
        stock_condition_source=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def declaration(n: int, **overrides) -> DeclarationRow:
    """An outstanding declaration-queue row for repair line ``45000000{n}-10``."""
    fields = dict(
        id=f"DQ-{n}",
        material_id=f"80000000{n:02d}",
        description=f"Pump seal {n}",
        plant="1300",
        pr_number=f"100000{n:02d}",
        pr_item="10",
        requester="10316",
        related_repair_id=f"45000000{n:02d}-10",
        status="Required",
        declared_by=None,
        declared_at=None,
        condition=None,
        next_action="Declare the condition of this part.",
        created_at=date(2026, 9, 1),
        vendor="100234",
        vendor_name="Acme Rewinds",
        repair_status="At Vendor",
    )
    fields.update(overrides)
    return DeclarationRow(**fields)


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # No tables: everything these routes read comes from the view and the
    # snapshot, which are replaced below.
    return sessionmaker(bind=engine)


@pytest.fixture
def queues(factory):
    """A client over the rows a test puts in ``state``."""
    from app.main import create_app

    app = create_app()
    state = SimpleNamespace(
        declarations=(),
        lines=(),
        universe=(),
        stats=None,
        requests=(),
        # The repair PRs are served only when a test switches them on.
        cfg=get_i8_settings().model_copy(update={"repair_requests_enabled": False}),
    )

    def _db():
        with factory() as session:
            yield session

    def _view():
        return SimpleNamespace(
            declarations=state.declarations,
            # No declaration covers any line here: the PO detail route reads
            # ``covered`` for the timeline's declaration stage.
            coverage=SimpleNamespace(window_days=30, covered={}),
            # What the register reads off the view: each line's queue row by
            # its key.
            declaration_by_line={
                tuple(row.related_repair_id.rsplit("-", 1)): row for row in state.declarations
            },
        )

    def _snapshot():
        return SimpleNamespace(
            reference_date=date(2026, 10, 18),
            lines=state.lines,
            universe=state.universe,
            register_stats=state.stats or register_stats(state.lines),
            repair_requests=state.requests,
            line=lambda document, item: next(
                (line for line in state.lines if line.key == (document, item)), None
            ),
            request=lambda banfn, bnfpo: next(
                (r for r in state.requests if r.key == (banfn, bnfpo)), None
            ),
        )

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[snapshot_dependency] = _snapshot
    app.dependency_overrides[attestation_view_dependency] = _view
    app.dependency_overrides[get_i8_settings] = lambda: state.cfg
    # No `with`: entering the context runs the lifespan against the real database.
    state.client = TestClient(app)
    yield state
    app.dependency_overrides.clear()


def ids(body: dict) -> list[str]:
    return [item["id"] for item in body["items"]]


# --- The declaration queue -------------------------------------------------


class TestDeclarationSearch:
    @pytest.fixture(autouse=True)
    def rows(self, queues):
        queues.declarations = (
            declaration(1),
            declaration(2, description="Gearbox bearing"),
            declaration(3, pr_number="10077777"),
        )

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("8000000001", ["DQ-1"]),  # material number
            ("GEARBOX", ["DQ-2"]),  # description, any case
            ("4500000003-10", ["DQ-3"]),  # repair line, as the screen shows it
            ("10077777", ["DQ-3"]),  # requisition
        ],
    )
    def test_searches_the_fields_the_screen_did(self, queues, q, expected) -> None:
        body = queues.client.get(DECLARATIONS, params={"q": q}).json()

        assert ids(body) == expected
        assert body["total"] == len(expected)

    def test_a_blank_search_is_no_search(self, queues) -> None:
        body = queues.client.get(DECLARATIONS, params={"q": "   "}).json()

        assert body["total"] == 3


class TestDeclarationFilters:
    def test_vendor_matches_the_code(self, queues) -> None:
        queues.declarations = (
            declaration(1),
            declaration(2, vendor="100999", vendor_name="Other Works"),
        )

        body = queues.client.get(DECLARATIONS, params={"vendor": "100999"}).json()

        assert ids(body) == ["DQ-2"]

    def test_repair_status_matches_whatever_the_case(self, queues) -> None:
        queues.declarations = (declaration(1), declaration(2, repair_status="PO Issued"))

        body = queues.client.get(DECLARATIONS, params={"repairStatus": "po issued"}).json()

        assert ids(body) == ["DQ-2"]

    def test_pages_count_every_match_not_the_page(self, queues) -> None:
        queues.declarations = tuple(declaration(n) for n in range(1, 8))

        body = queues.client.get(
            DECLARATIONS, params={"q": "pump seal", "page": 2, "pageSize": 3}
        ).json()

        assert ids(body) == ["DQ-4", "DQ-5", "DQ-6"]
        assert body["total"] == 7


class TestDeclarationFacets:
    def test_are_not_narrowed_by_the_filters_the_dropdowns_set(self, queues) -> None:
        queues.declarations = (
            declaration(1),
            declaration(2, plant="1500", vendor="100999", vendor_name=None, repair_status="PO Issued"),
        )

        body = queues.client.get(
            DECLARATIONS, params={"plant": "1300", "vendor": "100234", "q": "seal 1"}
        ).json()

        assert body["total"] == 1
        facets = body["facets"]
        assert [p["plantId"] for p in facets["plants"]] == ["1300", "1500"]
        assert facets["plants"][0]["name"] == "Black Mountain Mining"
        # Sorted by what the dropdown shows: the name, else the code.
        assert facets["vendors"] == [
            {"vendor": "100999", "name": None},
            {"vendor": "100234", "name": "Acme Rewinds"},
        ]
        assert facets["repairStatuses"] == ["At Vendor", "PO Issued"]

    def test_follow_outstanding_only(self, queues) -> None:
        """The queue screen lists outstanding rows only; a vendor whose every
        line is declared would be a dropdown choice that finds nothing."""
        queues.declarations = (
            declaration(1),
            declaration(2, status="Completed", vendor="100999", vendor_name="Done Ltd"),
        )

        facets = queues.client.get(DECLARATIONS, params={"outstandingOnly": True}).json()[
            "facets"
        ]

        assert [v["vendor"] for v in facets["vendors"]] == ["100234"]

    def test_a_named_vendor_keeps_its_name(self, queues) -> None:
        queues.declarations = (
            declaration(1, vendor_name=None),
            declaration(2),
        )

        facets = queues.client.get(DECLARATIONS).json()["facets"]

        assert facets["vendors"] == [{"vendor": "100234", "name": "Acme Rewinds"}]


class TestDeclarationMeta:
    def test_still_counts_what_the_filters_match(self, queues) -> None:
        """Unchanged by paging: the screen reads the whole queue's numbers from a
        request with no filters, so this one stays a count of the matches."""
        queues.declarations = (declaration(1), declaration(2, plant="1500"))

        meta = queues.client.get(DECLARATIONS, params={"plant": "1500"}).json()["meta"]

        assert meta["total"] == 1


# --- The register ------------------------------------------------------------


def listing(queues, **params) -> dict:
    return queues.client.get(REGISTER, params=params).json()


class TestRegisterSearch:
    @pytest.fixture(autouse=True)
    def rows(self, queues):
        queues.lines = (
            repair_line(1),
            repair_line(2, description="GATE VALVE 4IN"),
            repair_line(3, pr_number="20000099", pr_item="20"),
        )

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("8000000001", ["4500000001-10"]),  # material number
            ("gate valve", ["4500000002-10"]),  # description, any case
            ("4500000003-10", ["4500000003-10"]),  # the line, as its detail page names it
            ("4500000003/10", ["4500000003-10"]),  # the PO, as the register shows it
            ("20000099/20", ["4500000003-10"]),  # the PR, as the register shows it
        ],
    )
    def test_searches_what_the_screen_shows(self, queues, q, expected) -> None:
        body = listing(queues, q=q)

        assert ids(body) == expected
        assert body["total"] == len(expected)


class TestRegisterFilters:
    def test_vendor_is_a_code_and_none_finds_the_lines_with_no_vendor(self, queues) -> None:
        queues.lines = (
            repair_line(1),
            repair_line(2, vendor="100999", vendor_name=None),
            repair_line(3, vendor=None, vendor_name=None, has_po_header=False),
        )

        assert ids(listing(queues, vendor="100999")) == ["4500000002-10"]
        assert ids(listing(queues, vendor="none")) == ["4500000003-10"]

    def test_overdue_status_matches_whatever_the_case(self, queues) -> None:
        queues.lines = (
            repair_line(1),
            repair_line(2, overdue_status="OVERDUE"),
            repair_line(3, overdue_status="NO_DUE_DATE"),
        )

        assert ids(listing(queues, overdueStatus="overdue")) == ["4500000002-10"]
        assert ids(listing(queues, overdueStatus="NO_DUE_DATE")) == ["4500000003-10"]

    def test_criticality_is_the_rating_the_row_shows_at_its_own_plant(self, queues) -> None:
        """The material is CRITICAL at 1500 only. Its 1300 line shows no rating,
        so it must not come back for CRITICAL -- it did while this filter
        matched any plant's rating for the material."""
        queues.lines = (repair_line(1), repair_line(2, material_id="8000000001", plant="1500"))
        queues.universe = (rating("8000000001", "1500", "CRITICAL"),)

        assert ids(listing(queues, criticality="critical")) == ["4500000002-10"]
        assert ids(listing(queues, criticality="none")) == ["4500000001-10"]

    def test_declaration_status_reads_like_the_cell(self, queues) -> None:
        queues.lines = (repair_line(1), repair_line(2), repair_line(3))
        # Line 3 has no queue row, so its cell reads Required.
        queues.declarations = (
            declaration(1, status="Completed"),
            declaration(2, status="Flagged"),
        )

        assert ids(listing(queues, declarationStatus="Completed")) == ["4500000001-10"]
        assert ids(listing(queues, declarationStatus="required")) == ["4500000003-10"]

    def test_department_is_the_code_of_the_repair_pr_on_both_kinds_of_row(self, queues) -> None:
        """3-Nov-2026, for the Overview's Department filter: its links must open
        the same slice."""
        queues.lines = (repair_line(1, pr_department="10305"), repair_line(2, pr_department="10316"))
        serve_requests(queues, repair_request(1, department="10305"))

        assert ids(listing(queues, department="10305")) == ["4500000001-10", "PR-2000029101-10"]
        assert ids(listing(queues, department="99999")) == []

    def test_department_falls_back_to_the_requisitioner_as_the_row_does(self, queues) -> None:
        """A PO line whose repair PR was not read shows its own requisitioner
        as its department, so the filter finds it by that code."""
        queues.lines = (
            repair_line(1, pr_department="10305", requisitioner="10316"),
            repair_line(2, pr_department=None, requisitioner="10316"),
        )

        assert ids(listing(queues, department="10316")) == ["4500000002-10"]
        assert ids(listing(queues, department="10305")) == ["4500000001-10"]
        assert [d["value"] for d in listing(queues)["facets"]["departments"]] == ["10305", "10316"]

    def test_late_aging_bands_the_days_past_the_expected_return(self, queues) -> None:
        """3-Nov-2026: the Overview's "days past expected return" row. A line
        not yet due, or with no date, is in no band; a PR has no date at all."""
        queues.lines = (
            repair_line(1, days_remaining=-20),
            repair_line(2, days_remaining=-90),
            repair_line(3, days_remaining=5),
            repair_line(4, days_remaining=None, due_date=None),
        )
        serve_requests(queues, repair_request(1))

        assert ids(listing(queues, lateAgingBucket="16-30")) == ["4500000001-10"]
        assert ids(listing(queues, lateAgingBucket="60+")) == ["4500000002-10"]
        assert ids(listing(queues, lateAgingBucket="0-15")) == []

    def test_pages_count_every_match_not_the_page(self, queues) -> None:
        queues.lines = tuple(repair_line(n) for n in range(1, 8))

        body = listing(queues, q="pump seal", page=2, pageSize=3)

        assert ids(body) == ["4500000004-10", "4500000005-10", "4500000006-10"]
        assert body["total"] == 7


class TestQuickViews:
    """The register screen's preset buttons: Overdue, Not declared and At
    vendor, each showing how many lines it would list. (With exceptions went
    with the exception queue on 3-Nov-2026.)"""

    def test_each_view_is_counted_over_the_filtered_lines(self, queues) -> None:
        queues.lines = (
            repair_line(1, overdue_status="OVERDUE"),
            repair_line(2, overdue_status="OVERDUE", repair_status="PO Issued"),
            repair_line(3, repair_status="PO Issued"),
            repair_line(4, plant="1500"),
        )
        queues.declarations = (declaration(3, status="Completed"),)

        body = listing(queues, plant="1300")

        assert body["viewCounts"] == {
            "all": 3,
            "overdue": 2,
            "undeclared": 2,
            "atVendor": 1,
            # Repair PRs with no PO yet: none served while the switch is off.
            "notOrdered": 0,
        }

    def test_a_view_narrows_the_list_but_not_the_counts(self, queues) -> None:
        """The counts are taken before the view, so the other buttons keep
        their numbers while one is selected."""
        queues.lines = (repair_line(1, overdue_status="OVERDUE"), repair_line(2))

        everything = listing(queues)
        overdue = listing(queues, view="overdue")

        assert ids(overdue) == ["4500000001-10"]
        assert overdue["total"] == 1
        assert overdue["viewCounts"] == everything["viewCounts"]

    def test_not_declared_is_required_only(self, queues) -> None:
        """27-Oct-2026: a Flagged line has its declaration (its finding is an
        exception), and a Historical one was raised before declarations were
        asked for -- neither is waiting for somebody to declare."""
        queues.lines = (repair_line(1), repair_line(2), repair_line(3), repair_line(4))
        queues.declarations = (
            declaration(1, status="Completed"),
            declaration(2, status="Flagged"),
            declaration(4, status="Historical"),
        )

        assert ids(listing(queues, view="undeclared")) == ["4500000003-10"]

    def test_an_unknown_view_is_refused(self, queues) -> None:
        queues.lines = (repair_line(1),)

        assert queues.client.get(REGISTER, params={"view": "everything"}).status_code == 422
        # Gone with the exception queue (3-Nov-2026).
        assert queues.client.get(REGISTER, params={"view": "withExceptions"}).status_code == 422


class TestStageAging:
    def test_bands_the_days_in_the_current_stage_not_the_days_open(self, queues) -> None:
        """The Overview's cells are days in the current stage; a cell's link
        must open those lines, not the ones open that long."""
        queues.lines = (
            repair_line(1, days_open=200, days_in_current_stage=10),
            repair_line(2, days_open=200, days_in_current_stage=90),
        )

        assert ids(listing(queues, stageAgingBucket="0-15")) == ["4500000001-10"]
        assert ids(listing(queues, stageAgingBucket="60+")) == ["4500000002-10"]


class TestOpenLinesOnly:
    """Every register line is open in SAP. What SAP has closed ("Delivery
    completed"), deleted or blocked is counted in the meta and never listed."""

    @pytest.fixture(autouse=True)
    def rows(self, queues):
        queues.lines = (repair_line(1), repair_line(2, repair_status="PO Issued"))
        queues.stats = RegisterStats(
            total_lines=2,
            open_lines=2,
            excluded_deleted_lines=5,
            excluded_delivery_completed_lines=644,
            excluded_blocked_lines=68,
        )

    def test_the_meta_counts_what_sap_closed(self, queues) -> None:
        meta = listing(queues)["meta"]

        assert meta["totalLines"] == meta["openLines"] == 2
        assert meta["excludedDeletedLines"] == 5
        assert meta["excludedDeliveryCompletedLines"] == 644
        assert meta["excludedBlockedLines"] == 68
        # Nothing is held back from the listing any more, so neither count
        # that described doing so is served.
        assert "heldLines" not in meta
        assert "blockedLines" not in meta

    def test_an_old_excludeheld_request_lists_the_same_lines(self, queues) -> None:
        """A screen built before 19-Oct-2026 still sends it, until it deploys."""
        assert ids(listing(queues, excludeHeld=True)) == ids(listing(queues))


class TestRegisterFacets:
    def test_list_every_value_whatever_the_filters(self, queues) -> None:
        queues.lines = (
            repair_line(1),
            repair_line(2, plant="1500", vendor="100999", vendor_name=None, repair_status="PO Issued"),
            repair_line(3, vendor=None, vendor_name=None),
        )
        queues.universe = (rating("8000000001", "1300", "CRITICAL"),)

        body = listing(queues, plant="1300", vendor="100234", q="seal 1")

        assert body["total"] == 1
        facets = body["facets"]
        assert [p["plantId"] for p in facets["plants"]] == ["1300", "1500"]
        assert facets["plants"][1]["name"] == "Gamsberg"
        # Sorted by what the dropdown shows: the name, else the code.
        assert facets["vendors"] == [
            {"vendor": "100999", "name": None},
            {"vendor": "100234", "name": "Acme Rewinds"},
        ]
        assert facets["hasUnknownVendor"] is True
        assert facets["repairStatuses"] == ["At Vendor", "PO Issued"]
        assert facets["criticalities"] == ["CRITICAL"]
        assert facets["hasUnrated"] is True
        # No line names its repair PR's department here, so each offers the
        # requisitioner its row shows instead.
        assert [d["value"] for d in facets["departments"]] == ["10316"]

    def test_departments_are_offered_by_code_with_their_names(self, queues, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.assistant.departments.name_for",
            lambda code: {"10305": "Engineering"}.get(code or ""),
        )
        queues.lines = (repair_line(1, pr_department="10316"), repair_line(2, pr_department="10305"))

        assert listing(queues, department="10305")["facets"]["departments"] == [
            {"value": "10305", "name": "Engineering"},
            {"value": "10316", "name": None},
        ]


# --- Repair PRs with no PO yet (27-Oct-2026) --------------------------------


def repair_request(n: int, **overrides) -> RepairRequest:
    """Open repair PR ``20000291{n}/10`` of material ``80000000{n}`` at 1300,
    raised on 01-Sep-2026 by department 10305, with no PO yet."""
    fields = dict(
        purchase_requisition=f"20000291{n:02d}",
        item="10",
        material_id=f"80000000{n:02d}",
        plant="1300",
        description=f"Motor {n}",
        quantity=Decimal(2),
        unit="EA",
        requested_on=date(2026, 9, 1),
        need_date=None,
        department="10305",
        requisition_status="B",
        document_type="ZMNP",
        repair_order="4000123",
        reservation="0001234",
        days_open=47,
        aging_bucket="31-60",
    )
    fields.update(overrides)
    return RepairRequest(**fields)


def serve_requests(queues, *requests: RepairRequest) -> None:
    """Put ``requests`` in the snapshot and switch on serving them."""
    queues.requests = requests
    queues.cfg = queues.cfg.model_copy(update={"repair_requests_enabled": True})


class TestRepairRequestsSwitchedOff:
    """Off by default, the register is exactly what it was before PR rows
    existed -- only the meta says how many there are."""

    def test_lists_no_pr_rows_and_counts_none_in_the_views(self, queues) -> None:
        queues.lines = (repair_line(1),)
        queues.requests = (repair_request(1),)
        queues.stats = RegisterStats(total_lines=1, open_lines=1, open_repair_requests=1)

        body = listing(queues)

        assert ids(body) == ["4500000001-10"]
        assert body["total"] == 1
        assert body["viewCounts"]["all"] == 1
        assert body["viewCounts"]["notOrdered"] == 0
        assert "PR Raised" not in body["facets"]["repairStatuses"]
        # Counted whether or not served; the line totals stay PO-only.
        assert body["meta"]["openRepairRequests"] == 1
        assert body["meta"]["totalLines"] == body["meta"]["openLines"] == 1

    def test_the_view_and_the_filters_find_nothing(self, queues) -> None:
        queues.requests = (repair_request(1),)

        assert listing(queues, view="notOrdered")["total"] == 0
        assert listing(queues, status="PR Raised")["total"] == 0
        assert listing(queues, q="2000029101")["total"] == 0

    def test_a_po_row_says_it_is_a_po_line_and_names_no_unknown_department(
        self, queues, monkeypatch
    ) -> None:
        """A department the list does not know is its code alone."""
        monkeypatch.setattr("app.assistant.departments.name_for", lambda code: None)
        queues.lines = (repair_line(1),)

        row = listing(queues)["items"][0]

        assert row["lineKind"] == "PO"
        assert row["requesterName"] is None


class TestRepairRequestRows:
    def test_follow_every_po_row_in_the_snapshot_order(self, queues) -> None:
        queues.lines = (repair_line(1), repair_line(2))
        serve_requests(
            queues, repair_request(1), repair_request(2, item="20"), repair_request(3)
        )

        body = listing(queues)

        assert ids(body) == [
            "4500000001-10",
            "4500000002-10",
            "PR-2000029101-10",
            "PR-2000029102-20",
            "PR-2000029103-10",
        ]
        assert body["total"] == 5
        assert [item["lineKind"] for item in body["items"]] == ["PO", "PO", "PR", "PR", "PR"]

    def test_a_pr_row_is_the_repair_with_nothing_ordered(self, queues, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.assistant.departments.name_for",
            lambda code: {"10305": "Engineering"}.get(code or ""),
        )
        queues.universe = (
            rating(
                "8000000001",
                "1300",
                "CRITICAL",
                stock_on_hand=Decimal(3),
                reorder_point=Decimal(1),
                planned_delivery_days=60,
            ),
        )
        serve_requests(queues, repair_request(1))

        row = listing(queues)["items"][0]

        assert row["id"] == "PR-2000029101-10"
        assert row["lineKind"] == "PR"
        assert row["repairPr"] == {"type": "PR", "documentNumber": "2000029101", "line": "10"}
        assert row["repairPo"] is None
        assert row["vendor"] is None
        assert row["vendorName"] is None
        assert row["poIssuedAt"] is None
        assert row["repairStatus"] == "PR Raised"
        assert row["overdueStatus"] == "NOT_ORDERED"
        assert row["declarationStatus"] == "Required"
        assert row["raisedAt"] == "2026-09-01"
        assert row["daysOpen"] == row["daysInCurrentStage"] == 47
        assert row["agingBucket"] == "31-60"
        assert Decimal(row["qtyUnderRepair"]) == 2
        assert row["requester"] == "10305"
        assert row["requesterName"] == "Engineering"
        # The material's position at the plant, as on a PO row.
        assert row["criticality"] == "CRITICAL"
        assert Decimal(row["stockOnHand"]) == 3
        assert Decimal(row["reorderPoint"]) == 1
        assert "exceptions" not in row

    def test_an_unknown_department_is_its_code_alone(self, queues, monkeypatch) -> None:
        monkeypatch.setattr("app.assistant.departments.name_for", lambda code: None)
        serve_requests(queues, repair_request(1), repair_request(2, department=None))

        rows = listing(queues)["items"]

        assert [(r["requester"], r["requesterName"]) for r in rows] == [
            ("10305", None),
            (None, None),
        ]

    def test_meta_line_counts_stay_po_only(self, queues) -> None:
        queues.lines = (repair_line(1),)
        queues.stats = RegisterStats(
            total_lines=1,
            open_lines=1,
            open_repair_requests=2,
            requisitions_excluded_deleted=48,
            requisitions_excluded_live_po=2310,
            requisitions_excluded_reservation_closed=14,
        )
        serve_requests(queues, repair_request(1), repair_request(2))

        meta = listing(queues)["meta"]

        assert meta["totalLines"] == meta["openLines"] == 1
        assert meta["openRepairRequests"] == 2
        assert meta["requisitionsExcludedDeleted"] == 48
        assert meta["requisitionsExcludedLivePo"] == 2310
        assert meta["requisitionsExcludedReservationClosed"] == 14

    def test_paging_runs_across_the_boundary(self, queues) -> None:
        queues.lines = tuple(repair_line(n) for n in range(1, 4))
        serve_requests(queues, repair_request(1), repair_request(2))

        second = listing(queues, page=2, pageSize=2)
        third = listing(queues, page=3, pageSize=2)

        assert ids(second) == ["4500000003-10", "PR-2000029101-10"]
        assert ids(third) == ["PR-2000029102-10"]
        assert second["total"] == third["total"] == 5


class TestRepairRequestFilters:
    @pytest.fixture(autouse=True)
    def rows(self, queues):
        queues.lines = (repair_line(1), repair_line(2, plant="1500"))
        serve_requests(
            queues,
            repair_request(1),
            repair_request(2, plant="1500", days_open=10, aging_bucket="0-15"),
        )

    def test_status_pr_raised_lists_the_prs_only(self, queues) -> None:
        assert ids(listing(queues, status="PR Raised")) == [
            "PR-2000029101-10",
            "PR-2000029102-10",
        ]
        assert ids(listing(queues, status="At Vendor")) == ["4500000001-10", "4500000002-10"]

    def test_not_ordered_is_an_overdue_status_of_its_own(self, queues) -> None:
        assert ids(listing(queues, overdueStatus="not_ordered")) == [
            "PR-2000029101-10",
            "PR-2000029102-10",
        ]
        assert ids(listing(queues, overdueStatus="ON_TIME")) == [
            "4500000001-10",
            "4500000002-10",
        ]
        assert listing(queues, overdueOnly=True)["total"] == 0

    def test_plant_material_and_aging(self, queues) -> None:
        assert ids(listing(queues, plant="1500")) == ["4500000002-10", "PR-2000029102-10"]
        assert ids(listing(queues, material="8000000001")) == [
            "4500000001-10",
            "PR-2000029101-10",
        ]
        assert ids(listing(queues, agingBucket="0-15")) == ["PR-2000029102-10"]
        assert ids(listing(queues, stageAgingBucket="0-15")) == ["PR-2000029102-10"]

    def test_a_pr_row_has_no_vendor(self, queues) -> None:
        assert ids(listing(queues, vendor="none")) == ["PR-2000029101-10", "PR-2000029102-10"]
        assert ids(listing(queues, vendor="100234")) == ["4500000001-10", "4500000002-10"]

    def test_declaration_status_reads_required(self, queues) -> None:
        queues.declarations = (
            declaration(1, status="Completed"),
            declaration(2, status="Completed"),
        )

        assert ids(listing(queues, declarationStatus="required")) == [
            "PR-2000029101-10",
            "PR-2000029102-10",
        ]
        assert ids(listing(queues, declarationStatus="Completed")) == [
            "4500000001-10",
            "4500000002-10",
        ]

    def test_criticality(self, queues) -> None:
        queues.universe = (rating("8000000002", "1500", "CRITICAL"),)

        assert ids(listing(queues, criticality="CRITICAL")) == [
            "4500000002-10",
            "PR-2000029102-10",
        ]

    @pytest.mark.parametrize(
        "q", ["2000029101", "2000029101/10", "PR-2000029101-10", "pr-2000029101", "motor 1"]
    )
    def test_search_finds_the_pr_by_its_number(self, queues, q) -> None:
        assert ids(listing(queues, q=q)) == ["PR-2000029101-10"]

    def test_facets_offer_what_the_pr_rows_show(self, queues) -> None:
        facets = listing(queues, plant="1300")["facets"]

        assert "PR Raised" in facets["repairStatuses"]
        assert facets["hasUnknownVendor"] is True
        assert [p["plantId"] for p in facets["plants"]] == ["1300", "1500"]


class TestNotOrderedView:
    def test_counted_alone_and_as_undeclared(self, queues) -> None:
        """27-Oct-2026: a PR row can be declared on, so an undeclared one
        counts as undeclared too."""
        queues.lines = (repair_line(1), repair_line(2, overdue_status="OVERDUE"))
        serve_requests(queues, repair_request(1), repair_request(2))

        assert listing(queues)["viewCounts"] == {
            "all": 4,
            "overdue": 1,
            "undeclared": 4,
            "atVendor": 2,
            "notOrdered": 2,
        }

    def test_lists_the_prs_and_the_other_views_leave_them_out(self, queues) -> None:
        queues.lines = (repair_line(1),)
        serve_requests(queues, repair_request(1))

        assert ids(listing(queues, view="notOrdered")) == ["PR-2000029101-10"]
        assert ids(listing(queues, view="undeclared")) == ["4500000001-10", "PR-2000029101-10"]
        assert ids(listing(queues, view="atVendor")) == ["4500000001-10"]
        assert listing(queues, view="overdue")["total"] == 0

    def test_counts_after_the_filters(self, queues) -> None:
        serve_requests(queues, repair_request(1), repair_request(2, plant="1500"))

        assert listing(queues, plant="1500")["viewCounts"]["notOrdered"] == 1


class TestRepairRequestDetail:
    def test_answers_with_the_row_and_its_three_stages(self, queues, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.assistant.departments.name_for",
            lambda code: {"10305": "Engineering"}.get(code or ""),
        )
        serve_requests(queues, repair_request(1))

        response = queues.client.get(f"{REGISTER}/PR-2000029101/10")

        assert response.status_code == 200
        body = response.json()
        assert body["line"]["id"] == "PR-2000029101-10"
        assert body["line"]["lineKind"] == "PR"
        assert [(s["stage"], s["label"], s["occurredAt"]) for s in body["timeline"]] == [
            ("pr_raised", "PR raised", "2026-09-01"),
            ("attested", "Condition declared", None),
            ("po_raised", "Repair PO raised", None),
        ]
        assert body["timeline"][0]["evidence"] == (
            "Repair PR 2000029101/10, raised by department 10305 (Engineering)."
        )
        assert body["timeline"][0]["daysSince"] == 47
        assert body["timeline"][1]["evidence"] == "Not declared yet."
        assert body["timeline"][2]["evidence"] == "No PO yet."

    def test_names_no_department_it_does_not_know(self, queues, monkeypatch) -> None:
        monkeypatch.setattr("app.assistant.departments.name_for", lambda code: None)
        serve_requests(queues, repair_request(1), repair_request(2, department=None))

        first = queues.client.get(f"{REGISTER}/PR-2000029101/10").json()
        second = queues.client.get(f"{REGISTER}/PR-2000029102/10").json()

        assert first["timeline"][0]["evidence"] == (
            "Repair PR 2000029101/10, raised by department 10305."
        )
        assert second["timeline"][0]["evidence"] == "Repair PR 2000029102/10."

    def test_a_zero_padded_item_finds_the_same_pr(self, queues) -> None:
        serve_requests(queues, repair_request(1))

        assert queues.client.get(f"{REGISTER}/PR-2000029101/00010").status_code == 200

    def test_is_404_while_switched_off(self, queues) -> None:
        queues.requests = (repair_request(1),)

        assert queues.client.get(f"{REGISTER}/PR-2000029101/10").status_code == 404

    def test_an_unknown_pr_is_404(self, queues) -> None:
        serve_requests(queues, repair_request(1))

        assert queues.client.get(f"{REGISTER}/PR-2000029199/10").status_code == 404
        assert queues.client.get(f"{REGISTER}/PR-2000029101/20").status_code == 404


class TestRepairLineDetail:
    def test_the_timeline_has_no_removed_from_machine_stage(self, queues) -> None:
        # Dropped 27-Oct-2026 (ruled 26-Oct): SAP has no record of a unit
        # coming off a machine that names the repair PO, so the stage was
        # blank on every line.
        queues.lines = (repair_line(1, dispatched_at=date(2026, 9, 5)),)

        response = queues.client.get(f"{REGISTER}/4500000001/10")

        assert response.status_code == 200
        timeline = response.json()["timeline"]
        # The repair starts at its PR since 27-Oct-2026.
        assert [(s["stage"], s["label"]) for s in timeline] == [
            ("pr_raised", "PR raised"),
            ("attested", "Condition declared"),
            ("po_raised", "Repair PO raised"),
            ("dispatched", "Sent to vendor"),
            ("at_vendor", "At vendor"),
        ]
        assert not any("machine" in s["evidence"] for s in timeline)


class TestRepairableUnitRoute:
    """GET /api/i8/repairable-unit counts open repair PRs only while the switch
    is on, and splits usable from damaged stock -- as the assistant does."""

    @staticmethod
    def _universe_row(**overrides):
        from app.initiatives.i8.universe import UniverseRow

        fields = dict(
            material_id="8000000001",
            plant="1300",
            description="Motor 1",
            material_type="ZREP",
            stock_on_hand=Decimal(0),
            storage_locations=1,
            reorder_point=None,
            mrp_type="ND",
            planned_delivery_days=30,
            criticality=None,
            open_repair_lines=0,
            qty_under_repair=Decimal(0),
            in_material_master=True,
        )
        fields.update(overrides)
        return UniverseRow(**fields)

    def _get(self, queues) -> dict:
        response = queues.client.get(
            "/api/i8/repairable-unit", params={"material": "8000000001", "plant": "1300"}
        )
        assert response.status_code == 200
        return response.json()

    def test_a_pr_only_part_has_a_unit_only_when_switched_on(self, queues, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.assistant.departments.name_for",
            lambda code: {"10305": "Engineering"}.get(code or ""),
        )
        queues.universe = (self._universe_row(),)
        queues.requests = (repair_request(1),)

        off = self._get(queues)
        assert off["exists"] is False
        assert off["openRepairRequestLines"] == 0
        assert off["requestEvidence"] == []

        serve_requests(queues, repair_request(1))
        on = self._get(queues)
        assert on["exists"] is True
        assert "REPAIR_REQUESTED" in on["sources"]
        assert on["openRepairRequestLines"] == 1
        assert on["requestEvidence"] == [
            {
                "purchaseRequisition": "2000029101",
                "item": "10",
                "quantity": "2",
                "requestedAt": "2026-09-01",
                "daysSinceRequested": 47,
                "department": "10305",
                "departmentName": "Engineering",
                "repairOrder": "4000123",
            }
        ]

    def test_damaged_only_stock_is_no_unit(self, queues) -> None:
        queues.universe = (
            self._universe_row(
                stock_on_hand=Decimal(2),
                usable_stock=Decimal(0),
                damaged_stock=Decimal(2),
                stock_by_condition=(("DAMAGED", Decimal(2)),),
                stock_condition_source="MCHB",
            ),
        )

        body = self._get(queues)

        assert body["exists"] is False
        assert body["stockOnHand"] == "2"
        assert body["usableStock"] == "0"
        assert body["damagedStock"] == "2"
        assert body["stockConditionKnown"] is True
        assert body["stockSource"] == "MCHB"


class TestDeclarationsOnTheRegister:
    """27-Oct-2026: PO rows name the department of their repair PR, PR rows
    carry their own declaration, and every row says how it was linked."""

    def test_a_po_row_names_the_department_of_its_repair_pr(self, queues, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.assistant.departments.name_for",
            lambda code: {"10305": "Engineering"}.get(code or ""),
        )
        queues.lines = (repair_line(1, pr_department="10305"), repair_line(2))

        rows = listing(queues)["items"]

        # Line 2's PR was not read: its own requisitioner, as before.
        assert [(r["requester"], r["requesterName"]) for r in rows] == [
            ("10305", "Engineering"),
            ("10316", None),
        ]

    def test_a_po_row_says_how_its_declaration_was_linked(self, queues) -> None:
        queues.lines = (repair_line(1), repair_line(2))
        queues.declarations = (
            declaration(1, status="Completed", declaration_link="BOUND"),
            declaration(2, status="Historical"),
        )

        rows = listing(queues)["items"]

        assert [(r["declarationStatus"], r["declarationLink"]) for r in rows] == [
            ("Completed", "BOUND"),
            ("Historical", None),
        ]

    def test_a_pr_row_carries_its_declaration(self, queues) -> None:
        serve_requests(queues, repair_request(1), repair_request(2))
        queues.declarations = (
            declaration(
                1,
                id="D-PR-2000029101-10",
                related_repair_id="PR-2000029101-10",
                repair_document_type="PR",
                status="Completed",
                declaration_id="att-1",
                declared_by="A. Planner",
                condition="Repairable",
                declaration_link="WINDOW",
            ),
        )

        body = listing(queues)
        first, second = body["items"]

        assert (first["declarationStatus"], first["declarationId"], first["declarationLink"]) == (
            "Completed",
            "att-1",
            "WINDOW",
        )
        assert second["declarationStatus"] == "Required"
        assert body["viewCounts"]["undeclared"] == 1
        assert ids(listing(queues, view="undeclared")) == ["PR-2000029102-10"]
        assert ids(listing(queues, declarationStatus="Completed")) == ["PR-2000029101-10"]

    def test_rows_name_the_assistant_session_of_their_declaration(self, queues) -> None:
        """28-Oct-2026: the repair page links to the visit the declaration was
        made in; a declaration made on a register screen has no session."""
        queues.lines = (repair_line(1), repair_line(2), repair_line(3))
        serve_requests(queues, repair_request(4))
        queues.declarations = (
            declaration(1, status="Completed", declaration_session_id="S7K2M4P8Q1"),
            declaration(2, status="Completed"),
            declaration(
                4,
                id="D-PR-2000029104-10",
                related_repair_id="PR-2000029104-10",
                repair_document_type="PR",
                status="Completed",
                declaration_session_id="SQ4X9B2T7M",
            ),
        )

        rows = {r["id"]: r for r in listing(queues)["items"]}

        assert rows["4500000001-10"]["declarationSessionId"] == "S7K2M4P8Q1"
        assert rows["4500000002-10"]["declarationSessionId"] is None
        assert rows["4500000003-10"]["declarationSessionId"] is None  # nothing covers it
        assert rows["PR-2000029104-10"]["declarationSessionId"] == "SQ4X9B2T7M"


class TestStockSplitOnTheRegister:
    """28-Oct-2026: usable and damaged stock on each register row, from the
    universe row of its material at its plant."""

    def test_a_known_split_is_served(self, queues) -> None:
        queues.lines = (repair_line(1),)
        queues.universe = (
            rating(
                "8000000001", "1300", None,
                stock_on_hand=Decimal(3), usable_stock=Decimal(2), damaged_stock=Decimal(1),
                stock_condition_source="MCHB",
            ),
        )

        [row] = listing(queues)["items"]

        assert (Decimal(row["usableStock"]), Decimal(row["damagedStock"])) == (2, 1)
        assert row["stockConditionKnown"] is True

    def test_an_unknown_split_is_null_not_zero(self, queues) -> None:
        queues.lines = (repair_line(1),)
        queues.universe = (rating("8000000001", "1300", None, stock_on_hand=Decimal(3)),)

        [row] = listing(queues)["items"]

        assert (row["usableStock"], row["damagedStock"], row["stockConditionKnown"]) == (None, None, False)

    def test_no_universe_row_is_null_throughout(self, queues) -> None:
        queues.lines = (repair_line(1),)

        [row] = listing(queues)["items"]

        assert (row["usableStock"], row["damagedStock"], row["stockConditionKnown"]) == (None, None, None)

    def test_a_pr_row_gets_the_same_split(self, queues) -> None:
        serve_requests(queues, repair_request(1))
        queues.universe = (
            rating(
                "8000000001", "1300", None,
                usable_stock=Decimal(0), damaged_stock=Decimal(2), stock_condition_source="MCHB",
            ),
        )

        [row] = listing(queues)["items"]

        assert (Decimal(row["usableStock"]), Decimal(row["damagedStock"])) == (0, 2)
        assert row["stockConditionKnown"] is True
