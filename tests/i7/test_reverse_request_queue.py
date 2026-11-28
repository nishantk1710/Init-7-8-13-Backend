"""The conversion requests as a queue across materials: what the Action Center and the Pipeline list."""

from __future__ import annotations

from datetime import datetime, timezone

from app.initiatives.i7.recommendations.types import ApprovalAction as Action, ApprovalRole as Role
from app.initiatives.i7.reverse_approval import queue, service
from app.initiatives.i7.reverse_conversion.snapshot import find_stored

from tests.i7.test_reverse_conversion_approval import act, raise_request, session  # noqa: F401  (session is a fixture)


def test_no_requests_is_an_empty_queue(session):  # noqa: F811
    page = queue.list_requests(session)

    assert (page.rows, page.total, page.counts) == ([], 0, {})


def test_a_pending_request_is_listed_with_who_it_waits_for_and_the_material_description(session):  # noqa: F811
    raise_request(session, "REC1")

    [row] = queue.list_requests(session).rows

    assert (row.request.sap_material_number, row.request.sap_plant_code) == ("REC1", "1300")
    assert (row.request.status, row.request.pending_role) == ("PENDING_APPROVAL", "Inventory Controller")
    _, stored = find_stored(session, "REC1", "1300")
    assert row.description == stored.description


def test_counts_cover_every_status_and_the_filter_narrows_only_the_rows(session):  # noqa: F811
    pending = raise_request(session, "REC1")
    held = raise_request(session, "REC2")
    act(session, held, Role.INVENTORY_CONTROLLER, Action.HOLD, comment="waiting for stock take")

    everything = queue.list_requests(session)
    only_pending = queue.list_requests(session, statuses=["PENDING_APPROVAL"])

    assert everything.counts == {"PENDING_APPROVAL": 1, "HELD": 1}
    assert everything.total == 2
    assert [r.request.id for r in only_pending.rows] == [pending.id] and only_pending.total == 1
    assert only_pending.counts == everything.counts  # the counts do not move with the filter


def test_a_material_asked_for_again_appears_once_as_its_newest_request(session):  # noqa: F811
    first = raise_request(session, "REC1")
    act(session, first, Role.INVENTORY_CONTROLLER, Action.REJECT, comment="not now")
    second = raise_request(session, "REC1")

    page = queue.list_requests(session)

    assert [r.request.id for r in page.rows] == [second.id]
    assert page.counts == {"PENDING_APPROVAL": 1}


def test_the_most_recently_moved_request_comes_first_and_paging_slices_the_rest(session):  # noqa: F811
    a, b, c = (raise_request(session, m) for m in ("REC1", "REC2", "REC3"))
    act(session, a, Role.INVENTORY_CONTROLLER, Action.APPROVE)
    a.updated_at = datetime(2030, 1, 1, tzinfo=timezone.utc)  # moved last (set outright: two clock reads can tie)
    session.commit()

    ids = [r.request.id for r in queue.list_requests(session).rows]
    page_two = queue.list_requests(session, page=2, page_size=2)

    assert ids[0] == a.id and set(ids) == {a.id, b.id, c.id}
    assert len(page_two.rows) == 1 and page_two.total == 3


def test_a_fully_approved_request_is_listed_as_waiting_for_sap(session):  # noqa: F811
    request = raise_request(session, "REC1")
    for role in service.ROUTE:
        act(session, request, role, Action.APPROVE)

    [row] = queue.list_requests(session, statuses=["SAP_EXECUTION_PENDING"]).rows

    assert row.request.pending_role is None
