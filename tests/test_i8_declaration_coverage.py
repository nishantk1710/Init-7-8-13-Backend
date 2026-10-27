"""Declarations cover repair PRs, and the queue says who raised each repair.

27-Oct-2026 (I08 complete flow, phase 2): a declaration bound to a repair PR
covers that PR's row and, once the PR is ordered, the PO line ordered from it;
undeclared rows raised before the attestation cutover read Historical; the queue
lists the open repair PRs and names the department that raised each repair.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.initiatives.i8 import attestation as attestation_module
from app.initiatives.i8.attestation import coverage
from app.initiatives.i8.config import get_i8_settings
from app.initiatives.i8.declarations import build_queue
from app.initiatives.i8.register import with_requisition_details
from test_i8_queue_paging import repair_line, repair_request

CFG = get_i8_settings().model_copy(update={"attestation_window_days": 30})


def declared(n: int, *, on: date, type: str | None = None, document=None, item=None, **kw):
    """A current declaration for material ``80000000{n}`` at 1300."""
    fields = dict(
        id=f"att-{n}",
        material_id=f"80000000{n:02d}",
        plant="1300",
        repair_document=document,
        repair_item=item,
        repair_document_type=type,
        attested_at=datetime(on.year, on.month, on.day, tzinfo=timezone.utc),
        recommendation="REPAIRABLE",
        attestor="A. Planner",
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


@pytest.fixture
def stored(monkeypatch):
    rows: list = []
    monkeypatch.setattr(attestation_module, "find", lambda db, include_superseded=True: list(rows))
    return rows


class TestCoverage:
    def test_a_pr_binding_covers_the_pr_and_its_later_po_line(self, stored) -> None:
        # Raised long before the declaration: only the binding can cover it.
        stored.append(declared(1, on=date(2026, 10, 1), type="PR", document="2000029101", item="00010"))
        line = repair_line(1, pr_number="2000029101", pr_item="10", raised_at=date(2025, 1, 1))
        request = repair_request(1)

        cover = coverage(None, [line], CFG, requests=[request])

        assert cover.covered[line.key].id == "att-1"
        assert cover.links[line.key] == "BOUND"
        assert line.key in cover.bound
        assert cover.requests_covered[request.key].id == "att-1"
        assert cover.request_links[request.key] == "BOUND"

    def test_a_po_binding_does_not_cover_a_pr_with_the_same_number(self, stored) -> None:
        stored.append(declared(1, on=date(2020, 1, 1), type="PO", document="2000029101", item="10"))
        request = repair_request(1)

        cover = coverage(None, [], CFG, requests=[request])

        assert cover.requests_uncovered == {request.key}

    def test_the_window_covers_a_pr_by_its_pr_date(self, stored) -> None:
        stored.append(declared(1, on=date(2026, 9, 10)))
        near = repair_request(1)  # raised 01-Sep-2026
        far = repair_request(2)

        cover = coverage(None, [], CFG, requests=[near, far])

        assert cover.request_links == {near.key: "WINDOW"}
        assert cover.requests_uncovered == {far.key}

    def test_a_windowed_po_line_says_so(self, stored) -> None:
        line = repair_line(1, raised_at=date(2026, 9, 1))
        stored.append(declared(1, on=date(2026, 9, 5)))

        cover = coverage(None, [line], CFG)

        assert cover.links == {line.key: "WINDOW"}
        assert not cover.bound


class TestQueue:
    def test_undeclared_rows_before_the_cutover_are_historical(self, stored) -> None:
        old = repair_line(1, raised_at=date(2026, 1, 1))
        new = repair_line(2, raised_at=date(2026, 9, 1))
        old_pr = repair_request(3, requested_on=date(2026, 1, 1))
        cover = coverage(None, [old, new], CFG, requests=[old_pr])

        rows = build_queue([old, new], cover, requests=[old_pr], cutover=date(2026, 6, 1))

        assert [(r.id, r.status, r.is_outstanding) for r in rows] == [
            ("D-4500000001-10", "Historical", False),
            ("D-4500000002-10", "Required", True),
            ("D-PR-2000029103-10", "Historical", False),
        ]

    def test_a_declared_line_before_the_cutover_keeps_its_declaration(self, stored) -> None:
        line = repair_line(1, raised_at=date(2026, 1, 1))
        stored.append(declared(1, on=date(2026, 1, 5), recommendation="SCRAP"))
        cover = coverage(None, [line], CFG)

        (row,) = build_queue([line], cover, cutover=date(2026, 6, 1))

        assert row.status == "Flagged"
        # Flagged has its declaration: the finding is an exception, not a wait.
        assert not row.is_outstanding
        assert row.declaration_link == "WINDOW"

    def test_pr_rows_name_the_pr_and_its_department(self, stored, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.assistant.departments.name_for", lambda code: {"10305": "Engineering"}.get(code or "")
        )
        request = repair_request(1)
        cover = coverage(None, [], CFG, requests=[request])

        (row,) = build_queue([], cover, requests=[request])

        assert (row.id, row.related_repair_id, row.repair_document, row.repair_item) == (
            "D-PR-2000029101-10",
            "PR-2000029101-10",
            "2000029101",
            "10",
        )
        assert row.repair_document_type == "PR"
        assert (row.requester, row.requester_name) == ("10305", "Engineering")
        assert row.status == "Required"

    def test_a_po_row_names_its_repair_prs_department(self, stored, monkeypatch) -> None:
        monkeypatch.setattr("app.assistant.departments.name_for", lambda code: None)
        with_pr = repair_line(1, pr_department="10305")
        without = repair_line(2)
        cover = coverage(None, [with_pr, without], CFG)

        rows = build_queue([with_pr, without], cover)

        assert [(r.requester, r.requester_name, r.repair_document_type) for r in rows] == [
            ("10305", None, "PO"),
            ("10316", None, "PO"),
        ]


class TestRequisitionDetails:
    def test_fills_each_line_from_its_pr_and_leaves_the_rest_unknown(self) -> None:
        line = repair_line(1, pr_number="2000029101", pr_item="10")
        other = repair_line(2, pr_number="2000029999", pr_item="10")
        eban = {("2000029101", "10"): {"badat": date(2026, 8, 1), "afnam": "10305"}}

        filled, untouched = with_requisition_details([line, other], eban)

        assert (filled.pr_requested_on, filled.pr_department) == (date(2026, 8, 1), "10305")
        assert untouched == other
        assert (untouched.pr_requested_on, untouched.pr_department) == (None, None)
        assert replace(filled, pr_requested_on=None, pr_department=None) == line
