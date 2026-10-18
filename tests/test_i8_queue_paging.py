"""The declaration and exception queues, paged on the server.

Both screens fetched every row -- 5,153 and 5,978 of them -- and filtered in the
browser, so each page weighed 17 MB. They now ask for one page, which moves
three things here that the browser used to do over the rows it held:

* the search box (``q``), and the filters that had no server side yet
  (``vendor``, ``repairStatus``, ``actionableOnly``);
* the dropdown values (``facets``), which one page cannot supply;
* dropping a check the exception screen does not show (``excludeType``), which
  has to come off the counts as well as the rows.

``GET /exceptions/{id}`` is new for the same reason: a link to one item can no
longer be opened by finding it among the rows on screen.

The snapshot and the attestation view are replaced with a few hand-built rows,
and the one table the exception routes read and write -- the history -- is
in-memory SQLite, so this runs without the Azure database or the extracts.
"""

from __future__ import annotations

from datetime import date
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

DECLARATIONS = "/api/i8/declarations"
EXCEPTIONS = "/api/i8/exceptions"


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
        has_active_repair=True,
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
        is_open_repair=True,
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
    state = SimpleNamespace(declarations=(), exceptions=())

    def _db():
        with factory() as session:
            yield session

    def _view():
        return SimpleNamespace(
            declarations=state.declarations,
            exceptions=state.exceptions,
            exception_stats=stats_of(state.exceptions),
            coverage=SimpleNamespace(window_days=30),
        )

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[snapshot_dependency] = lambda: SimpleNamespace(
        reference_date=date(2026, 10, 18)
    )
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
        queues.declarations = (declaration(1), declaration(2, repair_status="Closed"))

        body = queues.client.get(DECLARATIONS, params={"repairStatus": "closed"}).json()

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
            declaration(2, plant="1500", vendor="100999", vendor_name=None, repair_status="Closed"),
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
        assert facets["repairStatuses"] == ["At Vendor", "Closed"]

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
            set_status(db, wanted, "IN_PROGRESS", actor="tester")

        response = queues.client.get(f"{EXCEPTIONS}/{wanted}")

        assert response.status_code == 200
        body = response.json()
        assert body["id"] == wanted
        assert body["status"] == "IN_PROGRESS"
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
