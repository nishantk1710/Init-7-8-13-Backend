"""High Stockout Risk keeps counting the materials that can convert to OAR.

The Stock item recommendation population leaves those materials out (their value would be released twice), but High
Stockout Risk is about stock against consumption and lead time, not about recommendations: a Stock item filter on the
Overview must not shrink it by them.
"""

from __future__ import annotations

from sqlalchemy import select

from app.api.i7 import recommendations as api
from app.models.i7_recommendation import Recommendation
from tests.i7.test_smooth_demand import db, recommendation, summary  # noqa: F401  (db is a fixture)
from tests.i7.test_stock_item_excludes_oar_eligible import population
from tests.i7.test_working_capital_stock_to_oar import run, verdict


def seed(db):  # noqa: F811
    db.add_all([recommendation("A", "ERRATIC"), recommendation("B", "ERRATIC")])
    db.commit()
    verdict(db, run(db), "A", confidence="Medium")  # A can convert to OAR; B cannot


def test_the_filter_can_keep_the_eligible_materials(db):  # noqa: F811
    seed(db)
    args = dict(status=None, plant=None, material=None, demand_class=None, is_oar=False, confidence=None,
                criticality=None, generated_from=None, generated_to=None)

    assert population(db, is_oar=False) == ["B"]  # the Stock item recommendations leave A out, as before
    kept = api._apply_filters(select(Recommendation.sap_material_number), keep_oar_eligible=True, **args)
    assert sorted(db.scalars(kept)) == ["A", "B"]


def test_a_stock_item_overview_gives_high_stockout_risk_every_material_but_its_own_figures_skip_the_eligible_one(db, monkeypatch):  # noqa: F811
    seed(db)
    seen: dict[str, list[str]] = {}
    real = api.stockout_risk_counts

    def spy(session, base, **kwargs):
        seen["materials"] = sorted(session.scalars(base.with_only_columns(Recommendation.sap_material_number)))
        return real(session, base, **kwargs)

    monkeypatch.setattr(api, "stockout_risk_counts", spy)

    result = summary(db, is_oar=False)

    assert seen["materials"] == ["A", "B"]  # High Stockout Risk still sees the eligible material
    assert result.total == 1  # while the Stock item recommendation total does not
