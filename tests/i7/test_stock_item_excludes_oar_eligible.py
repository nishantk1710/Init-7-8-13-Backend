"""A material eligible to convert to OAR is not a Stock item recommendation.

Its value would be released twice (as a Stock item change and as the conversion), so the Stock item population leaves
it out. Its recommended ROP and Max stock are still calculated and stored; only the Stock item lists and figures skip
it. Rejecting the conversion brings it back.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from test_working_capital_stock_to_oar import db, request, run, stock, verdict  # noqa: F401  (db is a fixture)

from app.api.i7 import recommendations as api
from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.kpi_formulas.working_capital import working_capital_impact
from app.models.i7_recommendation import Recommendation


def population(session, **kw):
    """The materials the list / summary / adoption counts see for a Stock item request."""
    args = dict(status=None, plant=None, material=None, demand_class=None, is_oar=None, confidence=None, criticality=None,
                generated_from=None, generated_to=None)
    args.update(kw)
    return sorted(session.scalars(api._apply_filters(select(Recommendation.sap_material_number), **args)))


@pytest.fixture
def mix(db):  # noqa: F811
    stock(db, "A", price="100", current="5", recommended="2")  # eligible, Medium
    stock(db, "B", price="50", current="4", recommended="1")  # not eligible
    stock(db, "C", price="10", current="3", recommended="1")  # eligible, Low
    stock(db, "O", price="10", current="3", recommended="1", is_oar=True)  # an OAR material
    latest = run(db)
    verdict(db, latest, "A", confidence="Medium")
    verdict(db, latest, "C", confidence="Low")
    return db


@pytest.mark.parametrize("scope", [{"is_oar": False}, {"demand_type": "stock_item"}])
def test_the_stock_item_population_leaves_out_the_eligible_materials(mix, scope):
    assert population(mix, **scope) == ["B"]


def test_a_rejected_conversion_puts_the_material_back_into_stock_item(mix):
    request(mix, "A", status="REJECTED")
    assert population(mix, is_oar=False) == ["A", "B"]
    request(mix, "A", status="PENDING_APPROVAL")  # asked for again: the newer request decides
    assert population(mix, is_oar=False) == ["B"]


def test_one_rejected_material_does_not_free_the_others(mix):
    """The rejected check must follow each row: a rejection on A says nothing about C."""
    request(mix, "A", status="REJECTED")
    assert population(mix, is_oar=False) == ["A", "B"]  # C is still eligible, so still out


def test_a_pending_or_approved_request_keeps_it_out(mix):
    request(mix, "A", status="PENDING_APPROVAL")
    request(mix, "C", status="SAP_EXECUTION_PENDING")
    assert population(mix, is_oar=False) == ["B"]


def test_the_oar_population_and_an_unfiltered_lookup_are_untouched(mix):
    assert population(mix, is_oar=True) == ["O"]
    assert population(mix, demand_type="oar") == ["O"]
    assert population(mix) == ["A", "B", "C", "O"]  # no Stock item scope asked: nothing is hidden
    assert population(mix, material="A") == ["A"]  # looking one material up still finds it


def test_with_no_run_nothing_is_left_out(db):  # noqa: F811
    stock(db, "B", price="50", current="4", recommended="1")
    assert population(db, is_oar=False) == ["B"]


def test_the_value_change_views_of_stock_item_no_longer_count_an_eligible_material(mix):
    assert population(mix, is_oar=False, value_change="released") == ["B"]
    assert population(mix, is_oar=False, value_change="tied_up") == []


def test_the_stock_item_to_oar_view_still_reads_the_eligible_materials(mix):
    assert population(mix, is_oar=False, value_change="released_oar_only") == ["A", "C"]


def test_release_value_and_stock_item_to_oar_together_count_every_material_once(mix):
    released = population(mix, is_oar=False, value_change="released")
    oar_only = population(mix, is_oar=False, value_change="released_oar_only")
    assert not set(released) & set(oar_only)
    total = Decimal(0)
    for view, mode in ((released, None), (oar_only, "replace")):
        scoped = select(Recommendation).where(Recommendation.sap_material_number.in_(view))
        total += working_capital_impact(mix, scoped, load_active(mix), stock_to_oar=mode).amount
    assert total == Decimal("150") + Decimal("500") + Decimal("30")  # B at its own release; A and C at their OAR release


def test_on_all_an_eligible_material_is_counted_once_as_convert_to_oar(mix):
    """OAR + Stock item together: A and C at the release their conversion gives, B and the OAR material O as usual."""
    result = working_capital_impact(mix, select(Recommendation), load_active(mix), stock_to_oar="replace")
    assert result.amount == Decimal("500") + Decimal("150") + Decimal("30") + Decimal("20")
    assert (result.included_count, result.overlap_count) == (4, 0)  # four material-plants, none counted twice


def test_the_alternative_still_has_the_stock_item_rop_and_max_stock_of_an_eligible_material(mix):
    from app.initiatives.i7.reverse_conversion.repository import fetch_alternatives

    alternatives = fetch_alternatives(mix, [("A", "1300")])
    assert ("A", "1300") in alternatives  # the row stays stored; only the Stock item lists skip it
