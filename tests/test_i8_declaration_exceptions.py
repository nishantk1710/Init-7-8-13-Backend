"""The three declaration findings added on 27-Oct-2026: a repair sent against
its assessment, a repair PR nobody declared, and a Repairable declaration
nobody raised a repair for. Pure functions over fakes; no database."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.initiatives.i8.attestation import LINK_BOUND, LINK_WINDOW, AttestationCoverage
from app.initiatives.i8.exceptions import (
    RAISED_BY_I8,
    ExceptionType,
    build_exceptions,
    declarations_without_repair,
    repaired_against_assessment,
    undeclared_repair_requests,
)

TODAY = date(2026, 10, 27)


def _line(doc="4500000001", item="10", material="M1", plant="P1", raised=date(2026, 9, 1), pr_on=None):
    return SimpleNamespace(
        key=(doc, item),
        purchasing_document=doc,
        item=item,
        material_id=material,
        description="Pump",
        plant=plant,
        raised_at=raised,
        pr_requested_on=pr_on,
        is_overdue=False,
    )


def _request(banfn="1000000001", item="10", material="M1", plant="P1", on=date(2026, 9, 1), dept="12345"):
    return SimpleNamespace(
        key=(banfn, item),
        purchase_requisition=banfn,
        item=item,
        material_id=material,
        description="Pump",
        plant=plant,
        requested_on=on,
        department=dept,
    )


def _declaration(
    id="A1", recommendation="REPAIRABLE", material="M1", plant="P1",
    at=datetime(2026, 9, 1, 10, tzinfo=timezone.utc), doc=None, item=None,
):
    return SimpleNamespace(
        id=id,
        recommendation=recommendation,
        material_id=material,
        plant=plant,
        attested_at=at,
        attestor="planner@example.com",
        attestor_name="Asha Rao",
        repair_document=doc,
        repair_item=item,
    )


def _coverage(**kwargs):
    kwargs.setdefault("covered", {})
    kwargs.setdefault("uncovered", set())
    return AttestationCoverage(window_days=30, **kwargs)


class TestRaisedTypes:
    def test_the_three_are_raised(self):
        for t in ("REPAIRED_AGAINST_ASSESSMENT", "REPAIR_PR_NOT_DECLARED", "DECLARED_NO_REPAIR_PR"):
            assert ExceptionType(t) in RAISED_BY_I8


class TestRepairedAgainstAssessment:
    def test_po_line_declared_scrap_is_raised(self):
        line = _line()
        a = _declaration(recommendation="SCRAP")
        cover = _coverage(covered={line.key: a}, links={line.key: LINK_BOUND})
        [item] = repaired_against_assessment([line], cover)
        assert item.id == "EX-REPAIRED_AGAINST_ASSESSMENT-4500000001-10"
        assert item.declaration_id == "A1"
        assert item.severity == "warning"
        assert item.repair_document_type == "PO"
        assert "Asha Rao" in item.detail and "01 Sep 2026" in item.detail
        assert "matched by part" not in item.detail

    def test_pr_declared_ber_is_raised_with_pr_id(self):
        request = _request()
        a = _declaration(recommendation="BEYOND_ECONOMICAL_REPAIR")
        cover = _coverage(requests_covered={request.key: a}, request_links={request.key: LINK_WINDOW})
        [item] = repaired_against_assessment([], cover, requests=[request])
        assert item.id == "EX-REPAIRED_AGAINST_ASSESSMENT-PR-1000000001-10"
        assert item.repair_document_type == "PR"
        assert item.purchasing_document == "1000000001"
        assert "matched by part, plant and date" in item.detail

    def test_repairable_is_not_raised(self):
        line = _line()
        cover = _coverage(covered={line.key: _declaration()})
        assert repaired_against_assessment([line], cover) == []


class TestRepairPrNotDeclared:
    def _run(self, request, cutover=None, covered=False):
        cover = (
            _coverage(requests_covered={request.key: _declaration()})
            if covered
            else _coverage(requests_uncovered={request.key})
        )
        return undeclared_repair_requests(
            [request], cover, reference_date=TODAY, grace_days=14, cutover=cutover
        )

    def test_old_undeclared_pr_is_raised(self):
        [item] = self._run(_request(on=date(2026, 10, 1)))
        assert item.id == "EX-REPAIR_PR_NOT_DECLARED-PR-1000000001-10"
        assert item.repair_document_type == "PR"
        assert "26 days ago" in item.detail and "department 12345" in item.detail
        assert item.declaration_id is None

    def test_within_grace_is_not_raised(self):
        assert self._run(_request(on=date(2026, 10, 13))) == []  # exactly 14 days

    def test_historical_is_not_raised(self):
        assert self._run(_request(on=date(2026, 9, 1)), cutover=date(2026, 9, 15)) == []

    def test_covered_or_undated_is_not_raised(self):
        assert self._run(_request(), covered=True) == []
        assert self._run(_request(on=None)) == []


class TestDeclaredNoRepairPr:
    def _run(self, declarations, lines=(), requests=()):
        return declarations_without_repair(
            declarations, lines, requests, reference_date=TODAY, grace_days=14
        )

    def test_repairable_with_no_repair_since_is_raised(self):
        # A repair raised BEFORE the declaration does not answer it.
        [item] = self._run([_declaration()], lines=[_line(raised=date(2026, 8, 31))])
        assert item.id == "EX-DECLARED_NO_REPAIR_PR-A1"
        assert item.declaration_id == "A1"
        assert item.purchasing_document == ""
        assert item.raised_at == date(2026, 9, 1)
        assert "56 days ago" in item.detail

    def test_a_pr_or_po_raised_on_or_after_clears_it(self):
        assert self._run([_declaration()], requests=[_request(on=date(2026, 9, 1))]) == []
        assert self._run([_declaration()], lines=[_line(raised=date(2026, 9, 5))]) == []
        # A PO line is dated by its PR when the PR was read.
        assert self._run(
            [_declaration()], lines=[_line(raised=date(2026, 8, 1), pr_on=date(2026, 9, 2))]
        ) == []

    def test_other_plant_does_not_clear_it(self):
        assert len(self._run([_declaration()], requests=[_request(plant="P2")])) == 1

    def test_not_raised_within_grace_bound_or_not_repairable(self):
        recent = _declaration(at=datetime(2026, 10, 20, tzinfo=timezone.utc))
        bound = _declaration(doc="4500000001", item="10")
        scrap = _declaration(recommendation="SCRAP")
        assert self._run([recent, bound, scrap]) == []


class TestBuildExceptions:
    def test_grace_checks_need_a_reference_date(self):
        request = _request(on=date(2026, 9, 1))
        cover = _coverage(requests_uncovered={request.key})
        items, _ = build_exceptions(
            [], cover, served_requests=[request], declarations=[_declaration()]
        )
        assert items == []
        items, stats = build_exceptions(
            [], cover, served_requests=[request], declarations=[_declaration()],
            reference_date=TODAY, declaration_grace_days=14,
        )
        assert stats.by_type == {"REPAIR_PR_NOT_DECLARED": 1, "DECLARED_NO_REPAIR_PR": 1}
        # The served PR answers the declaration only if it is in repair_requests.
        items, stats = build_exceptions(
            [], cover, served_requests=[request], repair_requests=[request],
            declarations=[_declaration()], reference_date=TODAY,
        )
        assert stats.by_type == {"REPAIR_PR_NOT_DECLARED": 1}
