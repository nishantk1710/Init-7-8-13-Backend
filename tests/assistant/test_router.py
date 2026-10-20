"""W7.1 -- which flow does a material get?

No database. ``route`` needs exactly one query (the MRP type for a material at a
plant), so a fake session that answers it is enough to test every branch, and
the 80-series half is pure already.

What is deliberately NOT tested here: the two predicates themselves. Both were
built and tested before WS7 -- ``tests/test_i8_rules.py`` covers
``is_eighty_series`` and ``tests/i13/test_material_scope.py`` covers
``classify_material_scope``. Re-asserting them here would mean two places to
update when a ruling changes, and the router's own job is precedence and
plumbing, not classification.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.assistant.router import Flow, route
from app.shared.material_scope import MaterialScope


@dataclass(frozen=True)
class _Row:
    material: str
    plant: str
    mrp_type: str | None
    mrp_controller: str | None = None


class _FakeResult:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def fetchall(self) -> list[_Row]:
        return self._rows


class _FakeSession:
    """Answers the one query ``fetch_material_scope_index`` issues.

    Deliberately ignores the SQL and the filters: this stands in for MARC, and
    what matters to the router is what comes back, not how it was selected.
    ``fetch_material_scope_index`` has its own Postgres tests.
    """

    def __init__(self, rows: list[_Row] | None = None) -> None:
        self.rows = rows or []
        self.queries = 0

    def execute(self, _query, _params=None):  # noqa: ANN001 - mirrors Session.execute
        self.queries += 1
        return _FakeResult(self.rows)


# The OAR vocabulary is not injected: ``classify_material_scope`` reads the
# process-wide settings, and these tests run against its shipped default
# (MRP controller ``OAR`` is OAR, anything else is not). That is deliberate -- a test that pinned its
# own vocabulary would keep passing on the day the real ruling changed, which is
# the one thing anybody would want it to notice.


def _marc(
    material: str = "8000005632", plant: str = "1300", dispo: str | None = "OAR", mrp_type: str | None = "PD"
):
    return _FakeSession([_Row(material=material, plant=plant, mrp_type=mrp_type, mrp_controller=dispo)])


class TestTheEightySeriesHalf:
    def test_an_eighty_series_material_gets_the_i08_flow(self) -> None:
        decision = route(_FakeSession(), "8000005632", "1300")
        assert decision.flow is Flow.I08
        assert decision.eighty_series is True

    def test_the_material_number_is_normalised_first(self) -> None:
        """Ruling 5.1. An assistant opened against the zero-padded CPI form and a
        register holding the stripped form are about the same part -- a router
        that cannot see that routes a repairable to nothing."""
        decision = route(_FakeSession(), "000000008000005632", "1300")
        assert decision.flow is Flow.I08
        assert decision.material_id == "8000005632"


class TestTheOarHalf:
    def test_an_oar_material_gets_the_i13_flow(self) -> None:
        decision = route(_marc(material="1000000123", dispo="OAR", mrp_type="ND"), "1000000123", "1300")
        assert decision.flow is Flow.I13
        assert decision.material_scope is MaterialScope.OAR
        assert decision.mrp_controller == "OAR"
        assert decision.mrp_type == "ND"
        assert "MRP controller OAR" in decision.reason

    def test_another_controller_is_not_oar_and_gets_nothing(self) -> None:
        decision = route(_marc(material="1000000123", dispo="POD"), "1000000123", "1300")
        assert decision.flow is Flow.NONE
        assert decision.material_scope is MaterialScope.EXCLUDED
        assert "MRP controller POD is not OAR" in decision.reason

    def test_the_mrp_type_no_longer_decides(self) -> None:
        """ND/PD was the rule until 20 Oct 2026; DISPO is now."""
        decision = route(_marc(material="1000000123", dispo="POD", mrp_type="ND"), "1000000123", "1300")
        assert decision.flow is Flow.NONE
        decision = route(_marc(material="1000000123", dispo="OAR", mrp_type="VB"), "1000000123", "1300")
        assert decision.flow is Flow.I13

    def test_scope_is_decided_per_plant(self) -> None:
        """The same material can be OAR at one plant and not at another --
        DISPO is plant-level (MARC), which is the whole reason the router takes
        a plant at all."""
        marc = _FakeSession(
            [
                _Row(material="1000000123", plant="1300", mrp_type="PD", mrp_controller="OAR"),
                _Row(material="1000000123", plant="1500", mrp_type="PD", mrp_controller="POD"),
            ]
        )
        assert route(marc, "1000000123", "1300").flow is Flow.I13
        assert route(marc, "1000000123", "1500").flow is Flow.NONE


class TestNoScope:
    def test_a_material_that_is_neither_gets_no_flow(self) -> None:
        """And therefore no session. Minting one to record silence would put a
        row in an append-only table for every reservation we have no opinion
        about."""
        decision = route(_marc(material="1000000123", dispo="POD"), "1000000123", "1300")
        assert decision.flow is Flow.NONE
        assert decision.in_scope is False

    def test_a_missing_marc_row_is_reported_as_a_data_gap_not_a_classification(self) -> None:
        """"MARC does not know" and "MARC says this is not OAR" are different
        statements and the reason says which."""
        decision = route(_FakeSession([]), "1000000123", "1300")
        assert decision.flow is Flow.NONE
        assert decision.mrp_type is None
        assert decision.mrp_controller is None
        assert "MARC has no row" in decision.reason

    def test_a_blank_dispo_is_not_oar(self) -> None:
        """A MARC row with no MRP controller is not OAR -- unlike a blank DISMM
        under the rule before 20 Oct 2026."""
        decision = route(_marc(material="1000000123", dispo=""), "1000000123", "1300")
        assert decision.material_scope is MaterialScope.EXCLUDED
        assert decision.flow is Flow.NONE
        assert "MRP controller blank is not OAR" in decision.reason


class TestPrecedence:
    """Nothing stops a material being both. The rules read different fields."""

    def test_i08_wins_when_both_match(self) -> None:
        decision = route(_marc(dispo="OAR"), "8000005632", "1300")
        assert decision.flow is Flow.I08

    def test_the_losing_match_is_recorded_rather_than_discarded(self) -> None:
        """This is a business decision, not a derivation, and it needs
        confirming -- so the flow that did not run travels on the result instead
        of disappearing into a conditional."""
        decision = route(_marc(dispo="OAR"), "8000005632", "1300")
        assert decision.also_matched is Flow.I13
        assert decision.material_scope is MaterialScope.OAR
        assert "takes precedence" in decision.reason

    def test_nothing_is_recorded_when_only_one_matched(self) -> None:
        decision = route(_marc(dispo="POD"), "8000005632", "1300")
        assert decision.flow is Flow.I08
        assert decision.also_matched is None


class TestTheDecisionCarriesItsInputs:
    def test_every_routing_input_is_on_the_result(self) -> None:
        """A requester shown the wrong flow asks why, and MARC's MRP controller
        may have changed by the time anybody looks."""
        decision = route(_marc(dispo="OAR", mrp_type="PD"), "8000005632", "1300")
        assert decision.eighty_series is True
        assert decision.material_scope is MaterialScope.OAR
        assert decision.mrp_controller == "OAR"
        assert decision.mrp_type == "PD"
        assert decision.plant == "1300"

    @pytest.mark.parametrize("dispo,expected", [("OAR", Flow.I13), ("POD", Flow.NONE)])
    def test_the_reason_is_a_sentence_a_person_can_read(self, dispo: str, expected: Flow) -> None:
        decision = route(_marc(material="1000000123", dispo=dispo), "1000000123", "1300")
        assert decision.flow is expected
        assert decision.reason.startswith("1000000123")
        assert decision.reason.endswith(".")


class TestCost:
    def test_routing_costs_exactly_one_query(self) -> None:
        """The 80-series test is pure, so the database is only ever asked for
        the OAR half. This runs on every reservation pop-up."""
        session = _marc()
        route(session, "8000005632", "1300")
        assert session.queries == 1
