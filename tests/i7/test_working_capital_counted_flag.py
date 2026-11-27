"""The Excel export's "Counted in Working Capital" flag is the card's own rule, row by row.

The per-row SQL flag (``counted_in_working_capital``) must agree with how many rows ``working_capital_impact`` includes,
and ``why_not_counted`` must name the reason for each row the flag leaves out.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import case, select

from app.initiatives.i7.kpi_formulas.resolved import load_active
from app.initiatives.i7.kpi_formulas.working_capital import (
    counted_in_working_capital,
    why_not_counted,
    working_capital_impact,
)
from app.models.i7_recommendation import Recommendation
from app.models.i7_staging import StagedMaterialPlant

from tests.i7.test_working_capital_oar_rule import add, db  # noqa: F401  (db is the SQLite fixture)


def flags(session):
    rule = counted_in_working_capital(load_active(session))
    return {
        material: bool(flag)
        for material, flag in session.execute(select(Recommendation.sap_material_number, case((rule, 1), else_=0)))
    }


def test_the_flag_agrees_with_the_card_row_by_row(db):  # noqa: F811
    add(db, "COUNTED")                                                  # priced, ZAR, ROP values
    add(db, "PLACEHOLDER", price="1.00")                                 # at the floor
    add(db, "CHEAP", price="0.34")                                       # below the floor
    add(db, "USD", currency="USD")                                       # a currency that is not stated in ZAR
    add(db, "OAR-OK", verdict="ELIGIBLE")                                # eligible OAR counts
    add(db, "OAR-NOT", verdict="NOT_ELIGIBLE")                           # not eligible, no request
    add(db, "OAR-REJECTED", verdict="ELIGIBLE", status="REJECTED")       # rejected request

    result = flags(db)

    assert result == {
        "COUNTED": True, "PLACEHOLDER": False, "CHEAP": False, "USD": False,
        "OAR-OK": True, "OAR-NOT": False, "OAR-REJECTED": False,
    }
    impact = working_capital_impact(db, select(Recommendation), load_active(db))
    assert impact.included_count == sum(result.values()) and impact.excluded_count == len(result) - sum(result.values())


@pytest.mark.parametrize(
    ("kwargs", "words"),
    [
        (dict(unit_price=None), "no price"),
        (dict(unit_price=Decimal("0.01")), "placeholder price"),
        (dict(unit_price=Decimal("1.00")), "placeholder price"),
        (dict(currency="USD"), "not in ZAR or NAD"),
        (dict(current_rop=None), "no current or recommended ROP"),
        (dict(recommended_rop=None), "no current or recommended ROP"),
        (dict(conversion_eligibility="ELIGIBLE", status="REJECTED"), "rejected"),
        (dict(conversion_eligibility="NOT_ELIGIBLE", manually_converted=False), "not eligible to convert"),
        (dict(), "formula could not be worked out"),
    ],
)
def test_the_reason_names_the_first_rule_that_leaves_a_row_out(kwargs, words):
    row = dict(
        unit_price=Decimal("50"), currency="ZAR", current_rop=Decimal("5"), recommended_rop=Decimal("2"),
        conversion_eligibility=None, status="READY_FOR_REVIEW", manually_converted=False,
    )
    row.update(kwargs)

    assert words in why_not_counted(**row)
