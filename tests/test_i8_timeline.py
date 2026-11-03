"""The repair timeline (27-Oct-2026): it starts at the repair PR, says how the
declaration is linked, and a PR row with no PO yet says so. The first stage
reads "PR raised" since 3-Nov-2026 (it was "Repair requested").

Moved out of ``test_i8_session_check.py`` when the missing-session check that
file also covered was removed with the exception queue (3-Nov-2026)."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.api.i8.mappers import request_timeline, timeline
from app.initiatives.i8.attestation import LINK_BOUND, LINK_REFERENCE, LINK_WINDOW


def _line(**over):
    values = dict(
        purchasing_document="4500000001",
        item="10",
        item_category="3",
        doc_type="ZMNP",
        raised_at=date(2026, 9, 5),
        dispatched_at=None,
        due_date=None,
        days_at_vendor=None,
        pr_number="1000001",
        pr_item="10",
        pr_requested_on=date(2026, 9, 1),
        pr_department=None,
    )
    values.update(over)
    return SimpleNamespace(**values)


def _attestation():
    return SimpleNamespace(
        id="ATT-1",
        recommendation="REPAIRABLE",
        fault_category="WEAR",
        attestor="UNAUTHENTICATED-1",
        attestor_name="A Planner",
        attested_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )


class TestTimeline:
    def test_a_po_with_a_pr_starts_at_the_pr(self):
        stages = timeline(_line(), date(2026, 9, 11))
        assert [s.stage for s in stages] == ["pr_raised", "attested", "po_raised", "dispatched", "at_vendor"]
        assert stages[0].label == "PR raised"
        assert stages[0].occurred_at == date(2026, 9, 1)
        assert stages[0].evidence == "Repair PR 1000001/10."
        assert stages[0].days_since == 10
        assert stages[1].evidence == "Not declared yet."

    def test_the_pr_names_the_department_that_raised_it(self):
        stages = timeline(_line(pr_department="10305"), date(2026, 9, 11))
        assert stages[0].evidence.startswith("Repair PR 1000001/10, raised by department 10305")

    def test_a_po_with_no_pr_has_no_pr_stage(self):
        stages = timeline(_line(pr_number=None, pr_item=None), date(2026, 9, 11))
        assert stages[0].stage == "attested"

    def test_the_declaration_says_how_it_is_linked(self):
        today = date(2026, 9, 11)
        bound = timeline(_line(), today, _attestation(), LINK_BOUND)[1].evidence
        ref = timeline(_line(), today, _attestation(), LINK_REFERENCE, "RABC/1")[1].evidence
        window = timeline(_line(), today, _attestation(), LINK_WINDOW)[1].evidence
        assert bound.startswith("Declaration ATT-1: Repairable (wear), by A Planner.")
        assert bound.endswith("It names this repair.")
        assert ref.endswith("Linked by the R-reference RABC/1 typed on the repair order.")
        assert window.endswith("Matched by material and plant within the declaration window.")

    def test_a_declared_pr_row_shows_its_declaration(self):
        request = SimpleNamespace(
            purchase_requisition="1000001", item="10", department=None, requested_on=date(2026, 9, 1)
        )
        stages = request_timeline(request, date(2026, 9, 11), _attestation(), LINK_BOUND)
        assert [s.stage for s in stages] == ["pr_raised", "attested", "po_raised"]
        assert stages[0].label == "PR raised"
        assert stages[1].occurred_at == date(2026, 9, 2)
        assert stages[2].evidence == "No PO yet."
