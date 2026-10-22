"""The register and the two queues, paged on the server.

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
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.i8.router import attestation_view_dependency, snapshot_dependency
from app.core.db import get_db
from app.initiatives.i8.declarations import DeclarationRow
from app.initiatives.i8.exception_state import assign, set_status
from app.initiatives.i8.exceptions import ExceptionItem, ExceptionStats
from app.initiatives.i8.models import ExceptionEvent
from app.initiatives.i8.register import RegisterStats, RepairLine

DECLARATIONS = "/api/i8/declarations"
EXCEPTIONS = "/api/i8/exceptions"
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


def rating(material_id: str, plant: str, criticality: str | None) -> SimpleNamespace:
    """A universe row: the material's position at one plant."""
    return SimpleNamespace(
        material_id=material_id,
        plant=plant,
        criticality=criticality,
        stock_on_hand=None,
        reorder_point=None,
        planned_delivery_days=None,
    )


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


def exception(n: int, type: str = "MISSING_ATTESTATION", **overrides) -> ExceptionItem:
    """One item raised against repair line ``45000000{n}-10``."""
    document = f"45000000{n:02d}"
    fields = dict(
        id=f"EX-{type}-{document}-10",
        type=type,
        severity="warning",
        material_id=f"80000000{n:02d}",
        description=f"Pump seal {n}",
        plant="1300",
        purchasing_document=document,
        item="10",
        title="No condition declaration",
        detail="No attestation for this material and plant within 30 days.",
        raised_at=date(2026, 9, 1),
    )
    fields.update(overrides)
    return ExceptionItem(**fields)


def stats_of(items: tuple[ExceptionItem, ...]) -> ExceptionStats:
    """The counts the real build would have made over ``items``."""
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for item in items:
        by_type[item.type] = by_type.get(item.type, 0) + 1
        by_severity[item.severity] = by_severity.get(item.severity, 0) + 1
    historical = sum(1 for item in items if item.pre_automation)
    return ExceptionStats(
        total=len(items),
        by_type=by_type,
        by_severity=by_severity,
        lines_checked=40,
        lines_covered=2,
        attestation_window_days=30,
        pre_automation=historical,
        actionable=len(items) - historical,
        acquisitions_checked=7,
        justification_window_days=14,
    )


@pytest.fixture
def factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Only the history table: everything else these routes read comes from the
    # view and the snapshot, which are replaced below.
    ExceptionEvent.__table__.create(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def queues(factory):
    """A client over the rows a test puts in ``state``."""
    from app.main import create_app

    app = create_app()
    state = SimpleNamespace(declarations=(), exceptions=(), lines=(), universe=(), stats=None)

    def _db():
        with factory() as session:
            yield session

    def _view():
        return SimpleNamespace(
            declarations=state.declarations,
            exceptions=state.exceptions,
            exception_stats=stats_of(state.exceptions),
            coverage=SimpleNamespace(window_days=30),
            # What the register reads off the view: each line's queue row by
            # its key, and its justification (none here).
            declaration_by_line={
                tuple(row.related_repair_id.split("-", 1)): row for row in state.declarations
            },
            justifications={},
        )

    def _snapshot():
        return SimpleNamespace(
            reference_date=date(2026, 10, 18),
            lines=state.lines,
            universe=state.universe,
            register_stats=state.stats or register_stats(state.lines),
        )

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[snapshot_dependency] = _snapshot
    app.dependency_overrides[attestation_view_dependency] = _view
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


# --- The exception queue ---------------------------------------------------


class TestExceptionSearch:
    @pytest.fixture(autouse=True)
    def rows(self, queues):
        queues.exceptions = (
            exception(1),
            exception(2, description="Gearbox bearing"),
            exception(3, type="OVERDUE_REPAIR"),
        )

    @pytest.mark.parametrize(
        ("q", "expected"),
        [
            ("ex-overdue", ["EX-OVERDUE_REPAIR-4500000003-10"]),  # the item's id
            ("8000000001", ["EX-MISSING_ATTESTATION-4500000001-10"]),  # material
            ("gearbox", ["EX-MISSING_ATTESTATION-4500000002-10"]),  # description
            ("4500000002-10", ["EX-MISSING_ATTESTATION-4500000002-10"]),  # repair line
        ],
    )
    def test_searches_the_fields_the_screen_did(self, queues, q, expected) -> None:
        body = queues.client.get(EXCEPTIONS, params={"q": q}).json()

        assert ids(body) == expected
        assert body["total"] == len(expected)


class TestActionableOnly:
    def test_leaves_out_what_predates_the_control_but_keeps_counting_it(self, queues) -> None:
        queues.exceptions = (exception(1), exception(2, pre_automation=True))

        body = queues.client.get(EXCEPTIONS, params={"actionableOnly": True}).json()

        assert ids(body) == ["EX-MISSING_ATTESTATION-4500000001-10"]
        # Both numbers stay in the meta: it describes the check, not the view.
        assert body["meta"]["total"] == 2
        assert body["meta"]["preAutomation"] == 1


class TestExcludeType:
    @pytest.fixture(autouse=True)
    def rows(self, queues):
        queues.exceptions = (
            exception(1),
            exception(2, type="OVERDUE_REPAIR", severity="critical"),
            exception(3, type="UNJUSTIFIED_ACQUISITION", plant="1500"),
            exception(4, type="UNJUSTIFIED_ACQUISITION", pre_automation=True),
        )

    def test_takes_the_items_out_of_the_list(self, queues) -> None:
        body = queues.client.get(EXCEPTIONS, params={"excludeType": "UNJUSTIFIED_ACQUISITION"}).json()

        assert body["total"] == 2
        assert {item["type"] for item in body["items"]} == {"MISSING_ATTESTATION", "OVERDUE_REPAIR"}

    def test_takes_them_off_the_counts_too(self, queues) -> None:
        meta = queues.client.get(EXCEPTIONS, params={"excludeType": "unjustified_acquisition"}).json()[
            "meta"
        ]

        assert meta["total"] == 2
        assert meta["byType"] == {"MISSING_ATTESTATION": 1, "OVERDUE_REPAIR": 1}
        assert meta["bySeverity"] == {"warning": 1, "critical": 1}
        # One of the two dropped was pre-automation: it comes off that count,
        # and the other off the actionable one.
        assert meta["preAutomation"] == 0
        assert meta["actionable"] == 2
        assert "UNJUSTIFIED_ACQUISITION" not in meta["typesRaised"]

    def test_leaves_the_runs_own_measurements_alone(self, queues) -> None:
        meta = queues.client.get(EXCEPTIONS, params={"excludeType": "UNJUSTIFIED_ACQUISITION"}).json()[
            "meta"
        ]

        assert meta["linesChecked"] == 40
        assert meta["linesCovered"] == 2
        assert meta["acquisitionsChecked"] == 7

    def test_takes_the_type_out_of_types_raised_even_with_none_raised(self, queues) -> None:
        """The case live data is in today: the check runs and raises nothing."""
        queues.exceptions = (exception(1),)

        meta = queues.client.get(EXCEPTIONS, params={"excludeType": "UNJUSTIFIED_ACQUISITION"}).json()[
            "meta"
        ]

        assert "UNJUSTIFIED_ACQUISITION" not in meta["typesRaised"]
        assert "MISSING_ATTESTATION" in meta["typesRaised"]

    def test_without_it_nothing_changes(self, queues) -> None:
        body = queues.client.get(EXCEPTIONS).json()

        assert body["total"] == 4
        assert body["meta"]["total"] == 4
        assert "UNJUSTIFIED_ACQUISITION" in body["meta"]["typesRaised"]

    def test_does_not_resolve_the_excluded_items(self, queues, factory) -> None:
        """Auto-resolution must still see them. An item somebody is working, of
        a type this caller does not list, is still raised -- resolving it
        because one screen looked away would close somebody else's work."""
        excluded = queues.exceptions[2].id
        with factory() as db:
            assign(db, excluded, "priya", actor="tester")

        queues.client.get(EXCEPTIONS, params={"excludeType": "UNJUSTIFIED_ACQUISITION"})

        with factory() as db:
            kinds = db.execute(
                select(ExceptionEvent.kind).where(ExceptionEvent.exception_id == excluded)
            ).scalars()
            assert list(kinds) == ["ASSIGNED"]


class TestExceptionFacets:
    def test_list_every_plant_and_type_whatever_the_filters(self, queues) -> None:
        queues.exceptions = (
            exception(1),
            exception(2, type="OVERDUE_REPAIR", plant="1500"),
            exception(3, type="UNJUSTIFIED_ACQUISITION", plant="1600"),
        )

        body = queues.client.get(
            EXCEPTIONS,
            params={
                "plant": "1300",
                "type": "MISSING_ATTESTATION",
                "status": "OPEN",
                "q": "pump",
                "excludeType": "UNJUSTIFIED_ACQUISITION",
            },
        ).json()

        assert body["total"] == 1
        # 1600 is only on the excluded type, so it is not offered.
        assert [p["plantId"] for p in body["facets"]["plants"]] == ["1300", "1500"]
        assert body["facets"]["plants"][1]["name"] == "Gamsberg"
        assert body["facets"]["types"] == ["MISSING_ATTESTATION", "OVERDUE_REPAIR"]


# --- One exception, by id --------------------------------------------------


class TestOneException:
    def test_answers_with_the_item_and_its_state(self, queues, factory) -> None:
        queues.exceptions = (exception(1), exception(2))
        wanted = queues.exceptions[1].id
        with factory() as db:
            set_status(db, wanted, "CLOSED", actor="tester", note="Not a real gap.")

        response = queues.client.get(f"{EXCEPTIONS}/{wanted}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == wanted
        assert body["status"] == "CLOSED"
        assert body["stillDetected"] is True
        assert body["repairLine"]["documentNumber"] == "4500000002"

    def test_an_item_no_longer_raised_answers_from_its_history(self, queues, factory) -> None:
        gone = "EX-MISSING_ATTESTATION-4500000099-10"
        with factory() as db:
            assign(db, gone, "priya", actor="tester")

        body = queues.client.get(f"{EXCEPTIONS}/{gone}").json()

        assert body["stillDetected"] is False
        assert body["owner"] == "priya"

    def test_an_id_nothing_raised_or_touched_is_404(self, queues) -> None:
        queues.exceptions = (exception(1),)

        response = queues.client.get(f"{EXCEPTIONS}/EX-MISSING_ATTESTATION-1-1")

        assert response.status_code == 404

    def test_is_read_only(self, queues, factory) -> None:
        """Unlike the listing it does not auto-resolve: a lookup writes nothing."""
        gone = "EX-MISSING_ATTESTATION-4500000099-10"
        with factory() as db:
            assign(db, gone, "priya", actor="tester")

        queues.client.get(f"{EXCEPTIONS}/{gone}")

        with factory() as db:
            kinds = db.execute(
                select(ExceptionEvent.kind).where(ExceptionEvent.exception_id == gone)
            ).scalars()
            assert list(kinds) == ["ASSIGNED"]


# --- Two statuses, OPEN and CLOSED (21-Oct-2026) -----------------------------


def legacy_entry(exception_id: str, kind: str, status: str) -> ExceptionEvent:
    """A history row as written before IN_PROGRESS and RESOLVED were retired."""
    return ExceptionEvent(exception_id=exception_id, kind=kind, status=status, actor="system")


class TestTwoStatuses:
    def test_closing_without_a_note_is_refused(self, queues) -> None:
        queues.exceptions = (exception(1),)
        wanted = queues.exceptions[0].id

        response = queues.client.post(
            f"{EXCEPTIONS}/{wanted}/events", json={"kind": "STATUS", "status": "CLOSED"}
        )

        assert response.status_code == 422
        assert "note" in response.json()["detail"]

    def test_closing_with_a_note_closes_it(self, queues) -> None:
        queues.exceptions = (exception(1),)
        wanted = queues.exceptions[0].id

        response = queues.client.post(
            f"{EXCEPTIONS}/{wanted}/events",
            json={"kind": "STATUS", "status": "CLOSED", "text": "Pre-dates the control."},
        )

        assert response.status_code == 201
        assert response.json()["status"] == "CLOSED"

    @pytest.mark.parametrize("retired", ["IN_PROGRESS", "RESOLVED"])
    def test_a_retired_status_cannot_be_written(self, queues, retired) -> None:
        queues.exceptions = (exception(1),)
        wanted = queues.exceptions[0].id

        response = queues.client.post(
            f"{EXCEPTIONS}/{wanted}/events",
            json={"kind": "STATUS", "status": retired, "text": "Old screen."},
        )

        assert response.status_code == 422

    def test_old_history_is_served_in_the_two_statuses(self, queues, factory) -> None:
        """Rows are never rewritten, so the old words are still stored. The
        item and every entry in its history must still answer OPEN or CLOSED,
        or the response would not validate."""
        queues.exceptions = (exception(1), exception(2))
        in_progress, resolved = (item.id for item in queues.exceptions)
        with factory() as db:
            db.add(legacy_entry(in_progress, "STATUS", "IN_PROGRESS"))
            db.add(legacy_entry(resolved, "STATUS", "RESOLVED"))
            db.commit()

        first = queues.client.get(f"{EXCEPTIONS}/{in_progress}").json()
        second = queues.client.get(f"{EXCEPTIONS}/{resolved}").json()

        assert first["status"] == "OPEN"
        assert first["history"][0]["status"] == "OPEN"
        assert second["status"] == "CLOSED"
        assert second["history"][0]["status"] == "CLOSED"

    def test_an_old_status_filter_still_finds_its_items(self, queues, factory) -> None:
        # A bookmark from before the change: ?status=RESOLVED means CLOSED now.
        queues.exceptions = (exception(1), exception(2))
        closed = queues.exceptions[1].id
        with factory() as db:
            set_status(db, closed, "CLOSED", actor="tester", note="Not a real gap.")

        assert ids(queues.client.get(EXCEPTIONS, params={"status": "RESOLVED"}).json()) == [closed]
        assert ids(queues.client.get(EXCEPTIONS, params={"status": "CLOSED"}).json()) == [closed]

    def test_an_item_the_check_drops_is_closed_by_the_system(self, queues, factory) -> None:
        gone = "EX-MISSING_ATTESTATION-4500000099-10"
        with factory() as db:
            assign(db, gone, "priya", actor="tester")
        queues.exceptions = (exception(1),)

        queues.client.get(EXCEPTIONS)

        body = queues.client.get(f"{EXCEPTIONS}/{gone}").json()
        assert body["status"] == "CLOSED"
        assert body["history"][-1]["kind"] == "AUTO_RESOLVED"
        assert body["history"][-1]["actor"] == "system"


class TestTypeCounts:
    """The Exceptions screen's type tabs: how many items each would list."""

    def test_counted_after_every_filter_but_the_type(self, queues, factory) -> None:
        queues.exceptions = (
            exception(1),
            exception(2),
            exception(3, type="OVERDUE_REPAIR", severity="critical"),
            exception(4, type="OVERDUE_REPAIR", severity="critical", plant="1500"),
        )
        with factory() as db:
            set_status(db, queues.exceptions[1].id, "CLOSED", actor="tester", note="Handled.")

        body = queues.client.get(
            EXCEPTIONS, params={"status": "OPEN", "plant": "1300", "type": "OVERDUE_REPAIR"}
        ).json()

        # The type narrows the list, not the counts; status and plant do both.
        assert ids(body) == ["EX-OVERDUE_REPAIR-4500000003-10"]
        assert body["typeCounts"] == {"MISSING_ATTESTATION": 1, "OVERDUE_REPAIR": 1}

    def test_meta_still_describes_the_whole_check(self, queues, factory) -> None:
        """typeCounts follow the filters; meta.byType does not, on purpose."""
        queues.exceptions = (exception(1), exception(2))
        with factory() as db:
            set_status(db, queues.exceptions[0].id, "CLOSED", actor="tester", note="Handled.")

        body = queues.client.get(EXCEPTIONS, params={"status": "OPEN"}).json()

        assert body["typeCounts"] == {"MISSING_ATTESTATION": 1}
        assert body["meta"]["byType"] == {"MISSING_ATTESTATION": 2}


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

    def test_with_exceptions_does_not_count_an_excluded_type(self, queues) -> None:
        queues.lines = (repair_line(1), repair_line(2), repair_line(3))
        queues.exceptions = (exception(1), exception(2, type="UNJUSTIFIED_ACQUISITION"))

        assert ids(
            listing(queues, withExceptions=True, excludeExceptionType="UNJUSTIFIED_ACQUISITION")
        ) == ["4500000001-10"]
        assert ids(listing(queues, withExceptions=True)) == ["4500000001-10", "4500000002-10"]

    def test_pages_count_every_match_not_the_page(self, queues) -> None:
        queues.lines = tuple(repair_line(n) for n in range(1, 8))

        body = listing(queues, q="pump seal", page=2, pageSize=3)

        assert ids(body) == ["4500000004-10", "4500000005-10", "4500000006-10"]
        assert body["total"] == 7


class TestQuickViews:
    """The register screen's preset buttons: Overdue, Not declared, At vendor
    and With exceptions, each showing how many lines it would list."""

    def test_each_view_is_counted_over_the_filtered_lines(self, queues) -> None:
        queues.lines = (
            repair_line(1, overdue_status="OVERDUE"),
            repair_line(2, overdue_status="OVERDUE", repair_status="PO Issued"),
            repair_line(3, repair_status="PO Issued"),
            repair_line(4, plant="1500"),
        )
        queues.declarations = (declaration(3, status="Completed"),)
        queues.exceptions = (exception(1),)

        body = listing(queues, plant="1300")

        assert body["viewCounts"] == {
            "all": 3,
            "overdue": 2,
            "undeclared": 2,
            "atVendor": 1,
            "withExceptions": 1,
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

    def test_not_declared_is_required_and_flagged(self, queues) -> None:
        """A Flagged line was declared not repairable and sent anyway: it still
        wants somebody, so it is not declared in the sense the button means."""
        queues.lines = (repair_line(1), repair_line(2), repair_line(3))
        queues.declarations = (
            declaration(1, status="Completed"),
            declaration(2, status="Flagged"),
        )

        assert ids(listing(queues, view="undeclared")) == ["4500000002-10", "4500000003-10"]

    def test_an_unknown_view_is_refused(self, queues) -> None:
        queues.lines = (repair_line(1),)

        assert queues.client.get(REGISTER, params={"view": "everything"}).status_code == 422


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


class TestLineExceptions:
    def test_each_row_carries_its_own_less_the_excluded_types(self, queues) -> None:
        queues.lines = (repair_line(1), repair_line(2))
        queues.exceptions = (
            exception(1),
            exception(1, type="OVERDUE_REPAIR", severity="critical"),
            exception(1, type="UNJUSTIFIED_ACQUISITION"),
        )

        rows = {
            item["id"]: item
            for item in listing(queues, excludeExceptionType="UNJUSTIFIED_ACQUISITION")["items"]
        }

        assert rows["4500000001-10"]["exceptions"] == [
            {
                "id": "EX-MISSING_ATTESTATION-4500000001-10",
                "type": "MISSING_ATTESTATION",
                "severity": "warning",
            },
            {
                "id": "EX-OVERDUE_REPAIR-4500000001-10",
                "type": "OVERDUE_REPAIR",
                "severity": "critical",
            },
        ]
        # Checked and clean is an empty list. Null would mean "not computed",
        # which the screen shows as "Not checked" -- a different claim.
        assert rows["4500000002-10"]["exceptions"] == []


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
